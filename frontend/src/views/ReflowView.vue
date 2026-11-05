<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import * as api from '@/api/business'
import { errorMessage } from '@/api/http'
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
const error = ref('')
const notice = ref('')
const busy = ref('')

const pending = computed(() => feedback.value.filter((item) => item.status === 'pending'))
const selectedIds = computed(() => Object.keys(chosen.value).filter((key) => chosen.value[key]))

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
const planStale = computed(() =>
  isPlanStale(
    previewedState.value,
    formSnapshot(
      form.value,
      selectedIds.value,
      actions.value,
      splits.value,
      targetSamples.value,
    ),
  ),
)

onMounted(async () => {
  await business.loadDatasets(projectId.value)
  if (datasets.value.length) datasetId.value = datasets.value[0].id
  await loadFeedback()
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
  const submitted = formSnapshot(
    form.value,
    selectedIds.value,
    actions.value,
    splits.value,
    targetSamples.value,
  )
  try {
    const data = await api.saveReflowDraft(projectId.value, {
      draft_id: draftId.value || undefined,
      name: name.value || `回流草稿 ${new Date().toLocaleDateString()}`,
      mode: mode.value,
      dataset_id: mode.value === 'revision' ? datasetId.value || null : null,
      selections: selections(),
      strategy: strategy.value,
      ratios: strategy.value === 'ratio' ? ratios.value : undefined,
      seed: seed.value,
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
    error.value = errorMessage(failure)
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
  if (!datasetId.value) return
  busy.value = 'report'
  try {
    report.value = await api.datasetReport(projectId.value, datasetId.value)
  } catch (failure) {
    error.value = errorMessage(failure)
  } finally {
    busy.value = ''
  }
}

function chooseTarget(recordId: string, sampleId: string): void {
  targetSamples.value = { ...targetSamples.value, [recordId]: sampleId }
  void preview()
}

function openDataset(): void {
  void router.push({ name: 'datasets', params: { projectId: projectId.value } })
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
      <p class="card-head">回流方式</p>
      <div class="form-grid">
        <label class="field">
          <span>模式</span>
          <select v-model="mode">
            <option value="revision">回原修订（同图替换 label，不增样本）</option>
            <option value="new">另建数据集（只取选中样本并重新划分）</option>
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
        <label class="field">
          <span>划分方式</span>
          <select v-model="strategy">
            <option value="even">1/1/1（按身份分组轮流）</option>
            <option value="ratio">按比例 + 固定种子</option>
            <option value="manual">逐样本指定（每条都要选划分）</option>
          </select>
        </label>
        <label class="field">
          <span>随机种子</span>
          <input v-model.number="seed" type="number" min="0" step="1" />
        </label>
        <label v-if="strategy === 'ratio'" class="field">
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
        <button class="ghost small" type="button" :disabled="!datasetId" @click="checkIdentity">
          检查重复 / 跨划分泄漏
        </button>
        <span v-if="report" class="hint">
          重复组 {{ report.duplicate_groups.length }} · 泄漏 {{ report.cross_split_leakage.length }} ·
          标注冲突 {{ report.annotation_conflicts.length }}
        </span>
      </div>
    </div>

    <div class="card">
      <p class="card-head">待整理反馈（{{ pending.length }}）</p>
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
            <td>{{ item.review?.outcome === 'no_defect' ? '确认无缺陷' : `确认有缺陷（${item.review?.box_count ?? 0} 框）` }}</td>
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
        还没有待整理反馈。先到「识别记录」或「人工复核」确认记录，再点「加入反馈」。
      </p>

      <div class="form-actions">
        <button class="primary small" type="button" :disabled="!selectedIds.length || busy !== ''" @click="preview">
          {{ busy === 'preview' ? '计算中…' : `保存草稿并预览（${selectedIds.length} 条）` }}
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
    </div>

    <div v-if="plan" class="card">
      <p class="card-head">
        预览结果
        <span v-if="planStale" class="form-error">（已过期：下面的数字对应改动前的表单）</span>
      </p>
      <div class="kv">
        <span>替换</span><span>{{ plan.counts.replace }}</span>
        <span>追加</span><span>{{ plan.counts.append }}</span>
        <span>排除</span><span>{{ plan.counts.excluded }}</span>
        <span>保留</span><span>{{ plan.counts.kept }}</span>
        <span>冲突</span><span>{{ plan.counts.conflicts }}</span>
      </div>
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
