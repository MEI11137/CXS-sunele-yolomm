<template>
  <div class="draft-split-page">
    <div class="page-header">
      <div>
        <el-button text @click="router.push({ name: 'DatasetManagement' })">← 返回数据集管理</el-button>
        <h2>{{ draft?.name || '待划分数据集' }}</h2>
        <p>{{ draft?.description || '请人工检查样本，并将每条样本分配到训练、验证、测试或排除。' }}</p>
      </div>
      <el-button type="primary" :disabled="finalizationBlocked" :loading="finalizing" @click="finalize">
        完成划分并生成正式数据集
      </el-button>
    </div>

    <el-alert v-if="draft?.workflow_status === 'generating'" title="数据仍在生成，请稍后刷新" type="info" show-icon />
    <el-alert v-else-if="draft?.workflow_status === 'failed'" :title="draft.error_message || '数据集生成失败'" type="error" show-icon />

    <div class="split-summary">
      <el-card v-for="item in splitCards" :key="item.key" shadow="never">
        <span>{{ item.label }}</span><strong>{{ counts[item.key] }}</strong>
      </el-card>
      <el-card shadow="never" :class="{ 'warning-card': missingSecondary > 0 }">
        <span>待补充 X 模态</span><strong>{{ missingSecondary }}</strong>
      </el-card>
    </div>

    <el-alert v-if="finalizationBlocked && draft?.workflow_status === 'draft'" type="warning" :closable="false" show-icon>
      <template #title>完成训练集和验证集划分，并处理未分配或缺失的多模态样本后，才能生成正式数据集。</template>
    </el-alert>

    <div class="toolbar">
      <el-select v-model="splitFilter" placeholder="全部划分状态" clearable @change="changeFilter">
        <el-option label="未分配" value="unassigned" />
        <el-option label="训练集" value="train" />
        <el-option label="验证集" value="val" />
        <el-option label="测试集" value="test" />
        <el-option label="已排除" value="excluded" />
      </el-select>
      <el-select v-model="pairingFilter" placeholder="全部配对状态" clearable @change="changeFilter">
        <el-option label="配对完整" value="paired" />
        <el-option label="缺少 X 模态" value="missing" />
        <el-option label="单模态" value="not_required" />
      </el-select>
      <span>已选 {{ selectedIds.length }} 条</span>
      <el-button v-for="item in assignmentButtons" :key="item.key" :disabled="!selectedIds.length || draft?.workflow_status !== 'draft'" @click="assign(item.key)">
        {{ item.label }}
      </el-button>
      <el-button :loading="loading" @click="refresh">刷新</el-button>
    </div>

    <el-table :data="samples" v-loading="loading" row-key="id" @selection-change="onSelectionChange">
      <el-table-column type="selection" width="48" />
      <el-table-column label="RGB / X 预览" min-width="260">
        <template #default="{ row }">
          <div class="previews">
            <el-image v-if="previewUrls[row.id]?.rgb" :src="previewUrls[row.id]?.rgb || ''" fit="cover" :preview-src-list="[previewUrls[row.id]?.rgb || '']" />
            <span v-else class="preview-placeholder">RGB</span>
            <el-image v-if="previewUrls[row.id]?.secondary" :src="previewUrls[row.id]?.secondary || ''" fit="cover" :preview-src-list="[previewUrls[row.id]?.secondary || '']" />
            <span v-else-if="draft?.dataset_mode === 'multimodal'" class="preview-placeholder missing">X 缺失</span>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="帧 / 标签" min-width="180">
        <template #default="{ row }">
          <div>帧 {{ row.frame_index ?? '—' }} · {{ row.annotations?.length || 0 }} 个目标</div>
          <div class="class-list">{{ classNames(row) || '无目标（空标签）' }}</div>
          <small v-if="row.primary_timestamp != null">时间 {{ row.primary_timestamp.toFixed(3) }} 秒</small>
        </template>
      </el-table-column>
      <el-table-column label="配对" width="120">
        <template #default="{ row }">
          <el-tag :type="row.pairing_state === 'missing' ? 'danger' : 'success'">
            {{ row.pairing_state === 'missing' ? '缺少 X' : row.pairing_state === 'paired' ? '已配对' : '单模态' }}
          </el-tag>
          <small v-if="row.pairing_error_ms != null">误差 {{ Math.round(row.pairing_error_ms) }} ms</small>
        </template>
      </el-table-column>
      <el-table-column label="当前划分" width="125">
        <template #default="{ row }">{{ splitLabel(row.split) }}</template>
      </el-table-column>
      <el-table-column v-if="draft?.dataset_mode === 'multimodal'" label="补充 X 模态" width="150">
        <template #default="{ row }">
          <label class="upload-label" :class="{ disabled: draft?.workflow_status !== 'draft' }">
            上传图片
            <input type="file" accept="image/*" :disabled="draft?.workflow_status !== 'draft'" @change="repairSecondary(row, $event)" />
          </label>
        </template>
      </el-table-column>
    </el-table>

    <div class="pagination">
      <el-pagination v-model:current-page="page" v-model:page-size="pageSize" :total="total" :page-sizes="[12, 24, 48]" layout="total, sizes, prev, pager, next" @change="loadSamples" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { datasetApi, type DatasetDraft, type DatasetDraftSample, type DraftSampleSplit } from '@/api/dataset'

const route = useRoute()
const router = useRouter()
const draftId = computed(() => String(route.params.draftId || ''))
const draft = ref<DatasetDraft | null>(null)
const samples = ref<DatasetDraftSample[]>([])
const selectedIds = ref<string[]>([])
const page = ref(1)
const pageSize = ref(24)
const total = ref(0)
const loading = ref(false)
const finalizing = ref(false)
const splitFilter = ref<DraftSampleSplit | ''>('')
const pairingFilter = ref<DatasetDraftSample['pairing_state'] | ''>('')
const missingSecondary = ref(0)
const previewUrls = reactive<Record<string, { rgb?: string; secondary?: string }>>({})
const counts = reactive<Record<DraftSampleSplit, number>>({ unassigned: 0, train: 0, val: 0, test: 0, excluded: 0 })
const splitCards = [
  { key: 'unassigned' as const, label: '未分配' }, { key: 'train' as const, label: '训练集' },
  { key: 'val' as const, label: '验证集' }, { key: 'test' as const, label: '测试集' },
  { key: 'excluded' as const, label: '已排除' },
]
const assignmentButtons: Array<{ key: DraftSampleSplit; label: string }> = [
  { key: 'train', label: '移至训练集' }, { key: 'val', label: '移至验证集' },
  { key: 'test', label: '移至测试集' }, { key: 'excluded', label: '排除所选' },
  { key: 'unassigned', label: '取消划分' },
]
const finalizationBlocked = computed(() => !draft.value || draft.value.workflow_status !== 'draft' ||
  counts.unassigned > 0 || counts.train < 1 || counts.val < 1 || missingSecondary.value > 0)

const revokePreviews = () => {
  Object.values(previewUrls).forEach(urls => {
    if (urls.rgb) URL.revokeObjectURL(urls.rgb)
    if (urls.secondary) URL.revokeObjectURL(urls.secondary)
  })
  Object.keys(previewUrls).forEach(key => delete previewUrls[key])
}

const loadMissingSecondaryCount = async () => {
  if (draft.value?.dataset_mode !== 'multimodal') { missingSecondary.value = 0; return }
  let pageIndex = 1
  let missing = 0
  let pages = 1
  while (pageIndex <= pages) {
    const result = await datasetApi.listDraftSamples(draftId.value, { page: pageIndex, page_size: 200, pairing_state: 'missing' })
    pages = Math.ceil(result.total / 200)
    missing += result.samples.filter(sample => sample.split !== 'excluded').length
    pageIndex += 1
  }
  missingSecondary.value = missing
}

const loadSamples = async () => {
  if (!draftId.value) return
  loading.value = true
  revokePreviews()
  try {
    const result = await datasetApi.listDraftSamples(draftId.value, {
      page: page.value, page_size: pageSize.value,
      ...(splitFilter.value ? { split: splitFilter.value } : {}),
      ...(pairingFilter.value ? { pairing_state: pairingFilter.value } : {}),
    })
    draft.value = result.draft
    samples.value = result.samples
    total.value = result.total
    await Promise.all(result.samples.map(async sample => {
      const urls: { rgb?: string; secondary?: string } = {}
      try { urls.rgb = URL.createObjectURL(await datasetApi.getDraftSamplePreview(draftId.value, sample.id, 'rgb')) } catch { /* removed sample preview */ }
      if (sample.secondary_preview_url) {
        try { urls.secondary = URL.createObjectURL(await datasetApi.getDraftSamplePreview(draftId.value, sample.id, 'secondary')) } catch { /* unavailable secondary preview */ }
      }
      previewUrls[sample.id] = urls
    }))
    await Promise.all([loadMissingSecondaryCount(), loadSplitCounts()])
  } catch (error: any) {
    ElMessage.error(error.message || '加载待划分样本失败')
  } finally {
    loading.value = false
  }
}

const loadSplitCounts = async () => {
  await Promise.all((Object.keys(counts) as DraftSampleSplit[]).map(async key => {
    const result = await datasetApi.listDraftSamples(draftId.value, { page: 1, page_size: 1, split: key })
    counts[key] = result.total
  }))
}

const refresh = () => loadSamples()
const changeFilter = () => { page.value = 1; loadSamples() }
const onSelectionChange = (rows: DatasetDraftSample[]) => { selectedIds.value = rows.map(row => row.id) }
const assign = async (split: DraftSampleSplit) => {
  try {
    await datasetApi.assignDraftSamples(draftId.value, selectedIds.value, split)
    selectedIds.value = []
    ElMessage.success(`已更新 ${splitLabel(split)} 划分`)
    await loadSamples()
  } catch (error: any) { ElMessage.error(error.message || '更新划分失败') }
}

const repairSecondary = async (sample: any, event: Event) => {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  const form = new FormData()
  form.append('file', file)
  if (sample.secondary_frame_index != null) form.append('frame_index', String(sample.secondary_frame_index))
  if (sample.secondary_timestamp != null) form.append('timestamp', String(sample.secondary_timestamp))
  try {
    await datasetApi.repairDraftSecondary(draftId.value, sample.id, form)
    ElMessage.success(`已补充帧 ${sample.frame_index ?? ''} 的 X 模态图片`)
    await loadSamples()
  } catch (error: any) { ElMessage.error(error.message || '补充图片失败') }
  finally { input.value = '' }
}

const finalize = async () => {
  try {
    await ElMessageBox.confirm('划分完成后将生成正式数据集 ZIP。请确认训练集和验证集均已人工检查。', '确认完成划分', { type: 'warning', confirmButtonText: '生成正式数据集', cancelButtonText: '继续检查' })
    finalizing.value = true
    await datasetApi.finalizeDraft(draftId.value)
    ElMessage.success('正式数据集已生成')
    await router.push({ name: 'DatasetManagement' })
  } catch (error: any) {
    if (error !== 'cancel') ElMessage.error(error.response?.data?.msg || error.message || '生成正式数据集失败')
  } finally { finalizing.value = false }
}

const classNames = (sample: any) => [...new Set((sample.annotations || []).map((annotation: Record<string, unknown>) => String(annotation.class_name || annotation.class_id)).filter(Boolean))].join('、')
const splitLabel = (split: DraftSampleSplit) => ({ unassigned: '未分配', train: '训练集', val: '验证集', test: '测试集', excluded: '已排除' }[split])
watch(draftId, () => { page.value = 1; loadSamples() })
onMounted(loadSamples)
onBeforeUnmount(revokePreviews)
</script>

<style scoped>
.draft-split-page { padding: 20px; }
.page-header { display: flex; justify-content: space-between; align-items: center; gap: 20px; margin-bottom: 18px; }
.page-header h2 { margin: 8px 0; }
.page-header p { color: #777; margin: 0; }
.split-summary { display: grid; grid-template-columns: repeat(6, minmax(110px, 1fr)); gap: 12px; margin: 18px 0; }
.split-summary :deep(.el-card__body) { display: flex; justify-content: space-between; align-items: center; }
.split-summary strong { font-size: 22px; }
.warning-card { color: #c45656; }
.toolbar { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; margin: 18px 0 12px; }
.previews { display: flex; gap: 8px; }
.previews :deep(.el-image), .preview-placeholder { width: 112px; height: 76px; border-radius: 4px; background: #f3f4f6; object-fit: cover; }
.preview-placeholder { display: inline-flex; justify-content: center; align-items: center; color: #777; }
.preview-placeholder.missing { color: #c45656; background: #fef0f0; }
.class-list { color: #606266; margin-top: 4px; }
small { display: block; color: #909399; margin-top: 4px; }
.upload-label { color: #409eff; cursor: pointer; }
.upload-label.disabled { color: #aaa; cursor: not-allowed; }
.upload-label input { display: none; }
.pagination { display: flex; justify-content: flex-end; margin-top: 18px; }
@media (max-width: 900px) { .split-summary { grid-template-columns: repeat(3, 1fr); } .page-header { align-items: flex-start; flex-direction: column; } }
</style>
