<script setup lang="ts">
import { ref, computed } from 'vue'
import { useRouter } from 'vue-router'
import { useWorkspaceStore } from '@/stores/workspace'
import { useInspectionStore } from '@/stores/inspection'
import { getAdapter } from '@/services'
import type { AgentTask } from '@/types'
import { IMAGES } from '@/assets/images'

const router = useRouter()
const workspace = useWorkspaceStore()
const inspection = useInspectionStore()

type ImageItem = { id: string; name: string; url: string; size: number; source: 'user' | 'sample' }

const images = ref<ImageItem[]>([])
const selectedModel = ref<string>('')
const dragging = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)

// 计划状态
const task = ref<AgentTask | null>(null)
const planGenerated = ref(false)
const confirmState = ref<'idle' | 'submitting' | 'running' | 'done' | 'failed' | 'cancelled'>('idle')
const notice = ref('')

const MAX_SIZE = 100 * 1024 * 1024 // 100 MB

const canConfirm = computed(() => images.value.length > 0 && selectedModel.value !== '')

function onFileSelect(e: Event) {
  const files = (e.target as HTMLInputElement).files
  if (files) addFiles(Array.from(files))
  ;(e.target as HTMLInputElement).value = ''
}

function addFiles(files: File[]) {
  for (const f of files) {
    if (f.size > MAX_SIZE) {
      notice.value = `文件 ${f.name} 超过单文件上限 100 MB，已跳过。`
      continue
    }
    if (!/\.(jpe?g|png|webp)$/i.test(f.name)) {
      notice.value = `文件 ${f.name} 格式不支持（仅 JPG/PNG/WEBP），已跳过。`
      continue
    }
    const url = URL.createObjectURL(f)
    images.value.push({ id: 'img-' + Date.now() + Math.random(), name: f.name, url, size: f.size, source: 'user' })
  }
}

function onDrop(e: DragEvent) {
  e.preventDefault()
  dragging.value = false
  const files = e.dataTransfer?.files
  if (files) addFiles(Array.from(files))
}

function onPaste(e: ClipboardEvent) {
  const items = e.clipboardData?.items
  if (!items) return
  for (const item of items) {
    if (item.type.startsWith('image/')) {
      const f = item.getAsFile()
      if (f) addFiles([f])
    }
  }
}

function addSamples() {
  // 示例影像：使用真实叶片照片（演示占位，接入后端后由上传影像替换）
  const sampleNames = ['F01_A_叶片前缘.jpg', 'F03_B_叶片表面.jpg', 'F07_C_叶片涂层.jpg']
  const samplePool = [IMAGES.blade, IMAGES.turbine, IMAGES.scene]
  for (let i = 0; i < sampleNames.length; i++) {
    images.value.push({
      id: 'img-' + Date.now() + Math.random(),
      name: sampleNames[i],
      url: samplePool[i % samplePool.length],
      size: 0,
      source: 'sample',
    })
  }
}

function removeImage(id: string) {
  images.value = images.value.filter((i) => i.id !== id)
}

function formatSize(n: number) {
  if (n === 0) return '示例'
  if (n < 1024 * 1024) return (n / 1024).toFixed(0) + ' KB'
  return (n / 1024 / 1024).toFixed(1) + ' MB'
}

// 生成计划
async function generatePlan() {
  if (!canConfirm.value) return
  notice.value = ''
  confirmState.value = 'submitting'
  try {
    task.value = await getAdapter().createTask({
      project_id: workspace.currentId || 'ws-default',
      session_id: 'plan',
      images: images.value.map((i) => i.name),
      model_id: selectedModel.value,
    })
    planGenerated.value = true
    confirmState.value = 'idle'
  } catch (e) {
    notice.value = '生成计划失败：' + (e as Error).message
    confirmState.value = 'failed'
  }
}

// 执行（确认后）
async function confirmRun() {
  if (!task.value) return
  if (confirmState.value === 'running' || confirmState.value === 'submitting') return // 防双击
  confirmState.value = 'running'
  notice.value = ''
  // 模拟执行步骤
  for (let i = 0; i < 4; i++) {
    await sleep(600)
    if (task.value) task.value.steps[i].status = 'done'
  }
  confirmState.value = 'done'
  notice.value = '检测完成，结果已进入人工复核队列。'
}

async function rejectPlan() {
  if (!task.value) return
  task.value.status = 'stopped'
  confirmState.value = 'cancelled'
  notice.value = '已拒绝该计划，任务未执行。'
}

function sleep(ms: number) {
  return new Promise((r) => setTimeout(r, ms))
}

function goReview() {
  router.push('/review')
}

function reset() {
  task.value = null
  planGenerated.value = false
  confirmState.value = 'idle'
  notice.value = ''
}
</script>

<template>
  <div class="page">
    <div class="page-header">
      <h1 class="page-title">图片巡检</h1>
      <div class="header-meta">
        <span class="tag">交互原型 · 模拟数据</span>
      </div>
    </div>

    <!-- 上传区 -->
    <div class="section">
      <h2 class="section-title">选择影像</h2>
      <div
        class="dropzone panel"
        :class="{ dragging }"
        @drop="onDrop"
        @dragover.prevent="dragging = true"
        @dragleave="dragging = false"
        @paste="onPaste"
      >
        <div class="dropzone-inner">
          <svg viewBox="0 0 24 24" width="28" height="28" fill="none" stroke="currentColor" stroke-width="1.5">
            <rect x="3" y="3" width="18" height="18" rx="2" />
            <circle cx="8.5" cy="8.5" r="1.5" />
            <path d="M21 15l-5-5L5 21" />
          </svg>
          <p class="dropzone-text">拖拽或粘贴 JPG / PNG / WEBP 影像到此处，或</p>
          <button class="btn" @click="fileInput?.click()">选择文件</button>
          <button class="btn btn-ghost" @click="addSamples">使用示例影像</button>
          <input ref="fileInput" type="file" multiple hidden accept=".jpg,.jpeg,.png,.webp" @change="onFileSelect" />
          <p class="dropzone-hint">单文件上限 100 MB · 选择不等于已上传，执行前会先核对计划</p>
        </div>
      </div>

      <!-- 影像列表 -->
      <div v-if="images.length" class="image-list">
        <div v-for="img in images" :key="img.id" class="image-item panel">
          <img class="photo" :src="img.url" :alt="img.name" />
          <div class="image-meta">
            <div class="image-name" :title="img.name">{{ img.name }}</div>
            <div class="image-info">
              <span class="tag">{{ img.source === 'sample' ? '示例' : '本地' }}</span>
              <span class="num">{{ formatSize(img.size) }}</span>
            </div>
          </div>
          <button class="btn btn-ghost btn-sm image-remove" aria-label="移除" @click="removeImage(img.id)">✕</button>
        </div>
      </div>
    </div>

    <!-- 模型选择 -->
    <div class="section">
      <h2 class="section-title">检测模型</h2>
      <div class="model-select">
        <label v-for="m in inspection.models.filter((m) => m.status !== 'training')" :key="m.id" class="model-option panel" :class="{ selected: selectedModel === m.id }">
          <input type="radio" name="model" :value="m.id" v-model="selectedModel" class="sr-only" />
          <div class="model-name">{{ m.name }} <span class="mono num">{{ m.version }}</span></div>
          <div class="model-type">
            <span class="tag" :class="m.type === 'system' ? 'tag-accent' : ''">{{ m.type === 'system' ? '系统基线 · 只读' : '个人模型' }}</span>
            <span v-if="m.metrics" class="model-metric num">mAP50 {{ m.metrics.mAP50.toFixed(3) }}</span>
          </div>
        </label>
      </div>
    </div>

    <!-- 计划 / 执行 -->
    <div class="section" v-if="task || notice">
      <h2 class="section-title">检测计划</h2>
      <div v-if="notice" class="notice" :class="confirmState === 'failed' ? 'notice-error' : confirmState === 'done' ? 'notice-success' : ''">
        {{ notice }}
      </div>

      <div v-if="task" class="panel plan-card">
        <div class="plan-row">
          <span class="plan-label">目标项目</span>
          <span class="plan-value">{{ workspace.current?.name }}</span>
        </div>
        <div class="plan-row">
          <span class="plan-label">输入影像</span>
          <span class="plan-value num">{{ images.length }} 张</span>
        </div>
        <div class="plan-row">
          <span class="plan-label">模型版本</span>
          <span class="plan-value mono">{{ task.business_refs.find(b => b.kind === 'model')?.display_name }}</span>
        </div>
        <div class="plan-steps">
          <div class="plan-label">步骤</div>
          <ul class="step-list">
            <li v-for="s in task.steps" :key="s.id" class="step-item" :class="s.status">
              <span class="step-dot" aria-hidden="true"></span>
              <span>{{ s.name }}</span>
              <span class="step-status">{{ s.status === 'done' ? '完成' : s.status === 'pending' ? '待执行' : s.status }}</span>
            </li>
          </ul>
        </div>
        <div class="plan-note">确认执行会使用以上已确认版本的输入，不会静默切换图片、项目或模型。</div>

        <div class="plan-actions" v-if="!planGenerated">
          <button class="btn" :disabled="confirmState === 'submitting'" @click="generatePlan">生成计划</button>
        </div>
        <div class="plan-actions" v-else-if="planGenerated && confirmState === 'idle'">
          <button class="btn btn-primary" @click="confirmRun">确认执行</button>
          <button class="btn" @click="rejectPlan">拒绝</button>
          <button class="btn btn-ghost" @click="generatePlan">调整（重新生成）</button>
        </div>
        <div class="plan-actions" v-else-if="confirmState === 'running'">
          <span class="spinner"></span>
          <span class="running-text">正在执行检测…</span>
          <button class="btn" @click="confirmState = 'cancelled'; notice = '已取消当前步骤，部分结果可能已写入。'">取消当前步骤</button>
        </div>
        <div class="plan-actions" v-else-if="confirmState === 'done'">
          <button class="btn btn-primary" @click="goReview">前往人工复核</button>
          <button class="btn" @click="reset">再次巡检</button>
        </div>
        <div class="plan-actions" v-else-if="confirmState === 'cancelled'">
          <button class="btn" @click="reset">重新开始</button>
        </div>
      </div>
    </div>

    <!-- 底部操作 -->
    <div class="section" v-if="!task">
      <button class="btn btn-primary" :disabled="!canConfirm" @click="generatePlan">
        生成检测计划
      </button>
      <span v-if="!canConfirm" class="hint-text">请先选择至少一张影像并选择检测模型</span>
    </div>
  </div>
</template>

<style scoped>
.page {
  padding: var(--content-pad);
  max-width: 900px;
  margin: 0 auto;
}
.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 20px;
}
.page-title {
  font-size: 24px;
  font-weight: 600;
  margin: 0;
}
.header-meta {
  display: flex;
  gap: 8px;
}
.section {
  margin-bottom: 24px;
}
.section-title {
  font-size: 16px;
  font-weight: 600;
  margin: 0 0 12px;
}

.dropzone {
  border: 1.5px dashed var(--border-control);
  border-radius: var(--radius-md);
  transition: border-color 0.15s, background 0.15s;
}
.dropzone.dragging {
  border-color: var(--accent);
  background: color-mix(in srgb, var(--accent) 5%, transparent);
}
.dropzone-inner {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  padding: 36px 20px;
  color: var(--text-secondary);
}
.dropzone-text {
  margin: 0;
  font-size: 13px;
}
.dropzone-hint {
  margin: 0;
  font-size: 11px;
  color: var(--text-secondary);
}

.image-list {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 12px;
  margin-top: 16px;
}
.image-item {
  position: relative;
  overflow: hidden;
}
.image-item img {
  width: 100%;
  height: 120px;
  object-fit: cover;
  display: block;
}
.image-meta {
  padding: 8px 10px;
}
.image-name {
  font-size: 12px;
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  margin-bottom: 4px;
}
.image-info {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11px;
  color: var(--text-secondary);
}
.image-remove {
  position: absolute;
  top: 6px;
  right: 6px;
  background: var(--panel);
}

.model-select {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 12px;
}
.model-option {
  padding: 14px;
  cursor: pointer;
  border: 1px solid var(--border);
  transition: border-color 0.15s;
}
.model-option.selected {
  border-color: var(--accent);
  box-shadow: var(--focus-ring);
}
.model-name {
  font-size: 14px;
  font-weight: 600;
  margin-bottom: 8px;
}
.model-type {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.model-metric {
  font-size: 12px;
  color: var(--text-secondary);
}

.plan-card {
  padding: 16px;
}
.plan-row {
  display: flex;
  gap: 12px;
  padding: 8px 0;
  border-bottom: 1px solid var(--border);
}
.plan-label {
  width: 90px;
  color: var(--text-secondary);
  font-size: 13px;
  flex-shrink: 0;
}
.plan-value {
  font-size: 13px;
  font-weight: 500;
}
.plan-steps {
  padding: 12px 0;
}
.step-list {
  list-style: none;
  margin: 8px 0 0;
  padding: 0;
}
.step-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 0;
  font-size: 13px;
}
.step-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--border-control);
}
.step-item.done .step-dot {
  background: var(--success);
}
.step-item.running .step-dot {
  background: var(--accent);
  animation: pulse 1s infinite;
}
.step-status {
  margin-left: auto;
  font-size: 12px;
  color: var(--text-secondary);
}
.plan-note {
  font-size: 12px;
  color: var(--text-secondary);
  padding: 8px 0;
}
.plan-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  padding-top: 12px;
  border-top: 1px solid var(--border);
}
.running-text {
  font-size: 13px;
}
.hint-text {
  font-size: 12px;
  color: var(--text-secondary);
  margin-left: 10px;
}
</style>
