<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useInspectionStore } from '@/stores/inspection'
import { useWorkspaceStore } from '@/stores/workspace'
import AnnotationCanvas from '@/components/AnnotationCanvas.vue'
import type { BoundingBox } from '@/types'
import { mockDefectTypes, mockSeverities } from '@/services/mock-data'
import { bladeImageFor } from '@/assets/images'

const route = useRoute()
const router = useRouter()
const inspection = useInspectionStore()
const workspace = useWorkspaceStore()

const queue = computed(() => inspection.defects.filter((d) => d.reviewStatus !== 'done'))
const currentIdx = ref(0)

const currentDefect = computed(() => queue.value[currentIdx.value] ?? null)
const currentImageId = computed(() => currentDefect.value?.id ?? '')

// 标注状态
const boxes = ref<BoundingBox[]>([])
const selectedBoxId = ref<string | null>(null)
const mode = ref<'draw' | 'select'>('select')
const zoom = ref(1)
const noDefect = ref(false)
const viewMode = ref<'original' | 'result' | 'compare'>('original')
const undoStack = ref<BoundingBox[][]>([])

// 提交状态
const submitting = ref(false)
const notice = ref('')
const noticeType = ref<'success' | 'error' | ''>('')

// 类别/严重程度编辑
const editCategory = ref('')
const editSeverity = ref<'low' | 'medium' | 'high'>('medium')

// 从 store 载入当前影像的标注（切换影像不串草稿）
function loadAnnotation() {
  const id = currentImageId.value
  if (!id) return
  const ann = inspection.getAnnotation(id)
  boxes.value = JSON.parse(JSON.stringify(ann.boxes))
  noDefect.value = ann.noDefect
  selectedBoxId.value = null
  undoStack.value = []
  zoom.value = 1
  notice.value = ''
  noticeType.value = ''
}

watch(currentImageId, loadAnnotation)

// 从路由缺陷 id 定位
onMounted(() => {
  const defectId = route.params.defectId as string | undefined
  if (defectId) {
    const idx = queue.value.findIndex((d) => d.id === defectId)
    if (idx >= 0) currentIdx.value = idx
  }
  loadAnnotation()
})

function pushUndo() {
  undoStack.value.push(JSON.parse(JSON.stringify(boxes.value)))
}

function onDraw(box: BoundingBox) {
  pushUndo()
  boxes.value.push(box)
  selectedBoxId.value = box.id
  persist()
}

function onUpdateBoxes(newBoxes: BoundingBox[]) {
  boxes.value = newBoxes
  persist()
}

function persist() {
  inspection.setAnnotation(currentImageId.value, {
    imageId: currentImageId.value,
    boxes: boxes.value,
    noDefect: noDefect.value,
  })
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
  persist()
}

function deleteSelected() {
  if (!selectedBoxId.value) return
  pushUndo()
  boxes.value = boxes.value.filter((b) => b.id !== selectedBoxId.value)
  selectedBoxId.value = null
  persist()
}

function applyEdit() {
  if (!selectedBoxId.value) return
  boxes.value = boxes.value.map((b) =>
    b.id === selectedBoxId.value ? { ...b, category: editCategory.value, severity: editSeverity.value } : b,
  )
  persist()
}

function toggleNoDefect() {
  noDefect.value = !noDefect.value
  if (noDefect.value) boxes.value = []
  persist()
}

function changeZoom(delta: number) {
  zoom.value = Math.max(0.5, Math.min(3, zoom.value + delta))
}

function fitWindow() {
  zoom.value = 1
}

async function submit() {
  if (!currentDefect.value) return
  if (submitting.value) return // 防重复提交
  submitting.value = true
  notice.value = ''
  try {
    // 模拟后端提交（真实模式需后端返回成功才标记完成）
    await new Promise((r) => setTimeout(r, 600))
    // 更新缺陷状态
    const d = inspection.defects.find((x) => x.id === currentDefect.value!.id)
    if (d) d.reviewStatus = 'done'
    notice.value = '提交成功，已通知助手接力处理。'
    noticeType.value = 'success'
    // 下一项
    setTimeout(() => {
      if (currentIdx.value < queue.value.length - 1) {
        currentIdx.value++
      } else {
        router.push('/review')
      }
      notice.value = ''
    }, 800)
  } catch (e) {
    notice.value = '提交失败，草稿已保留，可重试。' + (e as Error).message
    noticeType.value = 'error'
  } finally {
    submitting.value = false
  }
}

function nextItem() {
  if (currentIdx.value < queue.value.length - 1) currentIdx.value++
}

function prevItem() {
  if (currentIdx.value > 0) currentIdx.value--
}

// 演示影像（模拟叶片照片 SVG）
// 演示影像：使用真实叶片照片（接入后端后由实际影像 URL 替换）
const demoImage = computed(() => {
  const id = currentDefect.value?.turbineId ?? 'F00'
  return bladeImageFor(id)
})
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
      <div class="empty-title">暂无可复核项</div>
      <div class="empty-desc">当前项目没有待复核的缺陷，识别结果处理完毕后会进入此队列。</div>
      <button class="btn" @click="router.push('/records')">查看识别记录</button>
    </div>

    <template v-else>
      <div class="review-header">
        <div class="review-info">
          <h1 class="page-title">人工复核</h1>
          <div class="review-path">
            <span class="tag">{{ currentDefect.turbineId }}</span>
            <span class="file-name">{{ currentDefect.bladeFile }}</span>
          </div>
        </div>
        <div class="review-nav">
          <span class="num">{{ currentIdx + 1 }} / {{ queue.length }}</span>
          <button class="btn btn-sm" :disabled="currentIdx === 0" @click="prevItem">上一项</button>
          <button class="btn btn-sm" :disabled="currentIdx >= queue.length - 1" @click="nextItem">下一项</button>
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
              :boxes="boxes"
              :image-url="demoImage"
              :zoom="zoom"
              :mode="mode"
              :selected-box-id="selectedBoxId"
              @draw="onDraw"
              @select="selectBox"
              @update:boxes="onUpdateBoxes"
            />
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
                  <option v-for="t in mockDefectTypes" :key="t" :value="t">{{ t }}</option>
                </select>
              </div>
              <div class="field">
                <label class="field-label" for="sev">严重程度</label>
                <select id="sev" v-model="editSeverity" class="select">
                  <option v-for="s in mockSeverities" :key="s.value" :value="s.value">{{ s.label }}</option>
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
              <button class="btn btn-primary" :disabled="submitting" @click="submit">
                {{ submitting ? '提交中…' : '提交' }}
              </button>
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
</style>
