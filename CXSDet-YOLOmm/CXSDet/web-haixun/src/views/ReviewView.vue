<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useWorkspaceStore } from '@/stores/workspace'
import AnnotationCanvas from '@/components/AnnotationCanvas.vue'
import { getAdapter } from '@/services'
import { ApiError } from '@/services/http'
import type { HumanReviewAnnotation } from '@/services'
import type { BoundingBox, DetectionRecordSummary } from '@/types'

const route = useRoute()
const router = useRouter()
const workspace = useWorkspaceStore()

// 真实记录队列：只放"待人工复核"的记录，状态与版本都来自后端
const records = ref<DetectionRecordSummary[]>([])
const currentIdx = ref(0)
const loading = ref(false)
const loadError = ref('')

const currentRecord = computed<DetectionRecordSummary | null>(
  () => records.value[currentIdx.value] ?? null,
)

/** 供模板使用的稳定视图（字段名沿用旧页面，避免模板大改） */
const currentDefect = computed(() => {
  const r = currentRecord.value
  if (!r) return null
  const top = [...r.detections].sort((a, b) => b.confidence - a.confidence)[0]
  return {
    id: r.id,
    turbineId: r.title || r.id.slice(0, 8),
    bladeFile: fileNameOf(r.original_image_url),
    suspectedType: categoryLabel(top?.class_name ?? ''),
    confidence: top?.confidence ?? 0,
    reviewStatus: r.human_review_status,
  }
})

const queueTotal = computed(() => records.value.length)

// 标注状态（本地 x/y/w/h 归一化；后端是 [x1,y1,x2,y2]）
const boxes = ref<BoundingBox[]>([])
const selectedBoxId = ref<string | null>(null)
const mode = ref<'draw' | 'select'>('select')
const zoom = ref(1)
const noDefect = ref(false)
const viewMode = ref<'original' | 'result' | 'compare'>('original')
const undoStack = ref<BoundingBox[][]>([])

const submitting = ref(false)
const notice = ref('')
const noticeType = ref<'success' | 'error' | ''>('')
const expectedVersion = ref(0)

// 真实类别表（来自 /api/model/classes），不再用风电示例类别
const categories = ref<Array<{ id: number; name: string; label: string; color?: string }>>([])
const editCategory = ref('')
const editSeverity = ref<'low' | 'medium' | 'high'>('medium')

const defaultCategory = computed(() => editCategory.value || categories.value[0]?.name || '')

function categoryLabel(name: string): string {
  const hit = categories.value.find((c) => c.name === name)
  return hit ? hit.label : name || '未分类'
}

function fileNameOf(url: string): string {
  if (!url) return ''
  const clean = url.split('?')[0]
  return decodeURIComponent(clean.split('/').pop() || clean)
}

/** 受保护图片统一走同源 /api/file，靠文件会话 cookie 鉴权 */
function assetUrl(url: string): string {
  if (!url) return ''
  const idx = url.indexOf('/api/')
  return idx >= 0 ? url.slice(idx) : url
}

const originalImage = computed(() => assetUrl(currentRecord.value?.original_image_url ?? ''))
const resultImage = computed(() => assetUrl(currentRecord.value?.result_image_url ?? ''))

// --------------------------------------------------------- 坐标转换
// 三套坐标必须区分清楚：
// 1) 检测结果 detection_result[].bbox —— [x1,y1,x2,y2] **像素坐标**
// 2) 人工标注接口 annotations[].bbox —— [x1,y1,x2,y2] **0-1 归一化**
// 3) 画布本地 BoundingBox —— 左上角 + 宽高，0-1 归一化
// 所以读检测框要先按原图尺寸归一化，提交时再把宽高加回右下角。
const naturalSize = ref({ w: 0, h: 0 })

function measureImage(url: string): Promise<{ w: number; h: number }> {
  if (!url) return Promise.resolve({ w: 0, h: 0 })
  return new Promise((resolve) => {
    const probe = new Image()
    probe.onload = () => resolve({ w: probe.naturalWidth || 0, h: probe.naturalHeight || 0 })
    probe.onerror = () => resolve({ w: 0, h: 0 })
    probe.src = url
  })
}

/** 像素坐标按原图尺寸归一化；已经是 0-1 的原样返回 */
function normalizeBbox(bbox: number[], size: { w: number; h: number }): number[] {
  const [x1 = 0, y1 = 0, x2 = 0, y2 = 0] = bbox.map((v) => Number(v) || 0)
  const maxValue = Math.max(Math.abs(x1), Math.abs(y1), Math.abs(x2), Math.abs(y2))
  if (maxValue <= 1.0001) return [x1, y1, x2, y2]
  const w = size.w || 1
  const h = size.h || 1
  const clamp = (v: number) => Math.min(1, Math.max(0, v))
  return [clamp(x1 / w), clamp(y1 / h), clamp(x2 / w), clamp(y2 / h)]
}

function remoteToLocal(bbox: number[]): { x: number; y: number; w: number; h: number } {
  const [x1 = 0, y1 = 0, x2 = 0, y2 = 0] = bbox
  return { x: x1, y: y1, w: Math.max(0, x2 - x1), h: Math.max(0, y2 - y1) }
}

function localToRemote(b: BoundingBox): [number, number, number, number] {
  return [b.x, b.y, b.x + b.w, b.y + b.h]
}

function parseStoredAnnotations(raw: unknown): HumanReviewAnnotation[] {
  if (!Array.isArray(raw)) return []
  return raw
    .filter((item) => item && typeof item === 'object')
    .map((item) => {
      const obj = item as Record<string, unknown>
      const bbox = Array.isArray(obj.bbox) ? (obj.bbox as number[]) : []
      return {
        class_name: String(obj.class_name ?? ''),
        class_id: Number(obj.class_id ?? obj.class ?? 0),
        bbox: [Number(bbox[0] ?? 0), Number(bbox[1] ?? 0), Number(bbox[2] ?? 0), Number(bbox[3] ?? 0)] as [
          number,
          number,
          number,
          number,
        ],
      }
    })
}

/** 载入当前记录：优先用上次人工标注，否则用真实检测框（像素坐标需归一化） */
const annotationLoading = ref(false)
let annotationGeneration = 0
async function loadAnnotation() {
  const token = ++annotationGeneration
  const r = currentRecord.value
  if (!r) {
    boxes.value = []
    categories.value = []
    annotationLoading.value = false
    return
  }
  categories.value = r.class_names.map((name, id) => ({ id, name, label: name }))
  editCategory.value = ''
  annotationLoading.value = true
  // 归一化依赖原图真实尺寸，先量一次
  const size = await measureImage(originalImage.value)
  if (token !== annotationGeneration) return
  naturalSize.value = size
  if (!size.w || !size.h) {
    boxes.value = []; annotationLoading.value = false
    loadError.value = '原图加载失败，无法可靠核对标注坐标，请检查文件访问后刷新。'
    return
  }

  const stored = parseStoredAnnotations(r.corrected_annotations)
  const source: HumanReviewAnnotation[] = stored.length || r.human_review_status === 'confirmed'
    ? stored
    : r.detections.map((d, index) => ({
        class_name: d.class_name,
        class_id: d.class || index,
        bbox: normalizeBbox(
          [d.bbox[0], d.bbox[1], d.bbox[2], d.bbox[3]].map((v) => Number(v) || 0),
          naturalSize.value,
        ) as [number, number, number, number],
      }))

  boxes.value = source.map((item, index) => {
    const { x, y, w, h } = remoteToLocal(item.bbox)
    return {
      id: `box-${r.id}-${index}`,
      x,
      y,
      w,
      h,
      category: item.class_name,
      label: categoryLabel(item.class_name),
      severity: 'medium' as const,
    }
  })
  noDefect.value = r.human_review_status === 'no_defect' || (r.human_review_status === 'confirmed' && !stored.length)
  expectedVersion.value = r.review_version ?? 0
  selectedBoxId.value = null
  undoStack.value = []
  zoom.value = 1
  annotationLoading.value = false
}

watch(currentRecord, loadAnnotation)

async function loadQueue() {
  loading.value = true
  loadError.value = ''
  try {
    const page = await getAdapter().listRecords({ page: 1, pageSize: 50 })
    // 已人工确认或无缺陷的不再进入待复核队列
    records.value = page.items.filter(
      (r) => r.human_review_status !== 'confirmed' && r.human_review_status !== 'no_defect',
    )
    const requestedId = route.params.defectId
    if (typeof requestedId === 'string' && !records.value.some(r => r.id === requestedId)) {
      const requested = await getAdapter().getRecord(requestedId)
      if (requested) records.value.unshift(requested)
    }
    if (currentIdx.value >= records.value.length) currentIdx.value = 0
    loadAnnotation()
  } catch (e) {
    loadError.value = '读取复核队列失败：' + (e as Error).message
    records.value = []
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  await loadQueue()
  const defectId = route.params.defectId as string | undefined
  if (defectId) {
    const idx = records.value.findIndex((r) => r.id === defectId)
    if (idx >= 0) currentIdx.value = idx
  }
})

function pushUndo() {
  undoStack.value.push(JSON.parse(JSON.stringify(boxes.value)))
}

function onDraw(box: BoundingBox) {
  noDefect.value = false
  pushUndo()
  boxes.value.push(box)
  selectedBoxId.value = box.id
}

function onUpdateBoxes(newBoxes: BoundingBox[]) {
  boxes.value = newBoxes
}

function selectBox(id: string | null) {
  selectedBoxId.value = id
  if (id) {
    const b = boxes.value.find((x) => x.id === id)
    if (b) {
      editCategory.value = b.category
      editSeverity.value = b.severity
    }
  }
}

function undo() {
  if (!undoStack.value.length) return
  boxes.value = undoStack.value.pop()!
  if (boxes.value.length) noDefect.value = false
}

function deleteSelected() {
  if (!selectedBoxId.value) return
  pushUndo()
  boxes.value = boxes.value.filter((b) => b.id !== selectedBoxId.value)
  selectedBoxId.value = null
}

function applyEdit() {
  if (!selectedBoxId.value) return
  boxes.value = boxes.value.map((b) =>
    b.id === selectedBoxId.value ? { ...b, category: editCategory.value, severity: editSeverity.value } : b,
  )
}

function toggleNoDefect() {
  if (noDefect.value) { pushUndo(); boxes.value = [] }
}

function changeZoom(delta: number) {
  zoom.value = Math.max(0.5, Math.min(3, zoom.value + delta))
}

function fitWindow() {
  zoom.value = 1
}

/** 提交到后端：带真实 expected_version，服务端成功后才更新界面 */
async function submit(decisionOverride?: 'confirmed' | 'no_defect' | 'rejected') {
  const r = currentRecord.value
  if (!r || submitting.value) return

  let decision: 'confirmed' | 'no_defect' | 'rejected'
  if (decisionOverride) {
    decision = decisionOverride
  } else if (noDefect.value) {
    decision = 'no_defect'
  } else if (boxes.value.length) {
    decision = 'confirmed'
  } else {
    notice.value = '请先补框，或勾选「确认无缺陷」后再提交。'
    noticeType.value = 'error'
    return
  }

  // 类别 id 必须与类别名对应，不能拿上一套数据的 id 硬凑
  if (annotationLoading.value || !naturalSize.value.w || !naturalSize.value.h) {
    notice.value = '请等待原图正确加载后再提交。'; noticeType.value = 'error'; return
  }
  if (decision === 'confirmed' && boxes.value.some(b => !categories.value.some(c => c.name === b.category))) {
    notice.value = '类别不在本记录所属数据集的类别表中，请先核对数据集和类别。'; noticeType.value = 'error'; return
  }
  const annotations: HumanReviewAnnotation[] = boxes.value.map((b) => {
    const cat = categories.value.find((c) => c.name === b.category)
    return {
      class_name: b.category,
      class_id: cat?.id ?? -1,
      bbox: localToRemote(b),
    }
  })

  submitting.value = true
  notice.value = ''
  try {
    const result = await getAdapter().submitHumanReview(r.id, {
      decision,
      annotations: decision === 'confirmed' ? annotations : [],
      expectedVersion: expectedVersion.value,
    })
    noticeType.value = 'success'
    notice.value =
      decision === 'rejected'
        ? '已驳回该检测结果，不计入确认样本。'
        : decision === 'no_defect'
          ? '已提交「确认无缺陷」。'
          : `已提交 ${annotations.length} 个标注框。`
    const version = (result as Record<string, unknown>).review_version
    if (typeof version === 'number') expectedVersion.value = version

    // 服务端成功后才刷新队列；刷新会重建标注，但不清掉本次结果提示
    const message = notice.value
    await loadQueue()
    notice.value = message
    noticeType.value = 'success'
  } catch (e) {
    noticeType.value = 'error'
    if (e instanceof ApiError && e.httpStatus === 409) {
      // 版本冲突：重新拉取后再让人确认，不覆盖别人的提交
      notice.value = '该记录已被其他人更新，已重新加载最新版本，请再次核对后提交。'
      await loadQueue()
    } else {
      notice.value = '提交失败，修改仍在本地：' + (e as Error).message
    }
  } finally {
    submitting.value = false
  }
}

function clearNotice() {
  notice.value = ''
  noticeType.value = ''
}

function nextItem() {
  if (currentIdx.value < records.value.length - 1) {
    currentIdx.value++
    clearNotice()
  }
}

function prevItem() {
  if (currentIdx.value > 0) {
    currentIdx.value--
    clearNotice()
  }
}
</script>

<template>
  <div class="page review-page">
    <!-- 面包屑 -->
    <div class="breadcrumb">
      <span>人工复核</span>
      <span class="sep">/</span>
      <span class="current">{{ workspace.current?.name }}</span>
      <span class="sep">/</span>
      <span class="current">{{ currentDefect?.turbineId ?? '—' }}</span>
    </div>

    <div v-if="!currentDefect" class="panel empty">
      <div class="empty-title">{{ loading ? '正在读取复核队列…' : loadError ? '复核队列读取失败' : '暂无可复核项' }}</div>
      <div class="empty-desc">
        {{ loadError || '当前项目没有待人工复核的识别记录；检测完成后会自动进入此队列。' }}
      </div>
      <button class="btn" @click="router.push('/records')">查看识别记录</button>
      <button v-if="loadError" class="btn" @click="loadQueue">重试</button>
    </div>

    <template v-else>
      <div class="review-header">
        <div class="review-info">
          <h1 class="page-title">人工复核</h1>
          <div class="review-path">
            <span class="tag">{{ currentDefect.turbineId }}</span>
            <span class="file-name">{{ currentDefect.bladeFile }}</span>
            <span class="tag" :class="noDefect ? 'tag-success' : ''">rev {{ expectedVersion }}</span>
          </div>
        </div>
        <div class="review-nav">
          <span class="num">{{ currentIdx + 1 }} / {{ queueTotal }}</span>
          <button class="btn btn-sm" :disabled="currentIdx === 0" @click="prevItem">上一项</button>
          <button class="btn btn-sm" :disabled="currentIdx >= queueTotal - 1" @click="nextItem">下一项</button>
        </div>
      </div>

      <div class="review-body">
        <!-- 主图区 -->
        <div class="canvas-area panel">
          <div class="canvas-toolbar">
            <div class="toolbar-group">
              <button class="tool-btn" :class="{ active: viewMode === 'original' }" @click="viewMode = 'original'">原图</button>
              <button class="tool-btn" :class="{ active: viewMode === 'result' }" @click="viewMode = 'result'">结果</button>
              <button class="tool-btn" :class="{ active: viewMode === 'compare' }" @click="viewMode = 'compare'">对照</button>
            </div>
            <div class="toolbar-group">
              <button class="tool-btn" @click="changeZoom(-0.25)" aria-label="缩小">−</button>
              <span class="zoom-val num">{{ Math.round(zoom * 100) }}%</span>
              <button class="tool-btn" @click="changeZoom(0.25)" aria-label="放大">＋</button>
              <button class="tool-btn" @click="fitWindow">适应窗口</button>
            </div>
            <div class="toolbar-group">
              <button class="tool-btn" :class="{ active: mode === 'select' }" @click="mode = 'select'">选择</button>
              <button class="tool-btn" :class="{ active: mode === 'draw' }" @click="mode = 'draw'">重绘框</button>
              <button class="tool-btn" :disabled="!selectedBoxId" @click="deleteSelected">删除框</button>
              <button class="tool-btn" :disabled="!undoStack.length" @click="undo">撤销</button>
            </div>
          </div>
          <div class="canvas-scroll">
            <AnnotationCanvas
              v-if="viewMode !== 'result'"
              :boxes="boxes"
              :image-url="originalImage"
              :zoom="zoom"
              :mode="mode"
              :selected-box-id="selectedBoxId"
              :default-category="defaultCategory"
              @draw="onDraw"
              @select="selectBox"
              @update:boxes="onUpdateBoxes"
            />
            <div v-else class="result-preview">
              <img v-if="resultImage" :src="resultImage" alt="检测结果图" />
              <div v-else class="edit-hint">该记录没有结果图。</div>
            </div>
          </div>
          <div v-if="viewMode === 'compare'" class="compare-strip">
            <span class="edit-hint">当前显示原图（可编辑标注）；结果图见「结果」</span>
          </div>
          <div v-if="noDefect" class="no-defect-banner">已标记为「确认无缺陷」</div>
        </div>

        <!-- 右侧编辑面板 -->
        <aside class="edit-panel">
          <div class="panel edit-card">
            <h3 class="edit-title">缺陷信息</h3>
            <div class="field">
              <span class="field-label">疑似缺陷</span>
              <div class="defect-type">{{ currentDefect.suspectedType }}</div>
            </div>
            <div class="field">
              <span class="field-label">置信度</span>
              <div class="num">{{ (currentDefect.confidence * 100).toFixed(0) }}%</div>
            </div>

            <template v-if="selectedBoxId">
              <div class="field">
                <label class="field-label" for="cat">类别</label>
                <select id="cat" v-model="editCategory" class="select">
                  <option v-for="c in categories" :key="c.name" :value="c.name">{{ c.label }}</option>
                  <option v-if="!categories.length" value="">（后端未提供类别表）</option>
                </select>
              </div>
              <div class="field">
                <label class="field-label" for="sev">严重程度</label>
                <select id="sev" v-model="editSeverity" class="select">
                  <option value="low">低</option>
                  <option value="medium">中</option>
                  <option value="high">高</option>
                </select>
              </div>
              <button class="btn btn-primary btn-sm" @click="applyEdit">应用修改</button>
            </template>
            <div v-else class="edit-hint">选择一个检测框可修改类别与严重程度，或切换到「重绘框」补框。</div>

            <div class="edit-divider"></div>

            <label class="no-defect-check">
              <input type="checkbox" v-model="noDefect" @change="toggleNoDefect" />
              <span>确认无缺陷</span>
            </label>

            <div class="edit-actions">
              <button class="btn btn-primary" :disabled="submitting" @click="submit()">
                {{ submitting ? '提交中…' : '提交' }}
              </button>
              <button class="btn" :disabled="submitting" @click="submit('rejected')">驳回该结果</button>
            </div>
            <div class="edit-hint">
              提交会带上版本号 rev {{ expectedVersion }}；记录被别人更新时会拒绝覆盖并重新加载。
            </div>
          </div>

          <div class="panel tips-card">
            <h4 class="tips-title">操作提示</h4>
            <ul class="tips-list">
              <li>「重绘框」在图上拖拽补框，框坐标使用原图归一化坐标。</li>
              <li>「选择」模式可拖动已有框调整位置。</li>
              <li>确认无缺陷提交后，刷新或返回不会恢复旧框。</li>
              <li>提交失败会保留草稿，可重试。</li>
            </ul>
          </div>
        </aside>
      </div>

      <div v-if="notice" class="notice submit-notice" :class="noticeType === 'error' ? 'notice-error' : 'notice-success'">
        {{ notice }}
      </div>
    </template>
  </div>
</template>

<style scoped>
.page {
  padding: var(--content-pad);
  max-width: 1200px;
  margin: 0 auto;
}
.breadcrumb {
  font-size: 12px;
  color: var(--text-secondary);
  margin-bottom: 12px;
  display: flex;
  gap: 6px;
  align-items: center;
}
.breadcrumb .sep {
  color: var(--border-control);
}
.breadcrumb .current {
  color: var(--text);
}
.review-header {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  margin-bottom: 16px;
}
.page-title {
  font-size: 24px;
  font-weight: 600;
  margin: 0 0 8px;
}
.review-path {
  display: flex;
  align-items: center;
  gap: 8px;
}
.file-name {
  font-size: 13px;
  color: var(--text-secondary);
}
.review-nav {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--text-secondary);
}

.review-body {
  display: flex;
  gap: 16px;
  align-items: flex-start;
}
.canvas-area {
  flex: 1;
  min-width: 0;
  overflow: hidden;
}
.canvas-toolbar {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
  flex-wrap: wrap;
}
.toolbar-group {
  display: flex;
  align-items: center;
  gap: 4px;
}
.tool-btn {
  height: 28px;
  padding: 0 10px;
  border: 1px solid transparent;
  background: transparent;
  color: var(--text-secondary);
  font-size: 12px;
  border-radius: var(--radius-sm);
  cursor: pointer;
}
.tool-btn:hover {
  background: var(--bg-selected);
  color: var(--text);
}
.tool-btn.active {
  background: var(--bg-selected);
  color: var(--text);
  font-weight: 500;
}
.tool-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.zoom-val {
  font-size: 12px;
  min-width: 48px;
  text-align: center;
}
.canvas-scroll {
  overflow: auto;
  padding: 12px;
  max-height: 70vh;
}
.no-defect-banner {
  text-align: center;
  padding: 8px;
  font-size: 13px;
  color: var(--success);
  background: color-mix(in srgb, var(--success) 10%, transparent);
}

.edit-panel {
  width: 280px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.edit-card {
  padding: 16px;
}
.edit-title {
  font-size: 14px;
  font-weight: 600;
  margin: 0 0 12px;
}
.defect-type {
  font-size: 14px;
  font-weight: 500;
}
.edit-hint {
  font-size: 12px;
  color: var(--text-secondary);
  line-height: 1.5;
}
.edit-divider {
  height: 1px;
  background: var(--border);
  margin: 14px 0;
}
.no-defect-check {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  cursor: pointer;
  margin-bottom: 14px;
}
.edit-actions {
  display: flex;
}
.edit-actions .btn {
  flex: 1;
}
.tips-card {
  padding: 14px 16px;
}
.tips-title {
  font-size: 13px;
  font-weight: 600;
  margin: 0 0 8px;
}
.tips-list {
  margin: 0;
  padding-left: 16px;
  font-size: 12px;
  color: var(--text-secondary);
  line-height: 1.6;
}
.submit-notice {
  margin-top: 16px;
}

@media (max-width: 768px) {
  .review-body {
    flex-direction: column;
  }
  .edit-panel {
    width: 100%;
  }
  .review-header {
    flex-direction: column;
    align-items: flex-start;
    gap: 8px;
  }
}
.result-preview img {
  display: block;
  max-width: 100%;
  height: auto;
}
.compare-strip {
  padding: 6px 12px;
  border-top: 1px solid var(--border);
}
.edit-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
</style>
