import { defineStore } from 'pinia'
import { computed, reactive, ref } from 'vue'
import { ApiError, errorMessage, errorStatus } from '@/api/http'
import * as api from '@/api/business'
import { buildCards, lastJobSeq, mergeJobEvents, scopeEvents } from '@/lib/business'
import type {
  BusinessJob,
  DatasetSummary,
  JobEvent,
  ModelSummary,
  RecordFiltersState,
  RecordPage,
} from '@/types'

/**
 * 业务状态（检测 / 核查 / 巡检 / 数据集 / 模型）。
 *
 * 这里最容易做错的一件事：**只在当前对话轮次没结束时轮询业务进度**。
 * 长任务往往比对话更久（一次巡检跑几分钟很正常），一旦按“轮次是否终态”
 * 去关掉监听，用户就会看到“对话结束了、任务卡住了”，实际上任务还在跑。
 *
 * 所以监听按**会话**走：进入会话就开始拉 ``/job-events``，用 seq 游标增量续拉，
 * 与轮次状态完全无关；浏览器刷新、切走再回来、对话已经结束，都靠同一个游标补齐。
 */

const ACTIVE_DELAY_MS = 1200
const IDLE_DELAY_MS = 5000
const WATCH_CEILING_MS = 60 * 60 * 1000

export const useBusinessStore = defineStore('business', () => {
  const events = reactive<Record<string, JobEvent[]>>({})
  const jobs = reactive<Record<string, BusinessJob[]>>({})
  const records = reactive<Record<string, RecordPage | null>>({})
  const datasets = reactive<Record<string, DatasetSummary[]>>({})
  const models = reactive<Record<string, ModelSummary[]>>({})
  const loading = ref(false)
  const busy = ref('')
  const error = ref('')
  const notice = ref('')

  const filters = reactive<Record<string, RecordFiltersState>>({})
  const watching = new Map<string, { timer: ReturnType<typeof setTimeout>; started: number; running: boolean }>()
  let generation = 0
  // 记录页**当前真实范围**（筛选 + 回收站视图，不含分页游标）：作业刷新时必须
  // 在这个范围内更新，绝不能退回"整个会话"——筛选框还在而表格变全项目就是错觉。
  const recordScope: Record<string, Partial<RecordFiltersState> & { session_id?: string; deleted?: string }> = {}

  function filtersFor(projectId: string): RecordFiltersState {
    if (!filters[projectId]) {
      filters[projectId] = { status: '', model_id: '', has_hits: null, date_from: '', date_to: '', q: '' }
    }
    return filters[projectId]
  }

  function cardsFor(sessionId: string) {
    return buildCards(scopeEvents(events[sessionId] ?? [], sessionId), jobs[sessionId] ?? [])
  }

  function activeJobs(sessionId: string): BusinessJob[] {
    return (jobs[sessionId] ?? []).filter((job) => job.status === 'running' || job.status === 'queued')
  }

  // ------------------------------------------------------------------ 会话级监听

  function watchSession(projectId: string, sessionId: string): void {
    if (!sessionId) return
    if (watching.has(sessionId)) return
    const token = generation
    const entry = { timer: 0 as unknown as ReturnType<typeof setTimeout>, started: Date.now(), running: false }
    watching.set(sessionId, entry)
    const tick = async (): Promise<void> => {
      if (token !== generation || !watching.has(sessionId)) return
      const current = watching.get(sessionId)!
      if (Date.now() - current.started > WATCH_CEILING_MS) {
        stopWatching(sessionId)
        return
      }
      await pull(projectId, sessionId, token)
      if (token !== generation || !watching.has(sessionId)) return
      const delay = (watching.get(sessionId)?.running ?? false) ? ACTIVE_DELAY_MS : IDLE_DELAY_MS
      current.timer = setTimeout(() => void tick(), delay)
      watching.set(sessionId, current)
    }
    void tick()
  }

  async function pull(projectId: string, sessionId: string, token: number): Promise<void> {
    try {
      const data = await api.jobEvents(projectId, {
        after: lastJobSeq(events[sessionId] ?? []),
        session_id: sessionId,
      })
      if (token !== generation) return
      if (data.events.length) {
        events[sessionId] = mergeJobEvents(events[sessionId] ?? [], data.events)
        // 有新事件说明刚有进展：立刻刷新作业清单与记录，让页面和卡片同步。
        // 记录按**当前筛选范围**刷新，不退回"整个会话"。
        void refreshJobs(projectId, sessionId)
        void loadRecords(projectId, { ...recordScope[projectId] }, true)
      }
      const entry = watching.get(sessionId)
      if (entry) {
        entry.running = activeJobs(sessionId).length > 0
        watching.set(sessionId, entry)
      }
    } catch (failure) {
      const status = errorStatus(failure)
      if (status === 401 || status === 403 || status === 404) {
        // 确定结论：继续轮询永远不会成功。
        stopWatching(sessionId)
        return
      }
      // 断网/后端抖动：保持监听，下一次 tick 再试，不清空已有卡片。
    }
  }

  function stopWatching(sessionId: string): void {
    const entry = watching.get(sessionId)
    if (entry) clearTimeout(entry.timer)
    watching.delete(sessionId)
  }

  // ------------------------------------------------------------------ 读

  async function refreshJobs(projectId: string, sessionId?: string): Promise<void> {
    if (!sessionId) return
    try {
      const data = await api.listJobs(projectId, { session_id: sessionId, limit: 20 })
      jobs[sessionId] = data.jobs
      const entry = watching.get(sessionId)
      if (entry) {
        entry.running = data.jobs.some((job) => job.status === 'running' || job.status === 'queued')
        watching.set(sessionId, entry)
      }
    } catch (failure) {
      error.value = errorMessage(failure)
    }
  }

  async function loadRecords(
    projectId: string,
    params: Partial<RecordFiltersState> & {
      session_id?: string
      before?: string
      limit?: number
      deleted?: string
    } = {},
    silent = false,
  ): Promise<void> {
    if (!silent) loading.value = true
    try {
      const { before: _cursor, limit: _limit, ...scope } = params
      recordScope[projectId] = scope
      const scopeKey = JSON.stringify(scope)
      const data = await api.listRecords(projectId, { limit: 20, ...params })
      // 会话页后台作业轮询可能带着旧 session 范围迟到；若页面已切到新的筛选范围，
      // 旧响应必须丢弃，避免把刚加载好的记录表覆盖成空列表。
      if (JSON.stringify(recordScope[projectId]) !== scopeKey) return
      records[projectId] = data
    } catch (failure) {
      error.value = errorMessage(failure)
    } finally {
      if (!silent) loading.value = false
    }
  }

  async function loadDatasets(projectId: string, deleted = 'exclude'): Promise<void> {
    loading.value = true
    try {
      datasets[projectId] = (await api.listDatasets(projectId, deleted)).datasets
    } catch (failure) {
      error.value = errorMessage(failure)
    } finally {
      loading.value = false
    }
  }

  async function loadModels(projectId: string): Promise<void> {
    loading.value = true
    try {
      models[projectId] = (await api.listModels(projectId)).models
    } catch (failure) {
      error.value = errorMessage(failure)
    } finally {
      loading.value = false
    }
  }

  const publishedModels = computed(() => (projectId: string) =>
    (models[projectId] ?? []).filter((model) => model.status === 'published'),
  )

  // ------------------------------------------------------------------ 写

  async function reviewSelected(projectId: string, recordIds: string[]): Promise<boolean> {
    if (!recordIds.length) {
      error.value = '先勾选要核查的记录；只想核查一条就只勾一条'
      return false
    }
    busy.value = 'review'
    error.value = ''
    notice.value = ''
    try {
      const data = await api.reviewRecords(projectId, recordIds)
      notice.value = data.created
        ? `已开始核查 ${recordIds.length} 条记录，结论会写入记录详情（仅供参考，不等于人工复核）`
        : '这个核查作业已经存在，没有重复提交'
      const sessionId = data.job.session_id
      if (sessionId) {
        await refreshJobs(projectId, sessionId)
        if (!watching.has(sessionId)) watchSession(projectId, sessionId)
      }
      // 核查启动后的首刷也留在当前筛选范围内（root UI-04A 反例：原来这里
      // 只按 session_id 刷，把文件名/日期/状态/回收站筛选全丢了）。
      await loadRecords(projectId, { ...recordScope[projectId] }, true)
      return true
    } catch (failure) {
      if (failure instanceof ApiError && failure.code === 'VISION_UNVERIFIED') {
        // 这次 409 是“当前对话模型没有看图证据”，不是网络抖动：
        // 明确告诉用户当前模型和下一步，避免只剩一句抽象的前置条件错误。
        error.value = `${errorMessage(failure)}。当前对话选择的模型不能看图；请到设置页「模型连接」切换回已通过图像验证的模型，重新点“检查连接”后再核查。`
      } else {
        error.value = errorMessage(failure)
      }
      return false
    } finally {
      busy.value = ''
    }
  }

  async function publish(projectId: string, modelId: string): Promise<boolean> {
    busy.value = `publish:${modelId}`
    error.value = ''
    notice.value = ''
    try {
      const data = await api.publishModel(projectId, modelId)
      notice.value = `模型「${data.model.name}」已在本项目发布，可以被检测使用`
      await loadModels(projectId)
      return true
    } catch (failure) {
      error.value = errorMessage(failure)
      return false
    } finally {
      busy.value = ''
    }
  }

  async function importDataset(
    projectId: string,
    payload: Parameters<typeof api.importDataset>[1],
  ): Promise<boolean> {
    busy.value = 'dataset'
    error.value = ''
    notice.value = ''
    try {
      const data = await api.importDataset(projectId, payload)
      notice.value = data.dataset.reused
        ? `该数据集已存在（内容、模态、类别、标注与划分都相同），已使用现有数据集「${data.dataset.name}」`
        : `数据集「${data.dataset.name}」导入完成：${data.dataset.sample_count} 个样本`
      await loadDatasets(projectId)
      return true
    } catch (failure) {
      error.value = errorMessage(failure)
      return false
    } finally {
      busy.value = ''
    }
  }

  async function importModel(
    projectId: string,
    payload: Parameters<typeof api.importModel>[1],
  ): Promise<boolean> {
    busy.value = 'model'
    error.value = ''
    notice.value = ''
    try {
      const data = await api.importModel(projectId, payload)
      notice.value =
        data.model.status === 'published'
          ? `模型「${data.model.name}」已导入并发布`
          : `模型「${data.model.name}」已导入，确认类别映射后发布`
      await loadModels(projectId)
      return true
    } catch (failure) {
      error.value = errorMessage(failure)
      return false
    } finally {
      busy.value = ''
    }
  }

  function reset(): void {
    generation += 1
    for (const sessionId of [...watching.keys()]) stopWatching(sessionId)
    for (const key of Object.keys(events)) delete events[key]
    for (const key of Object.keys(jobs)) delete jobs[key]
    for (const key of Object.keys(records)) delete records[key]
    for (const key of Object.keys(datasets)) delete datasets[key]
    for (const key of Object.keys(models)) delete models[key]
    for (const key of Object.keys(filters)) delete filters[key]
    loading.value = false
    busy.value = ''
    error.value = ''
    notice.value = ''
  }

  return {
    events,
    jobs,
    records,
    datasets,
    models,
    loading,
    busy,
    error,
    notice,
    filtersFor,
    cardsFor,
    activeJobs,
    watchSession,
    stopWatching,
    refreshJobs,
    loadRecords,
    loadDatasets,
    loadModels,
    publishedModels,
    reviewSelected,
    publish,
    importDataset,
    importModel,
    reset,
  }
})
