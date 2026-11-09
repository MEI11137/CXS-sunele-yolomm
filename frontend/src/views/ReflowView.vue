<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import * as api from '@/api/business'
import { errorCode, errorMessage } from '@/api/http'
import {
  actionOf as resolveAction,
  buildSelections,
  defaultAction,
  formSnapshot,
  isPlanStale,
  splitOf as resolveSplit,
  splitRequired as needsSplit,
} from '@/lib/reflow'
import type { ReflowAction, ReflowForm, ReflowSplit } from '@/lib/reflow'
import { useBusinessStore } from '@/stores/business'
import { useWorkspaceStore } from '@/stores/workspace'
import type { DatasetReport, FeedbackSummary, ReflowPlan } from '@/types'

/**
 * 数据回流页：把「已确认的复核结论」变成真实的下一版数据集。
 *
 * 两种模式：
 * * 回原修订：同图替换 label，样本数不增加；新图要显式选“追加 + 划分”。
 * * 另建数据集：只取选中样本，按 1/1/1 或比例 + 固定种子划分，同一身份不跨划分。
 *
 * 预览只算不写；有冲突就停，歧义交给用户选，不自动覆盖。
 */

const route = useRoute()
const router = useRouter()
const business = useBusinessStore()
const workspace = useWorkspaceStore()

const projectId = computed(() => String(route.params.projectId ?? ''))
const datasets = computed(() => business.datasets[projectId.value] ?? [])

const mode = ref<'revision' | 'new'>('revision')
const datasetId = ref('')
const name = ref('')
const strategy = ref<'even' | 'ratio' | 'manual'>('even')
// 参考模态：labels/ 与参考图以它为准；auto = 交给后端按已确认版本/父数据集决定。
const referenceModality = ref<'auto' | 'rgb' | 'ir' | 'edge' | 'depth'>('auto')
const seed = ref(7)
const ratios = ref({ train: 7, val: 2, test: 1 })
const feedback = ref<FeedbackSummary[]>([])
const chosen = ref<Record<string, boolean>>({})
const actions = ref<Record<string, 'replace' | 'append' | 'exclude'>>({})
const splits = ref<Record<string, 'train' | 'val' | 'test'>>({})
const targetSamples = ref<Record<string, string>>({})
const plan = ref<ReflowPlan | null>(null)
const draftId = ref('')
const draftStatus = ref('')
const report = ref<DatasetReport | null>(null)
let reportEpoch = 0
const error = ref('')
const notice = ref('')
const busy = ref('')

const pending = computed(() => feedback.value.filter((item) => item.status === 'pending'))
const selectedIds = computed(() => Object.keys(chosen.value).filter((key) => chosen.value[key]))

// 模态 token 只用于显示，不参与任何坐标/导出逻辑。
function modalityLabel(token: string | null | undefined): string {
  const value = String(token || '').toLowerCase()
  if (value === 'rgb') return 'RGB'
  if (value === 'ir') return 'IR'
  if (value === 'edge') return '边缘'
  if (value === 'depth') return '深度'
  return value ? value.toUpperCase() : '辅模态'
}

// 缺陷结论必须同时给出两套真实框数：IR-only 缺陷不能显示成 0 框。
function conclusionText(item: FeedbackSummary): string {
  const review = item.review
  if (!review) return '—'
  if (review.outcome === 'no_defect') return '确认无缺陷'
  const parts: string[] = []
  if (review.box_count > 0) parts.push(`${review.box_count} 框（${modalityLabel(review.reference_modality || 'rgb')}）`)
  if ((review.x_box_count ?? 0) > 0) {
    parts.push(`${review.x_box_count} 框（${modalityLabel(review.x_modality || review.secondary_modality)}）`)
  }
  return `确认有缺陷（${parts.join(' + ') || '0 框'}）`
}

const form = computed<ReflowForm>(() => ({
  mode: mode.value,
  datasetId: datasetId.value,
  name: name.value,
  strategy: strategy.value,
  ratios: ratios.value,
  seed: seed.value,
}))

// 模板与提交共用同一份取值：显示什么就提交什么。
function actionOf(recordId: string): ReflowAction {
  return resolveAction(form.value, actions.value, recordId)
}

function splitRequired(recordId: string): boolean {
  return needsSplit(form.value, actions.value, recordId)
}

function splitOf(recordId: string): ReflowSplit {
  return resolveSplit(splits.value, recordId)
}

function setAction(recordId: string, event: Event): void {
  const value = (event.target as HTMLSelectElement).value as ReflowAction
  actions.value = { ...actions.value, [recordId]: value }
}

function setSplit(recordId: string, event: Event): void {
  const value = (event.target as HTMLSelectElement).value as ReflowSplit
  splits.value = { ...splits.value, [recordId]: value }
}

function selections() {
  return buildSelections(
    selectedIds.value,
    form.value,
    actions.value,
    splits.value,
    targetSamples.value,
  )
}

const previewedState = ref('')

function referenceValue(): 'rgb' | 'ir' | 'edge' | 'depth' | '' {
  return referenceModality.value === 'auto' ? '' : referenceModality.value
}

/** 参考模态也影响导出结果，必须一起进过期判断。 */
function formSignature(): string {
  return (
    formSnapshot(form.value, selectedIds.value, actions.value, splits.value, targetSamples.value) +
    `|ref:${referenceValue()}`
  )
}

const planStale = computed(() => isPlanStale(previewedState.value, formSignature()))

onMounted(async () => {
  await business.loadDatasets(projectId.value)
  const preferredDataset = String(route.query.dataset_id ?? '').trim()
  datasetId.value =
    datasets.value.find((item) => item.id === preferredDataset)?.id ?? datasets.value[0]?.id ?? ''
  await loadFeedback()
  // 从复核页“去回流”进入：直接带上当前记录，减少跨页勾选。
  const preferredRecord = String(route.query.record_id ?? '').trim()
  if (preferredRecord) {
    const match = feedback.value.find((item) => item.detection_id === preferredRecord)
    if (match) {
      chosen.value = { ...chosen.value, [match.detection_id]: true }
      notice.value = '已带入当前复核记录。选择“回原修订”或“另建数据集”后直接预览即可。'
    } else {
      notice.value = '这条记录还没进入回流池；先确认复核结论，或在本页选择其他反馈。'
    }
  }
})

// 切模式时把动作归位到该模式允许的取值，否则会带着另一个模式的默认值去提交。
watch(mode, () => {
  const next: Record<string, ReflowAction> = {}
  for (const item of feedback.value) {
    next[item.detection_id] = defaultAction(mode.value)
  }
  actions.value = next
})

watch(datasetId, () => {
  plan.value = null
  report.value = null
  // 旧数据集报告迟到时不能再写进当前画布；代次一变，旧请求的回填全部作废。
  reportEpoch += 1
  // 旧请求还挂着时切数据集，应允许立刻检查新数据集；旧请求的 finally 由代次挡住。
  if (busy.value === 'report') busy.value = ''
})

async function loadFeedback(): Promise<void> {
  try {
    const data = await api.listFeedback(projectId.value, 'pending')
    feedback.value = data.feedback
  } catch (failure) {
    error.value = errorMessage(failure)
  }
}


async function preview(): Promise<void> {
  error.value = ''
  notice.value = ''
  busy.value = 'preview'
  // 记下**这次真正提交出去**的表单值；定稿前拿它和当前表单比对。
  const submitted = formSignature()
  try {
    const data = await api.saveReflowDraft(projectId.value, {
      draft_id: draftId.value || undefined,
      name: name.value || `回流草稿 ${new Date().toLocaleDateString()} ${new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`,
      mode: mode.value,
      dataset_id: mode.value === 'revision' ? datasetId.value || null : null,
      selections: selections(),
      strategy: strategy.value,
      ratios: strategy.value === 'ratio' ? ratios.value : undefined,
      seed: seed.value,
      reference_modality: referenceValue() || undefined,
      session_id: workspace.currentSessionId || undefined,
    })
    draftId.value = data.draft.id
    draftStatus.value = data.draft.status
    plan.value = data.draft.plan
    previewedState.value = submitted
    notice.value =
      data.draft.status === 'ready'
        ? '草稿已可定稿；还没有生成任何数据集文件。'
        : '草稿已保存，需要先解决冲突。'
  } catch (failure) {
    error.value = [errorCode(failure), errorMessage(failure)].filter(Boolean).join('：')
  } finally {
    busy.value = ''
  }
}

async function apply(): Promise<void> {
  if (!draftId.value || !plan.value) {
    error.value = '先保存并预览草稿，确认没有冲突再定稿'
    return
  }
  if (planStale.value) {
    // 界面上改了模式/名称/所选反馈/动作/划分/比例/种子，旧草稿已经不是用户看到的计划。
    error.value = '表单改过了，这份预览对应的是改动前的设置；请重新“保存草稿并预览”再定稿。'
    return
  }
  if (plan.value.conflicts.length) {
    error.value = '草稿里有冲突，先解决再定稿（系统不会自动覆盖）'
    return
  }
  busy.value = 'apply'
  error.value = ''
  try {
    const data = await api.finalizeReflowDraft(projectId.value, draftId.value)
    notice.value = data.note || `已生成数据集「${data.dataset.name}」：${data.dataset.sample_count} 个样本`
    draftStatus.value = 'finalized'
    plan.value = null
    draftId.value = ''
    previewedState.value = ''
    name.value = ''
    await business.loadDatasets(projectId.value)
    await loadFeedback()
  } catch (failure) {
    const details = (failure as { details?: { conflicts?: unknown[] } }).details
    if (details?.conflicts?.length) {
      error.value = '执行时发现冲突，本次没有写入任何文件；请重新预览并处理冲突。'
    } else {
      error.value = errorMessage(failure)
    }
  } finally {
    busy.value = ''
  }
}

async function checkIdentity(): Promise<void> {
  // 新建模式不基于父数据集生成样本，此时“检查重复”不能偷偷检查一个页面上不可见的数据集。
  if (mode.value !== 'revision') {
    report.value = null
    notice.value = '新建模式不基于父数据集，不执行重复/泄漏检查；如需检查请切换到修订模式。'
    return
  }
  const requestedProject = projectId.value
  const requestedDataset = datasetId.value
  if (!requestedDataset) return
  const epoch = ++reportEpoch
  const stillCurrent = (): boolean =>
    epoch === reportEpoch &&
    projectId.value === requestedProject &&
    datasetId.value === requestedDataset
  busy.value = 'report'
  error.value = ''
  try {
    const data = await api.datasetReport(requestedProject, requestedDataset)
    if (!stillCurrent()) return
    report.value = data
  } catch (failure) {
    if (!stillCurrent()) return
    error.value = errorMessage(failure)
  } finally {
    if (epoch === reportEpoch) busy.value = ''
  }
}

function chooseTarget(recordId: string, sampleId: string): void {
  targetSamples.value = { ...targetSamples.value, [recordId]: sampleId }
  void preview()
}

function openDataset(): void {
  void router.push({ name: 'datasets', params: { projectId: projectId.value } })
}

function goReview(): void {
  void router.push({ name: 'reviewQueue', params: { projectId: projectId.value } })
}

function goRecords(): void {
  void router.push({ name: 'records', params: { projectId: projectId.value } })
}
</script>

<template>
  <section class="page">
    <header class="page-header">
      <div>
        <h1>数据回流</h1>
        <p class="subtitle">
          待整理反馈 {{ pending.length }} 条
          <span class="hint">· 只取已确认且未回流的版本；草稿只存计划，定稿才写文件</span>
        </p>
      </div>
      <div class="header-actions">
        <button class="ghost small" type="button" @click="loadFeedback">刷新反馈</button>
        <button class="ghost small" type="button" @click="openDataset">返回数据集</button>
      </div>
    </header>

    <p v-if="error" class="banner banner-error" role="alert">{{ error }}</p>
    <p v-else-if="notice" class="banner banner-ok" role="status">{{ notice }}</p>

    <div class="card">
      <p class="card-head">① 选择样本 · 待整理反馈（{{ pending.length }}）</p>
      <table v-if="pending.length" class="table">
        <thead>
          <tr>
            <th></th>
            <th>记录</th>
            <th>复核版本</th>
            <th>结论</th>
            <th>动作</th>
            <th>划分</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in pending" :key="item.id">
            <td><input v-model="chosen[item.detection_id]" type="checkbox" /></td>
            <td class="mono">{{ item.detection_id }}</td>
            <td>v{{ item.review?.version ?? '—' }}</td>
            <td>{{ conclusionText(item) }}</td>
            <td>
              <select
                :value="actionOf(item.detection_id)"
                @change="setAction(item.detection_id, $event)"
              >
                <option v-if="mode === 'revision'" value="replace">回原替换</option>
                <option value="append">追加为新图</option>
                <option value="exclude">不处理</option>
              </select>
            </td>
            <td>
              <!-- 回原替换固定沿用父划分；自动划分由服务端按身份分组算。 -->
              <span v-if="!splitRequired(item.detection_id)" class="hint">
                {{ mode === 'revision' ? '沿用父划分' : '自动划分' }}
              </span>
              <select
                v-else
                :value="splitOf(item.detection_id)"
                @change="setSplit(item.detection_id, $event)"
              >
                <option value="train">train</option>
                <option value="val">val</option>
                <option value="test">test</option>
              </select>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-else class="empty-hint">
        还没有待整理反馈。确认记录后点「加入反馈」，确认过的版本会出现在这里。
      </p>
      <div v-if="!pending.length" class="inline-form">
        <button class="ghost small" type="button" @click="goReview">前往人工复核</button>
        <button class="ghost small" type="button" @click="goRecords">前往识别记录</button>
      </div>
    </div>

    <div class="card">
      <p class="card-head">② 回流方式与目标</p>
      <div class="form-grid">
        <label class="field">
          <span>模式</span>
          <select v-model="mode">
            <option value="revision">回原修订（同图替换 label，不增样本）</option>
            <option value="new">另建数据集（只取选中样本并重新划分）</option>
          </select>
        </label>
        <label class="field">
          <span>参考模态（labels / 参考图）</span>
          <select v-model="referenceModality">
            <option value="auto">自动（按已确认版本）</option>
            <option value="rgb">RGB</option>
            <option value="ir">红外 IR</option>
            <option value="edge">边缘</option>
            <option value="depth">深度</option>
          </select>
        </label>
        <label v-if="mode === 'revision'" class="field">
          <span>父数据集</span>
          <select v-model="datasetId">
            <option value="">选择数据集</option>
            <option v-for="item in datasets" :key="item.id" :value="item.id">
              {{ item.name }}（{{ item.sample_count }} 样本）
            </option>
          </select>
        </label>
        <label class="field">
          <span>新数据集名称</span>
          <input v-model="name" placeholder="留空自动命名" maxlength="60" />
        </label>
        <label v-if="mode === 'new'" class="field">
          <span>划分方式</span>
          <select v-model="strategy">
            <option value="even">1/1/1（按身份分组轮流）</option>
            <option value="ratio">按比例 + 固定种子</option>
            <option value="manual">逐样本指定（每条都要选划分）</option>
          </select>
        </label>
        <label v-if="mode === 'new'" class="field">
          <span>随机种子</span>
          <input v-model.number="seed" type="number" min="0" step="1" />
        </label>
        <label v-if="mode === 'new' && strategy === 'ratio'" class="field">
          <span>比例 train/val/test</span>
          <input
            :value="`${ratios.train}/${ratios.val}/${ratios.test}`"
            @input="
              (event) => {
                const parts = (event.target as HTMLInputElement).value.split('/').map(Number)
                if (parts.length === 3 && parts.every((n) => n >= 0)) {
                  ratios = { train: parts[0], val: parts[1], test: parts[2] }
                }
              }
            "
          />
        </label>
      </div>
      <div class="inline-form">
        <button class="ghost small" type="button" :disabled="!datasetId || mode !== 'revision'" @click="checkIdentity">
          检查重复 / 跨划分泄漏
        </button>
        <span class="hint">范围：所选数据集的全部样本（只读），与上方待整理反馈是否为空无关。</span>
        <span v-if="report && report.sample_count === 0" class="form-error" role="alert">
          该数据集暂无样本，未执行重复/泄漏/冲突检查；请先加入样本再检查。
        </span>
        <span v-else-if="report" class="hint">
          重复组 {{ report.duplicate_groups.length }} · 泄漏 {{ report.cross_split_leakage.length }} ·
          标注冲突 {{ report.annotation_conflicts.length }}
        </span>
      </div>
    </div>

    <div class="form-actions">
      <button class="primary small" type="button" :disabled="!selectedIds.length || busy !== ''" @click="preview">
        {{ busy === 'preview' ? '计算中…' : `③ 保存草稿并预览（${selectedIds.length} 条）` }}
      </button>
      <button
        class="primary small"
        type="button"
        :disabled="
          !plan || draftStatus !== 'ready' || busy !== '' || planStale || (plan?.conflicts.length ?? 0) > 0
        "
        @click="apply"
      >
        {{ busy === 'apply' ? '写入中…' : '定稿并生成版本' }}
      </button>
      <span v-if="planStale" class="form-error">
        表单已改动，先重新保存预览，否则定稿执行的还是旧设置。
      </span>
    </div>

    <div v-if="plan" class="card">
      <p class="card-head">
        预览结果
        <span v-if="planStale" class="form-error">（已过期：下面的数字对应改动前的表单）</span>
      </p>
      <dl class="kv">
        <div><dt>替换</dt><dd>{{ plan.counts.replace }}</dd></div>
        <div><dt>追加</dt><dd>{{ plan.counts.append }}</dd></div>
        <div><dt>排除</dt><dd>{{ plan.counts.excluded }}</dd></div>
        <div><dt>保留</dt><dd>{{ plan.counts.kept }}</dd></div>
        <div><dt>冲突</dt><dd>{{ plan.counts.conflicts }}</dd></div>
        <div>
          <dt>参考模态</dt>
          <dd>
            {{ modalityLabel(plan.reference_modality || 'rgb') }}
            <span v-if="plan.secondary_modality" class="hint"> · 辅模态 {{ modalityLabel(plan.secondary_modality) }}</span>
          </dd>
        </div>
      </dl>
      <p v-if="plan.reference_modality === 'ir'" class="hint">
        参考模态是红外：images/ 存红外图，另一模态图与辅助标注另存；训练只有勾选“仅红外训练”时才会使用该布局。
      </p>
      <ul v-if="plan.conflicts.length" class="business-items">
        <li v-for="(conflict, index) in plan.conflicts" :key="index">
          <span class="mono">{{ conflict.record_id }}</span>
          <span class="form-error">{{ conflict.code }}：{{ conflict.message }}</span>
          <span v-if="conflict.matches?.length" class="inline-form">
            <button
              v-for="match in conflict.matches"
              :key="match.sample_id"
              class="ghost small"
              type="button"
              @click="chooseTarget(conflict.record_id, match.sample_id)"
            >
              选 {{ match.split }}/{{ match.stem }}
            </button>
          </span>
        </li>
      </ul>
      <p class="hint">{{ plan.note }}</p>
    </div>

    <p class="hint">
      说明：回原修订会复制原图与配图到新版本目录，只替换 label；另建数据集会复制选中样本并按身份分组划分，
      同一张图不会同时出现在 train 与 val。
    </p>
  </section>
</template>
