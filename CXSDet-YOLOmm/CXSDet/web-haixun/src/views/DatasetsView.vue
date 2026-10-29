<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { getAdapter } from '@/services'
import type { DatasetSummary, DetectionRecordSummary } from '@/types'
import DraftEditor from '@/components/DraftEditor.vue'
import DatasetSplitDialog from '@/components/DatasetSplitDialog.vue'
import FeedbackRevision from '@/components/FeedbackRevision.vue'
import DatasetAuditDialog from '@/components/DatasetAuditDialog.vue'
import ResourceRecycle from '@/components/ResourceRecycle.vue'
import { changeResource, useResourceRefresh } from '@/utils/resourceLifecycle'
import { isPendingReview } from '@/utils/reviewStatus'
import { useWorkspaceStore } from '@/stores/workspace'
import { feedbackEligibleRecords } from '@/utils/feedbackSelection'
const workspace = useWorkspaceStore()
const route = useRoute()
const editingDraft = ref(String(route.query.draft || ''))
const creatingDraft = ref(false)
const splittingDataset = ref<DatasetSummary | null>(null)
const flowStep = ref(1)
const feedbackSource = ref('')
const feedbackDestination = ref<'original' | 'new'>('original')
const auditingDataset = ref<DatasetSummary | null>(null)
const addingFeedback = ref(false)
function splitCompleted(name: string) { splittingDataset.value = null; noticeType.value = 'success'; notice.value = `新数据集「${name}」已登记，原数据集保留，未启动训练。`; void load() }
function revisionCompleted(name: string, directory: string) {
  tab.value = 'images'; noticeType.value = 'success'
  notice.value = `修订版「${name}」已生成，原数据集保留，未启动训练。${directory ? '工作区目录：' + directory : ''}`
  void load()
}
async function createDraft(d: DatasetSummary) {
  if (creatingDraft.value || !window.confirm(`确认提取「${d.name}」已人工确认的反馈样本，创建待整理版本？仅包含反馈样本，不合并原训练集。设置样本用途并检查通过后，才会生成正式数据集。`)) return
  creatingDraft.value = true
  try {
    const result = await getAdapter().createFeedbackDraft(d.id)
    tab.value = 'drafts'
    await load()
    const id = String(result.id || '')
    if (!id || !draftDatasets.value.some(item => item.id === id)) throw new Error('反馈草稿已创建，但列表未能读取到它。请刷新核实，不要重复生成。' + (loadError.value || ''))
    editingDraft.value = id
    flowStep.value = 3
    noticeType.value = 'success'; notice.value = '反馈样本已提取。请设置样本用途，再检查并生成数据集；原数据集保留。'
  }
  catch (e) { noticeType.value = 'error'; notice.value = (e as Error).message }
  finally { creatingDraft.value = false }
}

const router = useRouter()

const tab = ref<'images' | 'feedback' | 'drafts'>('images')
watch(() => [route.query.tab, route.query.draft], () => {
  const target = String(route.query.tab || '')
  if (['images', 'feedback', 'drafts'].includes(target)) tab.value = target as typeof tab.value
  editingDraft.value = String(route.query.draft || '')
  if (target === 'drafts' || editingDraft.value) { tab.value = 'drafts'; flowStep.value = 3 }
  else if (target === 'feedback') flowStep.value = 1
}, { immediate: true })

const datasets = ref<DatasetSummary[]>([])
const records = ref<DetectionRecordSummary[]>([])
const loading = ref(false)
const loadError = ref('')
const notice = ref('')
const noticeType = ref<'success' | 'error' | ''>('')

const formalDatasets = computed(() => datasets.value.filter((d) => d.record_kind === 'formal'))
const draftDatasets = computed(() => datasets.value.filter((d) => d.record_kind === 'draft'))
const selectedSource = computed(() => formalDatasets.value.find(dataset => dataset.id === feedbackSource.value))
const readyForFeedback = computed(() => feedbackEligibleRecords(records.value, records.value.map(record => record.id)))
const pooledRecords = computed(() => confirmedRecords.value.filter(record => !!record.feedback_dataset_id))
function openFeedback(dataset?: DatasetSummary) {
  tab.value = 'feedback'
  editingDraft.value = ''
  flowStep.value = dataset ? 2 : 1
  if (dataset) feedbackSource.value = dataset.id
  notice.value = ''
}
function openDraft(id: string) { tab.value = 'drafts'; flowStep.value = 3; editingDraft.value = id }
async function finalized() {
  editingDraft.value = ''; tab.value = 'images'; noticeType.value = 'success'
  notice.value = '新数据集已定稿，请在列表中查看；原数据集保留，尚未启动训练。'
  await load()
}

/** 已人工确认的记录才算真值；rejected 不计入 */
const confirmedRecords = computed(() =>
  records.value.filter((r) => r.human_review_status === 'confirmed' || r.human_review_status === 'no_defect'),
)
const pendingRecords = computed(
  () => records.value.filter((r) => isPendingReview(r.human_review_status)),
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
      getAdapter().listDatasets({ page: 1, pageSize: 200, recordKind: 'all' }),
      getAdapter().listRecords({ page: 1, pageSize: 50 }),
    ])
    datasets.value = d.items
    records.value = r.items
    if (!feedbackSource.value) feedbackSource.value = r.items.find(record => record.feedback_dataset_id && d.items.some(dataset => dataset.id === record.feedback_dataset_id))?.feedback_dataset_id || ''
  } catch (e) {
    loadError.value = '读取数据集失败：' + (e as Error).message
  } finally {
    loading.value = false
  }
}

onMounted(load)
useResourceRefresh(load)
const deleting = ref('')
async function deleteDataset(d: DatasetSummary) {
  if (deleting.value) return
  deleting.value = d.id
  try { if (await changeResource(d.record_kind === 'draft' ? 'draft' : 'dataset', d.id, 'delete')) { noticeType.value = 'success'; notice.value = '已移入回收站，文件和历史来源保留。'; if (editingDraft.value === d.id) editingDraft.value = ''; await load() } }
  catch (e) { noticeType.value = 'error'; notice.value = (e as Error).message }
  finally { deleting.value = '' }
}

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
      projectId: workspace.currentId,
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
  const targets = readyForFeedback.value.map(record => record.id)
  if (!targets.length || addingFeedback.value) return
  if (!window.confirm(`确认将当前已载记录中 ${targets.length} 条已人工确认、尚未入池的样本加入各自来源数据集的反馈池？不修改原数据集文件，不自动生成新版本。`)) return
  addingFeedback.value = true
  notice.value = ''
  try {
    const result = await getAdapter().addFeedbackBatch(targets)
    const succeeded = Number((result as Record<string, unknown>).succeeded ?? 0)
    const failed = Number((result as Record<string, unknown>).failed ?? 0)
    noticeType.value = failed ? 'error' : 'success'
    const items = (result.items || []) as Array<{record_id: string; status: string; error?: string}>
    notice.value = `已加入反馈池 ${succeeded} 条${failed ? `，${failed} 条失败：${items.filter(item => item.status !== 'success').map(item => item.record_id + '：' + item.error).join('；')}` : '，继续选择来源并生成新版本'}。`
    if (!failed) flowStep.value = 2
    await load()
  } catch (e) {
    noticeType.value = 'error'
    notice.value = '加入反馈池失败：' + (e as Error).message
  } finally { addingFeedback.value = false }
}

/** 用于训练：跳转到模型页并携带数据集，训练参数仍由人在模型页确认 */
function useForTraining(datasetId: string) {
  router.push({ path: '/models', query: { dataset: datasetId } })
}
</script>

<template>
  <div class="page">
    <DatasetAuditDialog v-if="auditingDataset" :key="auditingDataset.id" :dataset="auditingDataset" @close="auditingDataset=null" />
    <DatasetSplitDialog v-if="splittingDataset" :dataset="splittingDataset" @close="splittingDataset = null" @completed="splitCompleted" />
    <div class="page-header">
      <h1 class="page-title">数据集</h1>
      <div class="header-actions">
        <span class="tag num">{{ formalDatasets.length }} 个数据集 · {{ draftDatasets.length }} 个待整理版本</span>
        <button class="btn btn-primary" @click="openImport">导入 ZIP 数据集</button>
      </div>
    </div>

    <div v-if="notice" class="notice" :class="noticeType === 'error' ? 'notice-error' : 'notice-success'">
      {{ notice }}
    </div>

    <div class="section-switch" role="tablist" aria-label="数据集工作方式">
      <button class="section-option" role="tab" :aria-selected="tab === 'images'" :class="{ active: tab === 'images' }" @click="tab = 'images'; editingDraft = ''; notice = ''">
        <strong>我的数据集</strong><span>导入 → 划分 → 用于模型</span>
      </button>
      <button class="section-option" role="tab" :aria-selected="tab !== 'images'" :class="{ active: tab !== 'images' }" @click="openFeedback()">
        <strong>处理复核结果</strong><span>回原数据集，或另建数据集</span>
      </button>
    </div>

    <section v-if="tab === 'images'" class="panel" aria-label="我的数据集">
      <div class="panel-head"><span>可用于模型的数据集</span><span class="hint">{{ formalDatasets.length }} 个 · 修改生成新版本，原数据保留</span></div>
      <div v-if="loading" class="empty">正在读取…</div>
      <div v-else-if="loadError" class="empty"><p role="alert">{{ loadError }}</p><button class="btn" @click="load">重试</button></div>
      <div v-else-if="!formalDatasets.length" class="empty"><div class="empty-title">还没有数据集</div><p class="empty-desc">导入图片与标签的 ZIP，也可以让助手从项目文件夹查找并导入。</p><button class="btn" @click="openImport">导入 ZIP 数据集</button></div>
      <div v-else class="table-scroll"><table class="data-table">
        <thead><tr><th>数据集</th><th>样本数</th><th>状态</th><th>操作</th></tr></thead>
        <tbody><tr v-for="d in formalDatasets" :key="d.id">
          <td class="dataset-name"><strong>{{ d.name }}</strong><small>{{ d.dataset_mode === 'multimodal' ? 'RGB + ' + (d.secondary_modality || 'X') : 'RGB 单模态' }} · {{ d.upload_time?.slice(0, 19).replace('T', ' ') || '—' }}</small></td>
          <td class="num">{{ d.sample_count }}</td>
          <td><span class="tag" :class="d.status === 1 ? 'tag-success' : d.status === 2 ? 'tag-error' : ''">{{ d.status === 1 ? '可使用' : d.status === 2 ? '校验失败' : '待校验' }}</span></td>
          <td><div class="row-actions">
            <button class="btn btn-sm" :disabled="d.status !== 1" @click="useForTraining(d.id)">进入模型管理</button>
            <button class="btn btn-sm" :disabled="d.status !== 1" @click="splittingDataset = d">重新划分</button>
            <details class="row-more"><summary>更多</summary><div class="row-menu"><button class="btn btn-sm" :disabled="d.status !== 1" @click="openFeedback(d)">整理此数据集的反馈</button><button class="btn btn-sm" :disabled="d.status !== 1" @click="auditingDataset=d">文件来源与重复检查</button><button class="btn btn-sm danger" :disabled="!!deleting" @click="deleteDataset(d)">删除数据集</button></div></details>
          </div></td>
        </tr></tbody>
      </table></div>
      <p class="section-note">“重新划分”按比例分配当前数据集的全部样本，不会把反馈池样本自动合并进来。</p>
    </section>

    <section v-else class="feedback-flow" aria-label="处理复核结果">
      <div class="flow-heading"><div><h2>处理复核结果</h2><p class="hint">将人工确认的反馈回原数据集生成修订版，或单独组成新数据集。原版本保留。</p></div><button v-if="draftDatasets.length" class="btn btn-sm" @click="flowStep = 3">继续整理 {{ draftDatasets.length }} 个待完成版本</button></div>
      <nav class="flow-steps" aria-label="生成新版本步骤">
        <button v-for="step in [{id:1,title:'收集已确认样本'},{id:2,title:'选择去向并生成'},{id:3,title:'待整理的新数据集'}]" :key="step.id" :class="{active:flowStep === step.id}" :aria-current="flowStep === step.id ? 'step' : undefined" @click="flowStep = step.id"><span>{{ step.id }}</span>{{ step.title }}</button>
      </nav>
      <div v-if="loading" class="panel flow-content">正在读取…</div>
      <div v-else-if="loadError" class="panel flow-content"><p role="alert">{{ loadError }}</p><button class="btn" @click="load">重试</button></div>
      <div v-else-if="flowStep === 1" class="panel flow-content">
        <h3>把已人工确认的样本收集到反馈池</h3>
        <p class="hint">以下数量来自当前载入的最近 50 条识别记录。需要指定其他记录时，可在识别记录页勾选，或告诉助手具体范围。</p>
        <div class="feedback-groups">
          <div class="feedback-group"><div class="group-title">已确认，待加入</div><div class="group-count num">{{ readyForFeedback.length }}</div><button class="btn btn-primary" :disabled="addingFeedback || !readyForFeedback.length" @click="addConfirmedToFeedback">{{ addingFeedback ? '正在加入…' : '加入反馈池并继续' }}</button></div>
          <div class="feedback-group"><div class="group-title">已加入反馈池</div><div class="group-count num">{{ pooledRecords.length }}</div><span class="hint">无需重复加入，可以直接进入下一步。</span></div>
          <div class="feedback-group"><div class="group-title">待人工复核</div><div class="group-count num">{{ pendingRecords.length }}</div><button class="btn btn-sm" @click="router.push('/projects/' + workspace.currentId + '/review')">去人工复核</button></div>
        </div>
        <button class="btn" @click="flowStep = 2">已有反馈，选择来源 →</button>
      </div>
      <div v-else-if="flowStep === 2" class="panel flow-content">
        <h3>选择这些反馈所属的原数据集</h3>
        <p>反馈跟随检测时使用的模型，进入该模型关联的数据集反馈池。请选择对应来源。</p>
        <label class="field-label" for="feedback-source">来源数据集</label>
        <select id="feedback-source" v-model="feedbackSource" class="select source-select"><option value="">请选择来源数据集</option><option v-for="d in formalDatasets.filter(item => item.status === 1)" :key="d.id" :value="d.id">{{ d.name }}</option></select>
        <p class="hint">数据集的样本数不是反馈池数量。已经生成的反馈数据集，也不一定产生过新的反馈。</p>
        <button v-if="selectedSource" class="btn btn-sm" @click="auditingDataset=selectedSource">文件来源与重复检查</button>
        <fieldset class="destination-options"><legend>复核后的数据放到哪里？</legend>
          <label :class="{selected: feedbackDestination === 'original'}"><input v-model="feedbackDestination" type="radio" value="original" /><strong>回原数据集</strong><span>原有数据 + 复核修正，生成修订版</span></label>
          <label :class="{selected: feedbackDestination === 'new'}"><input v-model="feedbackDestination" type="radio" value="new" /><strong>另建数据集</strong><span>只包含确认反馈，设置用途后生成</span></label>
        </fieldset>
        <FeedbackRevision v-if="selectedSource && feedbackDestination === 'original'" :key="selectedSource.id" :dataset="selectedSource" @completed="revisionCompleted" />
        <div v-else-if="feedbackDestination === 'new'" class="new-dataset-action"><p class="hint">仅提取所选来源的有效反馈，不包含原训练集。下一步设置训练 / 验证 / 测试或排除，检查通过后生成独立数据集。</p>
          <button class="btn btn-primary" :disabled="!selectedSource || creatingDraft" @click="selectedSource && createDraft(selectedSource)">{{ creatingDraft ? '正在准备…' : '提取反馈，设置新数据集' }}</button>
        </div>
        <button class="btn" @click="flowStep = 1">返回收集样本</button>
      </div>
      <div v-else class="panel flow-content">
        <h3>整理样本，生成正式数据集</h3>
        <p class="hint">这里保存待完成的版本（原“候选草稿”）。完成训练 / 验证 / 测试划分并检查通过后，才会出现在“我的数据集”。</p>
        <DraftEditor v-if="editingDraft" :draft-id="editingDraft" @close="editingDraft = ''" @finalized="finalized" />
        <div v-else-if="!draftDatasets.length" class="empty"><div class="empty-title">还没有待整理版本</div><p class="empty-desc">先选择来源并提取反馈样本，已经定稿的版本可在“我的数据集”查看。</p><button class="btn" @click="flowStep = 2">选择来源</button></div>
        <div v-else class="draft-list"><article v-for="d in draftDatasets" :key="d.id" class="draft-item"><div class="dataset-name"><strong>{{ d.name }}</strong><small>{{ d.sample_count }} 个样本 · {{ ({ draft:'待整理', generating:'生成中', failed:'生成失败', finalized:'已生成' } as Record<string,string>)[d.workflow_status || 'draft'] || d.workflow_status }}</small></div><div class="row-actions"><button class="btn btn-sm" :disabled="d.workflow_status === 'generating'" @click="openDraft(d.id)">整理并生成</button><button class="btn btn-sm" :disabled="!!deleting" @click="deleteDataset(d)">删除</button></div></article></div>
      </div>
    </section>

    <ResourceRecycle :kinds="['dataset', 'draft']" @changed="load" />

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
.destination-options{border:0;padding:0;margin:20px 0;display:flex;gap:12px;flex-wrap:wrap}.destination-options legend{font-weight:600;margin-bottom:10px;font-size:14px}.destination-options label{flex:1;min-width:180px;border:1px solid var(--border);border-radius:8px;padding:14px;cursor:pointer}.destination-options label.selected{border-color:var(--accent)}.destination-options strong{margin-left:8px;font-size:14px}.destination-options span{display:block;margin-top:8px;font-size:12px;color:var(--text-secondary)}.new-dataset-action{margin:16px 0}
.section-switch { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin: 20px 0; }
.section-option { display: flex; flex-direction: column; gap: 7px; text-align: left; padding: 16px; border: 1px solid var(--border); border-radius: 12px; background: var(--panel); color: var(--text); cursor: pointer; }
.section-option strong { font-size: 14px; }
.section-option span { font-size: 12px; color: var(--text-secondary); line-height: 1.6; }
.section-option.active { border-color: var(--text-secondary); background: var(--panel-secondary); }
.table-scroll { overflow-x: auto; }
.dataset-name { min-width: 0; overflow-wrap: anywhere; }
.dataset-name strong { font-size: 13px; font-weight: 500; }
.dataset-name small { display: block; margin-top: 6px; color: var(--text-secondary); font-size: 11px; line-height: 1.6; }
.row-actions { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
.row-more { position: relative; font-size: 12px; }
.row-more summary { cursor: pointer; padding: 6px 8px; }
.row-menu { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; padding: 6px; border: 1px solid var(--border); border-radius: 8px; }
.danger { color: var(--danger); }
.section-note { color: var(--text-secondary); padding: 0 16px 10px; font-size: 12px; line-height: 1.7; }
.flow-heading { display: flex; gap: 12px; align-items: center; justify-content: space-between; flex-wrap: wrap; margin: 22px 0 14px; }
.flow-heading h2 { margin: 0 0 8px; font-size: 18px; }
.flow-heading p { margin: 0; line-height: 1.7; }
.flow-steps { display: flex; gap: 8px; margin-bottom: 14px; }
.flow-steps button { flex: 1; display: flex; align-items: center; gap: 8px; background: var(--panel); border: 1px solid var(--border); border-radius: 9px; color: var(--text-secondary); padding: 12px; font-size: 12px; cursor: pointer; }
.flow-steps button.active { color: var(--text); border-color: var(--text-secondary); background: var(--panel-secondary); }
.flow-steps button span { border: 1px solid var(--border); border-radius: 50%; width: 22px; height: 22px; display: grid; place-items: center; flex-shrink: 0; }
.flow-content { padding: 20px; }
.flow-content h3 { font-size: 15px; margin: 0 0 12px; }
.flow-content p { font-size: 13px; line-height: 1.8; }
.flow-content > button { margin: 8px 8px 0 0; }
.flow-content .feedback-groups { padding: 10px 0; flex-wrap: wrap; }
.flow-content .feedback-group { min-width: 160px; }
.source-select { width: 100%; display: block; margin-top: 8px; }
.draft-item { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; padding: 16px 0; border-top: 1px solid var(--border); }
.draft-item .dataset-name { flex: 1; min-width: 180px; }
@media (max-width: 650px) { .section-switch { grid-template-columns: 1fr; } .flow-steps { flex-direction: column; } .page-header, .header-actions { flex-wrap: wrap; gap: 10px; } }
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
