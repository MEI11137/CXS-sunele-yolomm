<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as api from '@/api/business'
import { errorMessage } from '@/api/http'
import type {
  DatasetSummary,
  ModelSummary,
  TrainingComparison,
  TrainingDeviceCapabilities,
  TrainingPlan,
  TrainingRun,
} from '@/types'

/**
 * 训练面板：与 Agent 工具调用**同一个服务**（后端 training 服务）。
 *
 * 这里不做“乐观成功”：状态、指标、日志全部来自服务端真实记录。
 * 训练槽是被占用的（TRAINING_BUSY）就如实显示占用者，不排队、不假装开始。
 * 设备按部署机真实探测结果展示（cpu 恒可用；GPU 不可用时给出真实原因，
 * 但不从界面消失——GPU 是产品能力，不因开发机没有 CUDA 而写死 CPU）。
 */

const props = defineProps<{
  projectId: string
  datasets: DatasetSummary[]
  models: ModelSummary[]
  /** 单机训练槽只允许一次训练：占用时页面按钮禁用并说明原因。 */
  busyLock?: { held: boolean; run_id: string }
  /** 数据集页「用于训练」带来的预选：只填好表单，不自动开始。 */
  presetDatasetId?: string
}>()

const emit = defineEmits<{ (event: 'changed'): void }>()

const runs = ref<TrainingRun[]>([])
const lock = ref<{ held: boolean; run_id: string }>({ held: false, run_id: '' })
const plan = ref<TrainingPlan | null>(null)
const comparison = ref<TrainingComparison | null>(null)
const logText = ref('')
const activeRunId = ref('')
const error = ref('')
const notice = ref('')
const working = ref(false)
const showAdvanced = ref(false)
const deviceCaps = ref<TrainingDeviceCapabilities | null>(null)
let timer: number | null = null

const form = ref({
  datasetId: '',
  parentModelId: '',
  name: '',
  epochs: 10,
  imgsz: 640,
  batch: 4,
  lr0: 0.01,
  seed: 0,
  device: 'cpu',
  singleIr: false,
})

const trainableDatasets = computed(() => props.datasets)
const parentCandidates = computed(() =>
  props.models.filter((model) => model.status !== 'superseded' && !model.deleted_at),
)
const canStart = computed(
  () => Boolean(form.value.datasetId) && !working.value && !lock.value.held,
)
const activeRun = computed(() => runs.value.find((run) => run.id === activeRunId.value) ?? null)

/** 设备选项来自真实探测：GPU 不可用时保留选项并标注原因，交给后端在启动时把关。 */
const deviceOptions = computed(() => {
  const caps = deviceCaps.value
  const options: { value: string; label: string; disabled: boolean }[] = [
    { value: 'cpu', label: 'CPU', disabled: false },
  ]
  if (!caps) return options
  if (caps.cuda_available) {
    options.push({ value: 'cuda', label: 'GPU（自动选择）', disabled: false })
    for (let index = 0; index < caps.cuda_count; index += 1) {
      options.push({ value: `cuda:${index}`, label: `GPU ${index}`, disabled: false })
    }
  } else {
    options.push({
      value: 'cuda',
      label: `GPU（当前环境不可用：${caps.detail || '探测失败'}）`,
      disabled: false,
    })
  }
  return options
})

const selectedDataset = computed(
  () => props.datasets.find((item) => item.id === form.value.datasetId) ?? null,
)

/** 只有“IR 参考 + 另有辅助模态”的数据集才需要“仅红外训练”这个显式选择。 */
const singleIrEligible = computed(() => {
  const dataset = selectedDataset.value
  if (!dataset) return false
  const reference = String(dataset.reference_modality || dataset.primary_modality || 'rgb').toLowerCase()
  return reference === 'ir' && dataset.modalities.length > 1
})

watch(
  () => form.value.datasetId,
  () => {
    if (!singleIrEligible.value) form.value.singleIr = false
    plan.value = null
  },
)

function params(): Record<string, number | string | boolean> {
  return {
    epochs: Number(form.value.epochs),
    imgsz: Number(form.value.imgsz),
    batch: Number(form.value.batch),
    lr0: Number(form.value.lr0),
    seed: Number(form.value.seed),
    device: form.value.device,
    single_ir: Boolean(form.value.singleIr),
  }
}

async function refresh(): Promise<void> {
  try {
    const data = await api.listTraining(props.projectId)
    runs.value = data.runs
    lock.value = { held: data.lock.held, run_id: data.lock.run_id }
  } catch (failure) {
    error.value = errorMessage(failure)
  }
}

async function preview(): Promise<void> {
  if (!form.value.datasetId) {
    error.value = '先选一个数据集'
    return
  }
  error.value = ''
  try {
    plan.value = await api.planTraining(props.projectId, {
      dataset_id: form.value.datasetId,
      parent_model_id: form.value.parentModelId || null,
      params: params(),
      name: form.value.name.trim() || undefined,
    })
  } catch (failure) {
    plan.value = null
    error.value = errorMessage(failure)
  }
}

async function start(): Promise<void> {
  if (!form.value.datasetId) return
  error.value = ''
  notice.value = ''
  working.value = true
  try {
    const data = await api.startTraining(props.projectId, {
      dataset_id: form.value.datasetId,
      parent_model_id: form.value.parentModelId || null,
      params: params(),
      name: form.value.name.trim() || undefined,
      request_key: `ui-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`,
    })
    activeRunId.value = data.run.id
    notice.value = data.created ? '训练已开始，进度与日志在下面实时读取' : '这次训练已经存在，没有重复启动'
    await refresh()
    await showLog(data.run.id)
    emit('changed')
    ensurePolling()
  } catch (failure) {
    error.value = errorMessage(failure)
  } finally {
    working.value = false
  }
}

async function cancel(run: TrainingRun): Promise<void> {
  error.value = ''
  try {
    const data = await api.cancelTraining(props.projectId, run.id)
    notice.value = data.run.status === 'cancelled' ? '训练已取消，进程已确认结束' : `训练状态：${data.run.status}`
    await refresh()
    emit('changed')
  } catch (failure) {
    // 终止失败时服务端保留训练槽，这里如实显示，不伪装成取消成功。
    error.value = errorMessage(failure)
  }
}

async function showLog(runId: string): Promise<void> {
  activeRunId.value = runId
  comparison.value = null
  try {
    const data = await api.trainingLog(props.projectId, runId)
    logText.value = data.text
  } catch (failure) {
    logText.value = ''
    error.value = errorMessage(failure)
  }
}

async function compare(run: TrainingRun): Promise<void> {
  error.value = ''
  try {
    comparison.value = await api.compareTraining(props.projectId, run.id)
  } catch (failure) {
    comparison.value = null
    error.value = errorMessage(failure)
  }
}

function ensurePolling(): void {
  if (timer !== null) return
  timer = window.setInterval(async () => {
    const running = runs.value.some((run) => run.status === 'queued' || run.status === 'running')
    if (!running) {
      stopPolling()
      return
    }
    await refresh()
    if (activeRunId.value) await showLog(activeRunId.value)
  }, 1500)
}

function stopPolling(): void {
  if (timer !== null) {
    window.clearInterval(timer)
    timer = null
  }
}

watch(
  () => props.projectId,
  async () => {
    stopPolling()
    await refresh()
    if (lock.value.held) ensurePolling()
  },
)

onMounted(async () => {
  if (props.presetDatasetId) form.value.datasetId = props.presetDatasetId
  try {
    deviceCaps.value = await api.trainingDevice(props.projectId)
  } catch {
    deviceCaps.value = null
  }
  await refresh()
  if (runs.value.some((run) => run.status === 'queued' || run.status === 'running')) ensurePolling()
})

onBeforeUnmount(stopPolling)

function statusLabel(status: string): string {
  return (
    {
      queued: '排队中',
      running: '训练中',
      succeeded: '已完成',
      failed: '失败',
      cancelled: '已取消',
      interrupted: '结果未知（服务重启）',
    }[status] ?? status
  )
}

function metricText(values?: Record<string, number | null>): string {
  if (!values) return '—'
  const parts: string[] = []
  if (values.map50 != null) parts.push(`mAP50 ${values.map50}`)
  if (values.map50_95 != null) parts.push(`mAP50-95 ${values.map50_95}`)
  if (values.precision != null) parts.push(`P ${values.precision}`)
  if (values.recall != null) parts.push(`R ${values.recall}`)
  return parts.join(' · ') || '—'
}
</script>

<template>
  <div class="card">
    <p class="card-head">训练</p>
    <p class="hint">
      同一时间只跑一个训练任务。设备按部署机真实探测：CPU 恒可用；选 GPU 但环境不支持时，
      校验/启动会给出真实原因。参数先校验再启动；训练在独立子进程里跑，日志与权重落在
      <span class="mono">training/&lt;run_id&gt;/</span>。
    </p>
    <p v-if="lock.held" class="banner banner-warn" role="status">
      训练槽被占用：{{ lock.run_id }}。等它结束或先取消，才能开始新的训练。
    </p>

    <div class="form-grid">
      <label class="field">
        <span>数据集</span>
        <select v-model="form.datasetId">
          <option value="">请选择</option>
          <option v-for="dataset in trainableDatasets" :key="dataset.id" :value="dataset.id">
            {{ dataset.name }}（{{ dataset.sample_count }} 样本）
          </option>
        </select>
      </label>
      <label class="field">
        <span>基础权重（增量训练）</span>
        <select v-model="form.parentModelId">
          <option value="">从零开始（首次训练）</option>
          <option v-for="model in parentCandidates" :key="model.id" :value="model.id">
            {{ model.name }}（{{ model.source === 'trained' ? '训练产出' : '导入' }}）
          </option>
        </select>
      </label>
      <label class="field">
        <span>模型名称</span>
        <input v-model="form.name" placeholder="留空自动取名" />
      </label>
      <label class="field">
        <span>训练设备</span>
        <select v-model="form.device">
          <option v-for="option in deviceOptions" :key="option.value" :value="option.value">
            {{ option.label }}
          </option>
        </select>
      </label>
      <label class="field">
        <span>轮数 epochs</span>
        <input v-model.number="form.epochs" type="number" min="1" max="500" />
      </label>
      <label class="field">
        <span>imgsz</span>
        <input v-model.number="form.imgsz" type="number" min="64" max="2048" step="32" />
      </label>
      <label class="field">
        <span>batch</span>
        <input v-model.number="form.batch" type="number" min="1" max="64" />
        <label v-if="singleIrEligible" class="field field-check">
          <span>仅红外训练</span>
          <span class="inline-form">
            <input v-model="form.singleIr" type="checkbox" />
            <span class="hint">
              该数据集以红外为参考、另有 RGB 辅助资料。勾选后只用红外图训练与推理，
              RGB 图与辅助标注保留但不参与本轮训练，模型不能当作双模态融合模型。
            </span>
          </span>
        </label>
      </label>
    </div>

    <p class="hint">
      <button class="link" type="button" :aria-expanded="showAdvanced" @click="showAdvanced = !showAdvanced">
        {{ showAdvanced ? '收起高级参数' : '高级参数（学习率、随机种子）' }}
      </button>
    </p>
    <div v-if="showAdvanced" class="form-grid">
      <label class="field">
        <span>lr0（初始学习率）</span>
        <input v-model.number="form.lr0" type="number" min="0.00001" max="0.5" step="0.001" />
      </label>
      <label class="field">
        <span>seed</span>
        <input v-model.number="form.seed" type="number" min="0" />
      </label>
    </div>

    <div class="form-actions">
      <button class="ghost small" type="button" :disabled="working" @click="preview">校验参数</button>
      <button class="primary small" type="button" :disabled="!canStart" @click="start">
        {{ lock.held ? '训练槽被占用' : working ? '提交中…' : '开始训练' }}
      </button>
    </div>

    <div v-if="plan" class="plan">
      <p class="card-head">训练计划（尚未启动）</p>
      <dl class="kv">
        <div><dt>数据集</dt><dd>{{ plan.dataset.name }}（{{ plan.dataset.samples }} 样本）</dd></div>
        <div><dt>基础权重</dt><dd class="mono">{{ plan.base_weight_rel || '从零开始（不下载预训练权重）' }}</dd></div>
        <div v-if="plan.single_ir"><dt>训练模式</dt><dd>仅红外（IR 图像作主输入）</dd></div>
        <div v-else-if="plan.primary_modality === 'ir'"><dt>训练模式</dt><dd>双模态融合（参考模态 IR）</dd></div>
        <div><dt>指标来源</dt><dd>验证集 split=val（训练结束时评估，不是独立测试集）</dd></div>
        <div><dt>条件</dt><dd class="mono">
          epochs={{ plan.conditions.epochs }} · imgsz={{ plan.conditions.imgsz }} ·
          batch={{ plan.conditions.batch }} · seed={{ plan.conditions.seed }} ·
          device={{ plan.conditions.device }}
        </dd></div>
      </dl>
      <ul v-if="plan.warnings.length" class="warning-list">
        <li v-for="warning in plan.warnings" :key="warning">{{ warning }}</li>
      </ul>
    </div>

    <p v-if="error" class="form-error" role="alert">{{ error }}</p>
    <p v-else-if="notice" class="banner banner-ok" role="status">{{ notice }}</p>

    <table v-if="runs.length" class="table">
      <thead>
        <tr>
          <th>训练</th><th>状态</th><th>指标</th><th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="run in runs" :key="run.id">
          <td>
            <div>{{ run.name }}</div>
            <div class="hint mono">{{ run.id }}</div>
          </td>
          <td>
            <span class="tag" :class="run.status === 'succeeded' ? 'tag-on' : 'tag-off'">
              {{ statusLabel(run.status) }}
            </span>
            <div v-if="run.error" class="hint">{{ run.error.code }}：{{ run.error.message }}</div>
          </td>
          <td class="mono">
            {{ metricText(run.metrics?.values) }}
            <div v-if="!run.metrics?.values" class="hint">无验证指标</div>
          </td>
          <td class="row-actions">
            <button class="ghost small" type="button" @click="showLog(run.id)">日志</button>
            <button
              v-if="run.status === 'queued' || run.status === 'running'"
              class="ghost small danger"
              type="button"
              @click="cancel(run)"
            >
              取消
            </button>
            <button class="ghost small" type="button" @click="compare(run)">对比父模型</button>
            <span v-if="run.output_model_id" class="hint">已产出模型（见上方列表）</span>
          </td>
        </tr>
      </tbody>
    </table>
    <p v-else class="empty-hint">还没有训练记录。</p>

    <div v-if="activeRun" class="log-box">
      <p class="card-head">
        日志（{{ activeRun.id }}）
        <span class="hint">真实 train.log 尾部；只在有训练进行时自动刷新</span>
      </p>
      <pre class="log">{{ logText || '暂无日志输出' }}</pre>
    </div>

    <div v-if="comparison" class="card">
      <p class="card-head">父子指标对比</p>
      <dl class="kv">
        <div><dt>父模型</dt><dd>{{ comparison.parent.model?.name ?? '（无）' }}</dd></div>
        <div><dt>父指标</dt><dd class="mono">{{ metricText(comparison.parent.values) }}</dd></div>
        <div><dt>子指标</dt><dd class="mono">{{ metricText(comparison.child.values) }}</dd></div>
        <div><dt>差值</dt><dd class="mono">
          {{ Object.entries(comparison.delta).map(([key, value]) => `${key} ${value > 0 ? '+' : ''}${value}`).join(' · ') || '—' }}
        </dd></div>
        <div><dt>指标来源</dt><dd>{{ comparison.child.source ?? '未知' }}（split={{ comparison.child.split ?? '?' }}）</dd></div>
      </dl>
      <p v-if="comparison.differences.length" class="form-error">
        条件不同：{{ comparison.differences.map((item) => `${item.key} ${item.parent}→${item.child}`).join('；') }}
      </p>
      <p class="hint">{{ comparison.note }}</p>
    </div>
  </div>
</template>

<style scoped>
.log-box {
  margin-top: 12px;
}
.log {
  max-height: 220px;
  overflow: auto;
  padding: 8px 10px;
  border-radius: 8px;
  background: var(--panel-secondary);
  color: inherit;
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-all;
}
.row-actions {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.warning-list {
  margin: 6px 0 0;
  padding-left: 18px;
  font-size: 12px;
  color: var(--text-muted, #6b7280);
}
.plan {
  margin-top: 10px;
}
</style>
