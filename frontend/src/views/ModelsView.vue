<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import * as api from '@/api/business'
import { errorCode, errorMessage } from '@/api/http'
import { formatBytes } from '@/lib/business'
import { useBusinessStore } from '@/stores/business'
import { useSyncStore } from '@/stores/sync'
import TrainingPanel from '@/components/TrainingPanel.vue'
import type { ModelSummary } from '@/types'

/**
 * 模型页：导入 PT、显式确认类别映射、发布到**本项目**。
 *
 * 列表紧凑条目：名称 / 来源 / 模态 / 发布状态 / 真实指标；类别表、文件大小、
 * 父模型、备注等细节收进展开区。「导入权重」「开始训练」是页头稳定入口，
 * 打开才显示表单，取消/返回不改任何真实模型。
 * 数据集页「用于训练」通过 ?train_dataset=<id> 预选到训练表单，只填好不自动开跑。
 * 类别名不靠翻译猜：导入时如果权重自带类别与数据集类别对不上，后端返回
 * ``NEED_CLASS_MAPPING`` 并给出两份清单，这里把两份清单摆出来让用户填映射。
 */

const route = useRoute()
const router = useRouter()
const business = useBusinessStore()
const sync = useSyncStore()

const projectId = computed(() => String(route.params.projectId ?? ''))
const models = computed(() => business.models[projectId.value] ?? [])
const datasets = computed(() => business.datasets[projectId.value] ?? [])

const showImport = ref(false)
const showTraining = ref(false)
const presetDatasetId = ref('')

const form = ref({
  name: '',
  family: 'yolomm' as 'yolo' | 'yolomm',
  secondary: 'ir' as '' | 'ir' | 'edge' | 'depth',
  datasetId: '',
  relPath: 'weights/best.pt',
  assetId: '',
})
const weights = ref<{ id: string; filename: string; size: number }[]>([])
const mapping = ref<{
  native: { index: number; name: string }[]
  target: string[]
  values: Record<string, string>
} | null>(null)
const localError = ref('')
const uploading = ref(false)
const deletedModels = ref<ModelSummary[]>([])
const showTrash = ref(false)
const trainingLock = ref<{ held: boolean; run_id: string }>({ held: false, run_id: '' })
const notesDraft = ref<Record<string, string>>({})
const expanded = ref<Record<string, boolean>>({})

onMounted(async () => {
  await business.loadDatasets(projectId.value)
  await business.loadModels(projectId.value)
  await loadWeights()
  await loadDeleted()
  // 数据集页「用于训练」：带真实 dataset ID 预选到训练表单（只打开并填好，不自动开始）
  const trainDataset = String(route.query.train_dataset ?? '')
  if (trainDataset && datasets.value.some((item) => item.id === trainDataset)) {
    presetDatasetId.value = trainDataset
    showTraining.value = true
  }
})

function toggleExpanded(modelId: string): void {
  expanded.value[modelId] = !expanded.value[modelId]
}

// 助手在后台导入/发布/删除模型后，当前页立即反映；只重读列表，不清导入表单与备注草稿。
watch(
  [() => sync.revision.models, () => sync.revision.datasets],
  () => {
    void business.loadModels(projectId.value)
    void business.loadDatasets(projectId.value)
    void loadDeleted()
  },
)

async function loadDeleted(): Promise<void> {
  try {
    const data = await api.listModels(projectId.value, true)
    deletedModels.value = data.models.filter((model) => Boolean(model.deleted_at))
  } catch {
    deletedModels.value = []
  }
}

async function runModel(action: 'unpublish' | 'delete' | 'restore', model: ModelSummary): Promise<void> {
  // 页面级动作（撤销发布/删除/恢复）的错误必须出现在**页级唯一横幅**：
  // 写进 localError 的话，导入表单默认收起时用户完全看不到（root UI-04A 反例）。
  localError.value = ''
  business.error = ''
  if (action === 'delete' && !window.confirm(`把「${model.name}」放入回收站？软删只标记，不删除权重文件。`)) return
  try {
    if (action === 'unpublish') await api.unpublishModel(projectId.value, model.id)
    else if (action === 'delete') await api.deleteModel(projectId.value, model.id)
    else await api.restoreModel(projectId.value, model.id)
    await business.loadModels(projectId.value)
    await loadDeleted()
  } catch (failure) {
    // 失败时不刷新模型列表：状态只来自后端，不做乐观更改。
    business.error = `「${model.name}」${action === 'unpublish' ? '撤销发布' : action === 'delete' ? '移入回收站' : '恢复'}失败：${errorMessage(failure)}`
  }
}

async function saveNotes(model: ModelSummary): Promise<void> {
  const notes = notesDraft.value[model.id]
  if (notes === undefined) return
  localError.value = ''
  business.error = ''
  try {
    await api.patchModel(projectId.value, model.id, { notes })
    delete notesDraft.value[model.id]
    await business.loadModels(projectId.value)
  } catch (failure) {
    // 备注保存失败同样走页级横幅（可能发生在任何折叠状态下）。
    business.error = `「${model.name}」备注保存失败：${errorMessage(failure)}（草稿还在输入框里，没有丢）`
  }
}

async function onTrainingChanged(): Promise<void> {
  await business.loadModels(projectId.value)
  try {
    const data = await api.listTraining(projectId.value, 1)
    trainingLock.value = { held: data.lock.held, run_id: data.lock.run_id }
  } catch {
    trainingLock.value = { held: false, run_id: '' }
  }
}

function metricText(model: ModelSummary): string {
  const values = model.metrics?.values
  if (!values) return '无验证指标'
  const parts: string[] = []
  if (values.map50 != null) parts.push(`mAP50 ${values.map50}`)
  if (values.map50_95 != null) parts.push(`mAP50-95 ${values.map50_95}`)
  if (values.precision != null) parts.push(`P ${values.precision}`)
  if (values.recall != null) parts.push(`R ${values.recall}`)
  return parts.join(' · ') || '无验证指标'
}

function statusText(model: ModelSummary): string {
  switch (model.status) {
    case 'published':
      return '已发布'
    case 'superseded':
      return '已被新版本取代'
    case 'archived':
      return '已归档'
    case 'failed':
      return '失败'
    default:
      return '未发布'
  }
}

async function loadWeights(): Promise<void> {
  try {
    const data = await api.listAssets(projectId.value, { kind: 'weight', limit: 50 })
    weights.value = data.assets.map((asset) => ({
      id: asset.id,
      filename: asset.filename,
      size: asset.size,
    }))
  } catch {
    weights.value = []
  }
}

async function onWeight(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  uploading.value = true
  localError.value = ''
  try {
    const asset = await api.uploadAsset(projectId.value, file)
    if (asset.kind !== 'weight') {
      localError.value = `这个文件按内容被识别为 ${asset.kind}，不是模型权重`
    } else {
      form.value.assetId = asset.id
      await loadWeights()
    }
  } catch (failure) {
    localError.value = errorMessage(failure)
  } finally {
    uploading.value = false
    input.value = ''
  }
}

function closeImport(): void {
  showImport.value = false
  mapping.value = null
  localError.value = ''
}

async function submit(): Promise<void> {
  localError.value = ''
  mapping.value = null
  const payload = {
    name: form.value.name.trim(),
    family: form.value.family,
    secondary_modality: form.value.family === 'yolomm' ? form.value.secondary || 'ir' : null,
    dataset_id: form.value.datasetId || null,
    rel_path: form.value.assetId ? undefined : form.value.relPath || undefined,
    asset_id: form.value.assetId || undefined,
  }
  try {
    const data = await api.importModel(projectId.value, payload)
    business.notice = `模型「${data.model.name}」已导入；确认类别映射后发布`
    await business.loadModels(projectId.value)
  } catch (failure) {
    if (errorCode(failure) === 'NEED_CLASS_MAPPING') {
      const raw = (failure as { details?: unknown }).details as
        | { native_classes?: { index: number; name: string }[]; dataset_classes?: string[] }
        | undefined
      const target = raw?.dataset_classes ?? []
      const native = raw?.native_classes ?? []
      mapping.value = {
        native,
        target,
        values: Object.fromEntries(native.map((item) => [String(item.index), ''])),
      }
      localError.value = '权重类别与数据集类别名称不一致：请逐项确认映射（不会自动猜）'
      return
    }
    localError.value = errorMessage(failure)
  }
}

async function confirmMapping(): Promise<void> {
  if (!mapping.value) return
  const classMap: Record<string, number> = {}
  for (const item of mapping.value.native) {
    const chosen = mapping.value.values[String(item.index)]
    if (chosen === '' || chosen === undefined) {
      localError.value = `类别 ${item.name} 还没有选择对应项`
      return
    }
    classMap[String(item.index)] = Number(chosen)
  }
  try {
    await api.importModel(projectId.value, {
      name: form.value.name.trim(),
      family: form.value.family,
      secondary_modality: form.value.family === 'yolomm' ? form.value.secondary || 'ir' : null,
      dataset_id: form.value.datasetId || null,
      rel_path: form.value.assetId ? undefined : form.value.relPath || undefined,
      asset_id: form.value.assetId || undefined,
      class_map: classMap,
    })
    mapping.value = null
    localError.value = ''
    business.notice = '类别映射已保存，模型已导入'
    await business.loadModels(projectId.value)
  } catch (failure) {
    localError.value = errorMessage(failure)
  }
}

async function publish(model: ModelSummary): Promise<void> {
  const ok = await business.publish(projectId.value, model.id)
  if (ok) await business.loadModels(projectId.value)
}

function useForInspect(): void {
  const published = models.value.find((model) => model.status === 'published')
  if (!published) {
    business.error = '先发布一个模型，巡检才能选它'
    return
  }
  // 真正的巡检入口：带模型 ID 预选到图片巡检页（只选中，不自动跑）。
  void router.push({
    name: 'inspect',
    params: { projectId: projectId.value },
    query: { model: published.id },
  })
}
</script>

<template>
  <section class="page">
    <header class="page-header">
      <div>
        <h1>模型</h1>
        <p class="subtitle">
          本项目共 {{ models.length }} 个模型，已发布
          {{ models.filter((item) => item.status === 'published').length }} 个
          <span class="hint">· 发布只作用于当前项目</span>
        </p>
      </div>
      <div class="header-actions">
        <button class="ghost small" type="button" @click="business.loadModels(projectId)">刷新</button>
        <button class="ghost small" type="button" @click="useForInspect">去巡检</button>
        <button
          class="ghost small"
          type="button"
          :aria-expanded="showTraining"
          @click="showTraining = !showTraining"
        >
          开始训练
        </button>
        <button
          class="primary small"
          type="button"
          :aria-expanded="showImport"
          @click="showImport = !showImport"
        >
          导入权重
        </button>
      </div>
    </header>

    <p v-if="business.error" class="banner banner-error" role="alert">{{ business.error }}</p>
    <p v-else-if="business.notice" class="banner banner-ok" role="status">{{ business.notice }}</p>

    <!-- 导入：按需展开 -->
    <div v-if="showImport" class="card">
      <p class="card-head">
        导入模型
        <button class="link" type="button" @click="closeImport">收起</button>
      </p>
      <div class="form-grid">
        <label class="field">
          <span>名称</span>
          <input v-model="form.name" placeholder="例如 双模态热斑 v1" />
        </label>
        <label class="field">
          <span>家族</span>
          <select v-model="form.family">
            <option value="yolomm">yolomm（双模态）</option>
            <option value="yolo">yolo（单模态）</option>
          </select>
        </label>
        <label v-if="form.family === 'yolomm'" class="field">
          <span>第二模态</span>
          <select v-model="form.secondary">
            <option value="ir">红外</option>
            <option value="edge">边缘</option>
            <option value="depth">深度</option>
          </select>
        </label>
        <label class="field">
          <span>绑定数据集（用于类别对齐）</span>
          <select v-model="form.datasetId">
            <option value="">不绑定</option>
            <option v-for="dataset in datasets" :key="dataset.id" :value="dataset.id">
              {{ dataset.name }}（{{ dataset.classes.join('/') || '无类别表' }}）
            </option>
          </select>
        </label>
        <label class="field">
          <span>权重路径（项目内相对路径）</span>
          <input v-model="form.relPath" placeholder="weights/best.pt" :disabled="Boolean(form.assetId)" />
        </label>
        <label class="field">
          <span>或使用已上传的权重</span>
          <select v-model="form.assetId">
            <option value="">不使用</option>
            <option v-for="asset in weights" :key="asset.id" :value="asset.id">
              {{ asset.filename }}（{{ formatBytes(asset.size) }}）
            </option>
          </select>
        </label>
      </div>
      <div class="inline-form">
        <label class="button ghost small">
          {{ uploading ? '上传中…' : '上传 .pt' }}
          <input type="file" accept=".pt" hidden :disabled="uploading" @change="onWeight" />
        </label>
      </div>
      <p v-if="localError" class="form-error">{{ localError }}</p>

      <div v-if="mapping" class="card">
        <p class="card-head">类别映射（必须逐项确认）</p>
        <table class="table">
          <thead>
            <tr><th>权重里的类别</th><th>对应数据集类别</th></tr>
          </thead>
          <tbody>
            <tr v-for="item in mapping.native" :key="item.index">
              <td>{{ item.index }}: {{ item.name }}</td>
              <td>
                <select v-model="mapping.values[String(item.index)]">
                  <option value="">请选择</option>
                  <option v-for="(name, index) in mapping.target" :key="name" :value="String(index)">
                    {{ index }}: {{ name }}
                  </option>
                </select>
              </td>
            </tr>
          </tbody>
        </table>
        <div class="form-actions">
          <button class="primary small" type="button" @click="confirmMapping">保存映射并导入</button>
        </div>
      </div>

      <div class="form-actions">
        <button
          class="primary small"
          type="button"
          :disabled="!form.name.trim() || business.busy === 'model'"
          @click="submit"
        >
          导入
        </button>
      </div>
    </div>

    <!-- 训练：按需展开；数据集页「用于训练」会带着真实 dataset ID 打开并预选 -->
    <TrainingPanel
      v-if="showTraining"
      :project-id="projectId"
      :datasets="datasets"
      :models="models"
      :busy-lock="trainingLock"
      :preset-dataset-id="presetDatasetId"
      @changed="onTrainingChanged"
    />

    <div class="cards">
      <article v-for="model in models" :key="model.id" class="card model-row">
        <div class="model-line">
          <strong class="model-name" :title="model.id">{{ model.name }}</strong>
          <span class="tag" :class="model.status === 'published' ? 'tag-on' : 'tag-off'">
            {{ statusText(model) }}
          </span>
          <span v-if="model.source === 'trained'" class="tag tag-off">训练产出</span>
          <span class="hint">{{ model.model_mode === 'multimodal' ? `RGB + ${model.secondary_modality ?? '?'}` : '仅 RGB' }}</span>
          <span class="hint">{{ model.source === 'trained' ? '本机训练' : '导入权重' }}</span>
          <span class="hint metrics">{{ metricText(model) }}</span>
          <span class="spacer"></span>
          <button
            v-if="model.status !== 'published'"
            class="primary small"
            type="button"
            :disabled="business.busy === `publish:${model.id}`"
            @click="publish(model)"
          >
            发布到本项目
          </button>
          <button
            v-else
            class="ghost small"
            type="button"
            @click="runModel('unpublish', model)"
          >
            撤销发布
          </button>
          <button class="ghost small" type="button" :aria-expanded="Boolean(expanded[model.id])" @click="toggleExpanded(model.id)">
            {{ expanded[model.id] ? '收起' : '详情' }}
          </button>
          <button
            v-if="model.status !== 'published'"
            class="ghost small danger"
            type="button"
            @click="runModel('delete', model)"
          >
            放入回收站
          </button>
        </div>
        <div v-if="expanded[model.id]" class="model-detail">
          <dl class="kv">
            <div><dt>家族</dt><dd class="mono">{{ model.family }}</dd></div>
            <div><dt>权重大小</dt><dd class="mono">{{ formatBytes(model.weight_size) }}</dd></div>
            <div><dt>类别</dt><dd>{{ model.classes.map((item) => `${item.index}:${item.name}`).join('、') || '未知' }}</dd></div>
            <div><dt>数据集成</dt><dd class="mono">{{ model.dataset_id ?? '未绑定' }}</dd></div>
            <div><dt>来源</dt><dd>
              {{ model.source === 'trained' ? '本机训练' : '导入权重' }}
              <template v-if="model.parent_model_id"> · 父模型 {{ model.parent_model_id }}</template>
            </dd></div>
            <div><dt>指标</dt><dd class="mono">
              {{ metricText(model) }}
              <template v-if="model.metrics?.source">
                （{{ model.metrics.source }}，split={{ model.metrics.split ?? '?' }}）
              </template>
            </dd></div>
          </dl>
          <p v-if="model.metrics?.conditions" class="hint mono">
            验证条件：{{ Object.entries(model.metrics.conditions).map(([key, value]) => `${key}=${value}`).join(' · ') }}
          </p>
          <p v-else class="hint">还没有验证集指标；训练完成后会在这里显示真实数值。</p>
          <div class="inline-form">
            <input
              v-model="notesDraft[model.id]"
              :placeholder="model.notes || '备注（仅本项目可见）'"
              maxlength="200"
            />
            <button class="ghost small" type="button" @click="saveNotes(model)">保存备注</button>
          </div>
        </div>
      </article>
      <p v-if="!models.length" class="empty-hint">
        还没有模型。点右上角「导入权重」上传一个 .pt，或填写项目目录里已有的权重路径。
      </p>
    </div>

    <div class="card">
      <p class="card-head">
        回收站中的模型（{{ deletedModels.length }}）
        <button class="ghost small" type="button" @click="showTrash = !showTrash">
          {{ showTrash ? '收起' : '展开' }}
        </button>
      </p>
      <p class="hint">软删只标记，不删除权重文件；恢复后原来的发布状态不会自动回来。</p>
      <ul v-if="showTrash" class="trash-list">
        <li v-for="model in deletedModels" :key="model.id">
          <span>{{ model.name }}</span>
          <span class="mono hint">{{ model.id }}</span>
          <button class="ghost small" type="button" @click="runModel('restore', model)">恢复</button>
        </li>
      </ul>
      <p v-if="showTrash && !deletedModels.length" class="empty-hint">回收站是空的。</p>
    </div>
  </section>
</template>

<style scoped>
.model-row {
  padding: 10px 14px;
}

.model-line {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.model-name {
  font-weight: 600;
  overflow-wrap: anywhere;
}

.model-line .metrics {
  font-variant-numeric: tabular-nums;
}

.model-line .spacer {
  flex: 1;
}

.model-detail {
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px dashed var(--border);
}
</style>
