<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { getAdapter } from '@/services'
import type { DatasetSummary, DetectionRecordSummary } from '@/types'

const router = useRouter()

const tab = ref<'images' | 'feedback' | 'drafts'>('images')

const datasets = ref<DatasetSummary[]>([])
const records = ref<DetectionRecordSummary[]>([])
const loading = ref(false)
const loadError = ref('')
const notice = ref('')
const noticeType = ref<'success' | 'error' | ''>('')

const formalDatasets = computed(() => datasets.value.filter((d) => d.record_kind === 'formal'))
const draftDatasets = computed(() => datasets.value.filter((d) => d.record_kind === 'draft'))

/** 已人工确认的记录才算真值；rejected 不计入 */
const confirmedRecords = computed(() =>
  records.value.filter((r) => r.human_review_status === 'confirmed' || r.human_review_status === 'no_defect'),
)
const pendingRecords = computed(
  () => records.value.filter((r) => r.human_review_status !== 'confirmed' && r.human_review_status !== 'no_defect'),
)

// 导入表单
const showImport = ref(false)
const importForm = ref({
  name: '',
  description: '',
  datasetMode: 'single' as 'single' | 'multimodal',
  secondaryModality: '',
  classNames: '',
})
const importFile = ref<File | null>(null)
const importing = ref(false)

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const [d, r] = await Promise.all([
      getAdapter().listDatasets({ page: 1, pageSize: 50 }),
      getAdapter().listRecords({ page: 1, pageSize: 50 }),
    ])
    datasets.value = d.items
    records.value = r.items
  } catch (e) {
    loadError.value = '读取数据集失败：' + (e as Error).message
  } finally {
    loading.value = false
  }
}

onMounted(load)

function openImport() {
  importForm.value = { name: '', description: '', datasetMode: 'single', secondaryModality: '', classNames: '' }
  importFile.value = null
  notice.value = ''
  showImport.value = true
}

function onPickZip(e: Event) {
  const files = (e.target as HTMLInputElement).files
  importFile.value = files?.[0] ?? null
  if (importFile.value && !importForm.value.name) {
    importForm.value.name = importFile.value.name.replace(/\.zip$/i, '')
  }
}

async function doImport() {
  if (!importFile.value) {
    notice.value = '请选择 YOLO 结构的 ZIP 文件'
    noticeType.value = 'error'
    return
  }
  if (!importForm.value.name.trim()) {
    notice.value = '请填写数据集名称'
    noticeType.value = 'error'
    return
  }
  importing.value = true
  notice.value = ''
  try {
    const result = await getAdapter().uploadDataset({
      file: importFile.value,
      name: importForm.value.name.trim(),
      description: importForm.value.description,
      datasetMode: importForm.value.datasetMode,
      secondaryModality: importForm.value.secondaryModality,
      classNames: importForm.value.classNames.split(/[,，;；\s]+/).filter(Boolean),
    })
    const warnings = Array.isArray((result as Record<string, unknown>).warnings)
      ? ((result as Record<string, unknown>).warnings as unknown[])
      : []
    noticeType.value = 'success'
    notice.value = warnings.length
      ? `导入成功，但有 ${warnings.length} 条结构提示：${warnings.slice(0, 2).join('；')}`
      : '数据集导入成功，已登记并校验通过。'
    showImport.value = false
    await load()
  } catch (e) {
    noticeType.value = 'error'
    notice.value = '导入失败：' + (e as Error).message
  } finally {
    importing.value = false
  }
}

async function addConfirmedToFeedback() {
  if (!confirmedRecords.value.length) return
  notice.value = ''
  try {
    const result = await getAdapter().addFeedbackBatch(confirmedRecords.value.map((r) => r.id))
    const succeeded = Number((result as Record<string, unknown>).succeeded ?? 0)
    const failed = Number((result as Record<string, unknown>).failed ?? 0)
    noticeType.value = failed ? 'error' : 'success'
    notice.value = `已回流 ${succeeded} 条到各自原训练数据集的反馈池${failed ? `，${failed} 条失败` : ''}。`
    await load()
  } catch (e) {
    noticeType.value = 'error'
    notice.value = '加入反馈池失败：' + (e as Error).message
  }
}

/** 用于训练：跳转到模型页并携带数据集，训练参数仍由人在模型页确认 */
function useForTraining(datasetId: string) {
  router.push({ path: '/models', query: { dataset: datasetId } })
}
</script>

<template>
  <div class="page">
    <div class="page-header">
      <h1 class="page-title">数据集</h1>
      <div class="header-actions">
        <span class="tag num">{{ formalDatasets.length }} 个数据集 · {{ draftDatasets.length }} 个草稿</span>
        <button class="btn btn-primary" @click="openImport">导入 ZIP 数据集</button>
      </div>
    </div>

    <div v-if="notice" class="notice" :class="noticeType === 'error' ? 'notice-error' : 'notice-success'">
      {{ notice }}
    </div>

    <div class="tabs">
      <button class="tab" :class="{ active: tab === 'images' }" @click="tab = 'images'">数据集</button>
      <button class="tab" :class="{ active: tab === 'feedback' }" @click="tab = 'feedback'">反馈池</button>
      <button class="tab" :class="{ active: tab === 'drafts' }" @click="tab = 'drafts'">候选草稿</button>
    </div>

    <!-- 数据集 -->
    <div v-if="tab === 'images'" class="panel">
      <div class="panel-head">
        <span>已登记数据集</span>
        <span class="num">{{ formalDatasets.length }} 个</span>
      </div>
      <div v-if="loading" class="empty"><div class="empty-desc">正在读取…</div></div>
      <div v-else-if="loadError" class="empty">
        <div class="empty-title">读取失败</div>
        <div class="empty-desc">{{ loadError }}</div>
        <button class="btn" @click="load">重试</button>
      </div>
      <div v-else-if="!formalDatasets.length" class="empty">
        <div class="empty-title">暂无数据集</div>
        <div class="empty-desc">导入 YOLO 结构的 ZIP 后，后端会校验目录与标签配对，通过后才会登记。</div>
      </div>
      <table v-else class="data-table">
        <thead>
          <tr><th>名称</th><th>模态</th><th>样本数</th><th>状态</th><th>上传时间</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="d in formalDatasets" :key="d.id">
            <td>{{ d.name }}</td>
            <td>
              {{ d.dataset_mode === 'multimodal' ? 'RGB + ' + (d.secondary_modality || 'X') : '单模态' }}
            </td>
            <td class="num">{{ d.sample_count }}</td>
            <td>
              <span class="tag" :class="d.status === 1 ? 'tag-success' : d.status === 2 ? 'tag-error' : ''">
                {{ d.status === 1 ? '校验通过' : d.status === 2 ? '校验失败' : '待校验' }}
              </span>
            </td>
            <td class="num">{{ d.upload_time?.slice(0, 19).replace('T', ' ') || '—' }}</td>
            <td>
              <button class="btn btn-sm" :disabled="d.status !== 1" @click="useForTraining(d.id)">用于训练</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 反馈池 -->
    <div v-else-if="tab === 'feedback'" class="panel">
      <div class="panel-head">
        <span>反馈池</span>
        <span class="tag tag-warn">未复核结果不自动作为真值</span>
      </div>
      <div class="feedback-groups">
        <div class="feedback-group">
          <div class="group-title">已人工确认（可回流）</div>
          <div class="group-count num">{{ confirmedRecords.length }} 项</div>
          <button class="btn btn-sm" :disabled="!confirmedRecords.length" @click="addConfirmedToFeedback">
            批量加入反馈池
          </button>
        </div>
        <div class="feedback-group">
          <div class="group-title">待复核（不作为真值）</div>
          <div class="group-count num">{{ pendingRecords.length }} 项</div>
          <span class="hint">待复核结果需人工确认后才能进入草稿</span>
        </div>
      </div>
      <div class="hint" style="margin-top: 12px">
        回流按每条记录所属模型回到它原来的训练数据集；来源不明确时后端会逐条报错，不会硬塞。
      </div>
    </div>

    <!-- 候选草稿 -->
    <div v-else class="panel">
      <div class="panel-head">
        <span>候选草稿</span>
        <span class="hint">创建草稿不自动训练</span>
      </div>
      <div v-if="!draftDatasets.length" class="empty">
        <div class="empty-title">暂无草稿</div>
        <div class="empty-desc">草稿由数据集变换或反馈回流产生，确认定稿后才成为正式数据集。</div>
      </div>
      <table v-else class="data-table">
        <thead>
          <tr><th>草稿名称</th><th>模态</th><th>样本数</th><th>流程状态</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="d in draftDatasets" :key="d.id">
            <td>{{ d.name }}</td>
            <td>{{ d.dataset_mode === 'multimodal' ? '多模态' : '单模态' }}</td>
            <td class="num">{{ d.sample_count }}</td>
            <td><span class="tag">{{ d.workflow_status || 'draft' }}</span></td>
            <td>
              <button class="btn btn-sm" :disabled="d.workflow_status !== 'finalized'" @click="useForTraining(d.id)">
                用于训练
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 导入弹窗 -->
    <div v-if="showImport" class="overlay" @click.self="showImport = false">
      <div class="dialog" role="dialog" aria-label="导入数据集">
        <div class="dialog-header">
          <h3 class="dialog-title">导入 ZIP 数据集</h3>
          <button class="btn btn-ghost btn-sm" @click="showImport = false">✕</button>
        </div>
        <div class="dialog-body">
          <div class="field">
            <label class="field-label">ZIP 文件</label>
            <input type="file" accept=".zip" @change="onPickZip" />
            <div class="field-hint">需符合 YOLO 目录结构（images/labels 与 train/val 划分），后端会逐项校验。</div>
          </div>
          <div class="field">
            <label class="field-label">名称</label>
            <input v-model="importForm.name" class="input" placeholder="数据集名称" />
          </div>
          <div class="field">
            <label class="field-label">描述</label>
            <input v-model="importForm.description" class="input" placeholder="可选" />
          </div>
          <div class="field">
            <label class="field-label">类型</label>
            <select v-model="importForm.datasetMode" class="select">
              <option value="single">单模态</option>
              <option value="multimodal">RGB + X 多模态</option>
            </select>
          </div>
          <div v-if="importForm.datasetMode === 'multimodal'" class="field">
            <label class="field-label">第二模态名称</label>
            <input v-model="importForm.secondaryModality" class="input" placeholder="例如 infrared / xray" />
          </div>
          <div class="field">
            <label class="field-label">类别名（按类别 id 顺序）</label>
            <input v-model="importForm.classNames" class="input" placeholder="可选，逗号分隔" />
          </div>
          <div v-if="importing" class="notice">
            <span class="spinner" style="display:inline-block;margin-right:8px"></span>正在上传并校验…
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
  margin-bottom: 16px;
}
.page-title {
  font-size: 24px;
  font-weight: 600;
  margin: 0;
}
.tabs {
  display: flex;
  gap: 4px;
  border-bottom: 1px solid var(--border);
  margin-bottom: 16px;
}
.tab {
  height: 36px;
  padding: 0 16px;
  border: none;
  background: transparent;
  color: var(--text-secondary);
  font-size: 14px;
  cursor: pointer;
  border-bottom: 2px solid transparent;
  margin-bottom: -1px;
}
.tab.active {
  color: var(--text);
  border-bottom-color: var(--accent);
  font-weight: 500;
}
.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
  font-size: 14px;
  font-weight: 500;
}
.feedback-groups {
  display: flex;
  gap: 16px;
  padding: 16px;
}
.feedback-group {
  flex: 1;
  padding: 16px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
}
.group-title {
  font-size: 13px;
  font-weight: 500;
  margin-bottom: 8px;
}
.group-count {
  font-size: 22px;
  font-weight: 600;
  margin-bottom: 12px;
}
.hint {
  font-size: 12px;
  color: var(--text-secondary);
}
.data-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.data-table th,
.data-table td {
  text-align: left;
  padding: 10px 14px;
  border-bottom: 1px solid var(--border);
}
.header-actions {
  display: flex;
  align-items: center;
  gap: 10px;
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
