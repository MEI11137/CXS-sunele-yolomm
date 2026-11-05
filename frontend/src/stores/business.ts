import { defineStore } from 'pinia'
import { computed, reactive, ref } from 'vue'
import { errorMessage, errorStatus } from '@/api/http'
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

  function filtersFor(projectId: string): RecordFiltersState {
    if (!filters[projectId]) {
      filters[projectId] = { status: '', model_id: '', has_hits: null, date_from: '', date_to: '' }
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
        void refreshJobs(projectId, sessionId)
        void loadRecords(projectId, { session_id: sessionId }, true)
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
    params: Partial<RecordFiltersState> & { session_id?: string; before?: string; limit?: number } = {},
    silent = false,
  ): Promise<void> {
    if (!silent) loading.value = true
    try {
      const data = await api.listRecords(projectId, { limit: 20, ...params })
      records[projectId] = data
    } catch (failure) {
      error.value = errorMessage(failure)
    } finally {
      if (!silent) loading.value = false
    }
  }

  async function loadDatasets(projectId: string): Promise<void> {
    loading.value = true
    try {
      datasets[projectId] = (await api.listDatasets(projectId)).datasets
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
      await loadRecords(projectId, { session_id: sessionId ?? undefined }, true)
      return true
    } catch (failure) {
      error.value = errorMessage(failure)
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
      notice.value = `数据集「${data.dataset.name}」导入完成：${data.dataset.sample_count} 个样本`
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
