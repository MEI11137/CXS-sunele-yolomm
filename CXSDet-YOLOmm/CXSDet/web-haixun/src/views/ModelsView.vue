<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { getAdapter } from '@/services'
import type { DatasetSummary, ModelSummary } from '@/types'

const route = useRoute()
const router = useRouter()

const models = ref<ModelSummary[]>([])
const datasets = ref<DatasetSummary[]>([])
const loading = ref(false)
const loadError = ref('')
const notice = ref('')
const noticeType = ref<'success' | 'error' | ''>('')

const published = computed(() => models.value.filter((m) => m.is_published))
const unpublished = computed(() => models.value.filter((m) => !m.is_published))

// 状态：0 训练中 / 1 训练完成 / 2 训练失败 / 3 已发布 / 4 训练终止
function statusLabel(status: number): string {
  return status === 0
    ? '训练中'
    : status === 1
      ? '训练完成'
      : status === 2
        ? '训练失败'
        : status === 3
          ? '已发布'
          : status === 4
            ? '训练终止'
            : '未知'
}

function statusClass(m: ModelSummary): string {
  if (m.is_published) return 'tag-success'
  if (m.status === 0) return 'tag-warn'
  if (m.status === 2 || m.status === 4) return 'tag-error'
  return ''
}

/** 可发布的只有"训练完成且尚未发布"的模型 */
function canPublish(m: ModelSummary): boolean {
  return m.status === 1
}

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const [m, d] = await Promise.all([
      getAdapter().listModels({ page: 1, pageSize: 50 }),
      getAdapter().listDatasets({ page: 1, pageSize: 50 }),
    ])
    // 列表接口返回的是旧页面 DTO，这里再取一次完整信息用于来源与发布状态
    models.value = await enrich(m.items.map((x) => x.id))
    datasets.value = d.items
  } catch (e) {
    loadError.value = '读取模型失败：' + (e as Error).message
  } finally {
    loading.value = false
  }
}

/**
 * 列表 DTO 把状态压成了 available/training/unavailable，
 * 这里补一次真实模型详情，拿到 status(0-4)、publish_status 与数据集来源。
 */
async function enrich(ids: string[]): Promise<ModelSummary[]> {
  const results: ModelSummary[] = []
  for (const id of ids) {
    try {
      const detail = await getAdapter().getModelDetail(id)
      if (detail) {
        results.push(detail)
        continue
      }
    } catch {
      /* 单个详情失败不阻断整页 */
    }
    const fallback = models.value.find((m) => m.id === id)
    if (fallback) results.push(fallback)
  }
  return results
}

onMounted(async () => {
  await load()
  // 从数据集页「用于训练」跳过来时，预选该数据集用于权重导入
  const datasetId = route.query.dataset
  if (typeof datasetId === 'string' && datasetId) {
    openImport()
    importForm.value.datasetId = datasetId
  }
})

// ---------------------------------------------------------------- 发布
const publishing = ref<string>('')

async function publish(m: ModelSummary) {
  if (publishing.value) return
  publishing.value = m.id
  notice.value = ''
  try {
    await getAdapter().publishModel(m.id)
    noticeType.value = 'success'
    notice.value = `「${m.name}」已发布；同项目同模态的旧版本会被归档。`
    await load()
  } catch (e) {
    noticeType.value = 'error'
    notice.value = '发布失败：' + (e as Error).message
  } finally {
    publishing.value = ''
  }
}

// ---------------------------------------------------------------- 对比
const compareParent = ref('')
const compareChild = ref('')
const compareResult = ref<Record<string, unknown> | null>(null)
const comparing = ref(false)

async function doCompare() {
  if (!compareParent.value || !compareChild.value) {
    notice.value = '请选择两个模型再对比'
    noticeType.value = 'error'
    return
  }
  comparing.value = true
  notice.value = ''
  try {
    compareResult.value = await getAdapter().compareModels(compareParent.value, compareChild.value)
  } catch (e) {
    noticeType.value = 'error'
    notice.value = '对比失败：' + (e as Error).message
  } finally {
    comparing.value = false
  }
}

function metricRows(): Array<{ label: string; parent: string; child: string; delta: string }> {
  const data = (compareResult.value ?? {}) as Record<string, unknown>
  const parent = (data.parent ?? data.metrics_a ?? {}) as Record<string, unknown>
  const child = (data.child ?? data.metrics_b ?? {}) as Record<string, unknown>
  const keys = ['mAP50', 'mAP5095', 'precision', 'recall']
  return keys.map((k) => {
    const a = Number(parent[k] ?? parent[k.toLowerCase()] ?? NaN)
    const b = Number(child[k] ?? child[k.toLowerCase()] ?? NaN)
    const fmt = (v: number) => (Number.isFinite(v) ? v.toFixed(3) : '—')
    const delta = Number.isFinite(a) && Number.isFinite(b) ? (b - a).toFixed(3) : '—'
    return { label: k, parent: fmt(a), child: fmt(b), delta }
  })
}

// ---------------------------------------------------------------- 权重导入
const showImport = ref(false)
const importing = ref(false)
const importForm = ref({ name: '', description: '', datasetId: '' })
const weightFile = ref<File | null>(null)

function openImport() {
  importForm.value = { name: '', description: '', datasetId: '' }
  weightFile.value = null
  notice.value = ''
  showImport.value = true
}

function onPickWeight(e: Event) {
  const files = (e.target as HTMLInputElement).files
  weightFile.value = files?.[0] ?? null
  if (weightFile.value && !importForm.value.name) {
    importForm.value.name = weightFile.value.name.replace(/\.pt$/i, '')
  }
}

async function doImport() {
  if (!weightFile.value) {
    notice.value = '请选择 .pt 权重文件'
    noticeType.value = 'error'
    return
  }
  if (!importForm.value.datasetId) {
    notice.value = '请选择来源数据集（类别表来自该数据集）'
    noticeType.value = 'error'
    return
  }
  importing.value = true
  notice.value = ''
  try {
    await getAdapter().importModelWeight({
      name: importForm.value.name.trim() || weightFile.value.name.replace(/\.pt$/i, ''),
      description: importForm.value.description,
      datasetId: importForm.value.datasetId,
      weight: weightFile.value,
    })
    noticeType.value = 'success'
    notice.value = '权重导入成功，模型为草稿状态；确认指标后可在列表中发布。'
    showImport.value = false
    await load()
  } catch (e) {
    noticeType.value = 'error'
    notice.value = '导入失败：' + (e as Error).message
  } finally {
    importing.value = false
  }
}

function goInspect(modelId: string) {
  router.push({ path: '/inspection', query: { model: modelId } })
}
</script>

<template>
  <div class="page">
    <div class="page-header">
      <h1 class="page-title">模型管理</h1>
      <div class="header-actions">
        <span class="tag num">{{ published.length }} 个已发布 · {{ unpublished.length }} 个未发布</span>
        <button class="btn btn-primary" @click="openImport">导入 PT 权重</button>
      </div>
    </div>

    <div v-if="notice" class="notice" :class="noticeType === 'error' ? 'notice-error' : 'notice-success'">
      {{ notice }}
    </div>
    <div v-if="loadError" class="notice notice-error">
      {{ loadError }}
      <button class="btn btn-sm" @click="load">重试</button>
    </div>

    <section class="section">
      <h2 class="section-title">已发布（可用于检测）</h2>
      <div v-if="loading" class="empty panel"><div class="empty-desc">正在读取…</div></div>
      <div v-else-if="!published.length" class="empty panel">
        <div class="empty-title">暂无已发布模型</div>
        <div class="empty-desc">训练完成或导入的模型需要人工发布后才能用于检测。</div>
      </div>
      <div class="model-list">
        <div v-for="m in published" :key="m.id" class="panel model-card">
          <div class="model-head">
            <div class="model-name">{{ m.name }} <span class="mono">{{ m.version }}</span></div>
            <div class="head-tags">
              <span class="tag" :class="statusClass(m)">{{ statusLabel(m.status) }}</span>
              <span class="tag tag-accent">{{ m.model_mode === 'multimodal' ? '多模态' : '单模态' }}</span>
            </div>
          </div>
          <div class="model-meta">
            <span class="hint">来源数据集：{{ m.dataset_name || m.dataset_id || '未记录' }}</span>
            <span class="hint num">创建：{{ m.create_time?.slice(0, 10) || '—' }}</span>
          </div>
          <div v-if="m.metrics" class="metrics-grid">
            <div class="metric-item"><span class="metric-label">mAP50</span><span class="metric-value num">{{ m.metrics.mAP50.toFixed(3) }}</span></div>
            <div class="metric-item"><span class="metric-label">mAP50-95</span><span class="metric-value num">{{ m.metrics.mAP5095.toFixed(3) }}</span></div>
            <div class="metric-item"><span class="metric-label">Precision</span><span class="metric-value num">{{ m.metrics.precision.toFixed(3) }}</span></div>
            <div class="metric-item"><span class="metric-label">Recall</span><span class="metric-value num">{{ m.metrics.recall.toFixed(3) }}</span></div>
          </div>
          <div v-else class="no-metrics">该模型没有记录训练指标</div>
          <div class="card-actions">
            <button class="btn btn-sm" @click="goInspect(m.id)">用它做检测</button>
          </div>
        </div>
      </div>
    </section>

    <section class="section">
      <h2 class="section-title">未发布</h2>
      <div v-if="!unpublished.length" class="empty panel">
        <div class="empty-title">暂无未发布模型</div>
        <div class="empty-desc">导入 PT 或训练完成后会出现在这里，确认指标后再发布。</div>
      </div>
      <div class="model-list">
        <div v-for="m in unpublished" :key="m.id" class="panel model-card">
          <div class="model-head">
            <div class="model-name">{{ m.name }} <span class="mono">{{ m.version }}</span></div>
            <span class="tag" :class="statusClass(m)">{{ statusLabel(m.status) }}</span>
          </div>
          <div class="model-meta">
            <span class="hint">来源数据集：{{ m.dataset_name || m.dataset_id || '未记录' }}</span>
          </div>
          <div class="card-actions">
            <button class="btn btn-sm btn-primary" :disabled="!canPublish(m) || publishing === m.id" @click="publish(m)">
              {{ publishing === m.id ? '发布中…' : '发布' }}
            </button>
            <span v-if="!canPublish(m)" class="hint">只有「训练完成」的模型可以发布</span>
          </div>
        </div>
      </div>
    </section>

    <section class="section">
      <h2 class="section-title">父子模型对比</h2>
      <div class="panel compare-panel">
        <div class="compare-selects">
          <select v-model="compareParent" class="select">
            <option value="">选择父模型</option>
            <option v-for="m in models" :key="m.id" :value="m.id">{{ m.name }} {{ m.version }}</option>
          </select>
          <select v-model="compareChild" class="select">
            <option value="">选择子模型</option>
            <option v-for="m in models" :key="m.id" :value="m.id">{{ m.name }} {{ m.version }}</option>
          </select>
          <button class="btn" :disabled="comparing" @click="doCompare">
            {{ comparing ? '对比中…' : '对比' }}
          </button>
        </div>
        <table v-if="compareResult" class="data-table">
          <thead>
            <tr><th>指标</th><th>父模型</th><th>子模型</th><th>差值</th></tr>
          </thead>
          <tbody>
            <tr v-for="row in metricRows()" :key="row.label">
              <td>{{ row.label }}</td>
              <td class="num">{{ row.parent }}</td>
              <td class="num">{{ row.child }}</td>
              <td class="num">{{ row.delta }}</td>
            </tr>
          </tbody>
        </table>
        <div v-else class="hint">选择两个模型查看指标差异；对比结果直接来自后端父子对比接口。</div>
      </div>
    </section>

    <!-- 权重导入 -->
    <div v-if="showImport" class="overlay" @click.self="showImport = false">
      <div class="dialog" role="dialog" aria-label="导入模型权重">
        <div class="dialog-header">
          <h3 class="dialog-title">导入 PT 权重</h3>
          <button class="btn btn-ghost btn-sm" @click="showImport = false">✕</button>
        </div>
        <div class="dialog-body">
          <div class="field">
            <label class="field-label">.pt 权重文件</label>
            <input type="file" accept=".pt" @change="onPickWeight" />
            <div class="field-hint">后端会尝试验证权重可加载；无效文件直接报错，不会登记成可用模型。</div>
          </div>
          <div class="field">
            <label class="field-label">模型名称</label>
            <input v-model="importForm.name" class="input" placeholder="模型名称" />
          </div>
          <div class="field">
            <label class="field-label">来源数据集</label>
            <select v-model="importForm.datasetId" class="select">
              <option value="">请选择（类别表来自该数据集）</option>
              <option v-for="d in datasets.filter((x) => x.status === 1)" :key="d.id" :value="d.id">
                {{ d.name }}
              </option>
            </select>
            <div class="field-hint">只有校验通过的数据集可选。</div>
          </div>
          <div class="field">
            <label class="field-label">描述</label>
            <input v-model="importForm.description" class="input" placeholder="可选" />
          </div>
          <div v-if="importing" class="notice">
            <span class="spinner" style="display:inline-block;margin-right:8px"></span>正在校验并登记…
          </div>
        </div>
        <div class="dialog-footer">
          <button class="btn" @click="showImport = false">取消</button>
          <button class="btn btn-primary" :disabled="importing" @click="doImport">导入</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.page {
  padding: var(--content-pad);
  max-width: 1000px;
  margin: 0 auto;
}
.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 20px;
}
.header-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}
.page-title {
  font-size: 24px;
  font-weight: 600;
  margin: 0;
}
.section {
  margin-bottom: 24px;
}
.section-title {
  font-size: 16px;
  font-weight: 600;
  margin: 0 0 12px;
}
.model-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.model-card {
  padding: 16px;
}
.model-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}
.head-tags {
  display: flex;
  gap: 6px;
}
.model-name {
  font-size: 15px;
  font-weight: 600;
}
.model-meta {
  display: flex;
  gap: 16px;
  margin-bottom: 10px;
}
.metrics-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
}
.metric-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.metric-label {
  font-size: 11px;
  color: var(--text-secondary);
}
.metric-value {
  font-size: 16px;
  font-weight: 600;
}
.no-metrics {
  font-size: 13px;
  color: var(--text-secondary);
}
.hint {
  font-size: 12px;
  color: var(--text-secondary);
}
.card-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid var(--border);
}
.compare-panel {
  padding: 16px;
}
.compare-selects {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}
.overlay {
  position: fixed;
  inset: 0;
  z-index: 900;
  background: rgba(17, 23, 29, 0.42);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}
.dialog {
  width: 100%;
  max-width: 460px;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  max-height: 88vh;
  display: flex;
  flex-direction: column;
}
.dialog-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 18px;
  border-bottom: 1px solid var(--border);
}
.dialog-title {
  font-size: 16px;
  font-weight: 600;
  margin: 0;
}
.dialog-body {
  padding: 16px 18px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.dialog-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding: 12px 18px;
  border-top: 1px solid var(--border);
}
.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.field-label {
  font-size: 12px;
  color: var(--text-secondary);
}
.field-hint {
  font-size: 11px;
  color: var(--text-secondary);
}
</style>
