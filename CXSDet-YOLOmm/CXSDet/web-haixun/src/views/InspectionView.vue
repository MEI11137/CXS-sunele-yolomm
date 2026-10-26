<script setup lang="ts">
import { ref, computed } from 'vue'
import { useRouter } from 'vue-router'
import { useWorkspaceStore } from '@/stores/workspace'
import { useInspectionStore } from '@/stores/inspection'
import { useSessionStore } from '@/stores/session'
import { useAuthStore } from '@/stores/auth'
import { CURRENT_MODE, getAdapter } from '@/services'
import type { AgentTask, Confirmation, DetectionRecordSummary, ResourceKind } from '@/types'
import { IMAGES } from '@/assets/images'

const router = useRouter()
const workspace = useWorkspaceStore()
const inspection = useInspectionStore()
const session = useSessionStore()
const auth = useAuthStore()

type ImageItem = {
  id: string
  name: string
  url: string
  size: number
  source: 'user' | 'sample'
  /** 本机文件本体：上传与重试都要用，不能丢 */
  file?: File
  /** RGB+X 多模态的第二模态影像 */
  xFile?: File
  fileId?: string
  resourceId?: string
  state: 'selected' | 'uploading' | 'uploaded' | 'failed'
  error?: string
}

const images = ref<ImageItem[]>([])
const selectedModel = ref<string>('')
const confidence = ref(0.5)
const dragging = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)
const xInput = ref<HTMLInputElement | null>(null)
const xInputTarget = ref<string>('')

// 计划 / 执行
const task = ref<AgentTask | null>(null)
const planAction = ref<Confirmation | null>(null)
const sessionId = ref('')
type Phase = 'idle' | 'planning' | 'awaiting_plan' | 'awaiting_action' | 'running' | 'done' | 'failed' | 'stopped'
const phase = ref<Phase>('idle')
const notice = ref('')
const results = ref<DetectionRecordSummary[]>([])
const polling = ref(false)

const MAX_SIZE = 100 * 1024 * 1024 // 100 MB
const isMock = CURRENT_MODE === 'mock'

const canConfirm = computed(
  () => images.value.length > 0 && selectedModel.value !== '' && !auth.readOnlyDemo,
)

const uploadedCount = computed(() => images.value.filter((i) => i.state === 'uploaded').length)

/** 只有可用（训练完成且已发布可推理）的模型能执行检测 */
const availableModels = computed(() => inspection.models.filter((m) => m.status === 'available'))

const modelName = computed(() => {
  const m = inspection.models.find((x) => x.id === selectedModel.value)
  return m ? `${m.name} ${m.version}` : selectedModel.value
})

function onFileSelect(e: Event) {
  const files = (e.target as HTMLInputElement).files
  if (files) void addFiles(Array.from(files))
  ;(e.target as HTMLInputElement).value = ''
}

function onXFileSelect(e: Event) {
  const files = (e.target as HTMLInputElement).files
  const target = images.value.find((i) => i.id === xInputTarget.value)
  if (files && files[0] && target) {
    target.xFile = files[0]
  }
  ;(e.target as HTMLInputElement).value = ''
}

async function addFiles(files: File[]) {
  for (const f of files) {
    if (f.size > MAX_SIZE) {
      notice.value = `文件 ${f.name} 超过单文件上限 100 MB，已跳过。`
      continue
    }
    // 与后端 allowed_extensions 保持一致，前端不多放也不偷放
    if (!/\.(jpe?g|png|webp|bmp|gif)$/i.test(f.name)) {
      notice.value = `文件 ${f.name} 格式不支持（仅 JPG/PNG/WEBP/BMP/GIF），已跳过。`
      continue
    }
    const item: ImageItem = {
      id: 'img-' + Date.now() + Math.random(),
      name: f.name,
      url: URL.createObjectURL(f),
      size: f.size,
      source: 'user',
      file: f,
      state: 'selected',
    }
    images.value.push(item)
    if (!isMock) void uploadImage(item)
  }
}

/** 真实上传并登记为项目资源；失败保留 File 可重试 */
async function uploadImage(img: ImageItem) {
  if (isMock || !img.file) return
  const projectId = workspace.currentId
  if (!projectId) {
    img.state = 'failed'
    img.error = '请先选择工作区'
    return
  }
  img.state = 'uploading'
  img.error = ''
  try {
    const asset = await getAdapter().uploadProjectFile(projectId, img.file)
    const resource = await getAdapter().registerResource(projectId, {
      kind: 'image',
      displayName: img.name,
      metadata: { file_id: asset.file_id, content_type: asset.content_type },
    })
    img.fileId = asset.file_id
    img.resourceId = resource.id
    img.state = 'uploaded'
  } catch (e) {
    img.state = 'failed'
    img.error = (e as Error).message
  }
}

function onDrop(e: DragEvent) {
  e.preventDefault()
  dragging.value = false
  const files = e.dataTransfer?.files
  if (files) void addFiles(Array.from(files))
}

function onPaste(e: ClipboardEvent) {
  const items = e.clipboardData?.items
  if (!items) return
  const picked: File[] = []
  for (const item of items) {
    if (item.type.startsWith('image/')) {
      const f = item.getAsFile()
      if (f) picked.push(f)
    }
  }
  if (picked.length) void addFiles(picked)
}

// 示例影像只在演示模式下可用：真实模式没有 File 就无法上传，不能用假图冒充
function addSamples() {
  const sampleNames = ['F01_A_叶片前缘.jpg', 'F03_B_叶片表面.jpg', 'F07_C_叶片涂层.jpg']
  const samplePool = [IMAGES.blade, IMAGES.turbine, IMAGES.scene]
  for (let i = 0; i < sampleNames.length; i++) {
    images.value.push({
      id: 'img-' + Date.now() + Math.random(),
      name: sampleNames[i],
      url: samplePool[i % samplePool.length],
      size: 0,
      source: 'sample',
      state: 'uploaded',
    })
  }
}

function removeImage(id: string) {
  images.value = images.value.filter((i) => i.id !== id)
}

function pickX(img: ImageItem) {
  xInputTarget.value = img.id
  xInput.value?.click()
}

function formatSize(n: number) {
  if (n === 0) return '示例'
  if (n < 1024 * 1024) return (n / 1024).toFixed(0) + ' KB'
  return (n / 1024 / 1024).toFixed(1) + ' MB'
}

function imageStateText(img: ImageItem): string {
  if (img.state === 'uploading') return '上传中…'
  if (img.state === 'uploaded') return isMock ? '待执行' : '已上传'
  if (img.state === 'failed') return img.error ? `上传失败：${img.error}` : '上传失败'
  return '待上传'
}

/** 生成计划：真实会话 + 真实 Agent 规划，不再写死 session_id */
async function generatePlan() {
  if (!canConfirm.value) return
  notice.value = ''
  results.value = []
  phase.value = 'planning'
  try {
    // 未上传的先补齐，避免把本地文件名当输入
    for (const img of images.value) {
      if (img.state !== 'uploaded' && img.state !== 'failed') await uploadImage(img)
    }
    const failed = images.value.filter((i) => i.state === 'failed')
    if (failed.length) {
      throw new Error(`有 ${failed.length} 张影像上传失败，请重试后再生计划`)
    }

    const projectId = workspace.currentId
    if (!projectId) throw new Error('请先选择工作区')

    if (!sessionId.value) {
      const created = await getAdapter().createSession(projectId, `巡检 ${new Date().toLocaleString('zh-CN')}`)
      sessionId.value = created.id
    }

    const refs = images.value
      .filter((i) => i.resourceId)
      .map((i) => ({ id: i.resourceId!, kind: 'image' as ResourceKind, display_name: i.name }))

    const instruction =
      `对已上传的 ${images.value.length} 张影像执行缺陷检测，使用模型「${modelName.value}」` +
      `（model_id=${selectedModel.value}），置信度阈值 ${confidence.value}。` +
      '请先给出检测计划，不要直接执行；需要我确认后再运行。'

    await session.send(instruction, { resourceRefs: refs })
    // store 保存了本轮完整结果（reply/task/action/plan），页面从那里取卡片
    const turn = session.lastTurn
    if (!turn) throw new Error('助手未返回本轮结果，请检查会话是否可用')
    task.value = turn.task
    planAction.value = turn.action

    if (turn.task?.status === 'awaiting_plan') {
      phase.value = 'awaiting_plan'
      notice.value = '计划已生成，请核对影像数量与模型版本后确认。'
    } else if (turn.action && turn.action.status === 'pending') {
      phase.value = 'awaiting_action'
      notice.value = '需要你确认该检测操作后才会执行。'
    } else if (turn.task?.status === 'completed') {
      phase.value = 'done'
      notice.value = '任务已完成。'
      await refreshResults()
    } else {
      phase.value = 'idle'
      notice.value = turn.message.content || '助手未返回可执行的计划。'
    }
  } catch (e) {
    phase.value = 'failed'
    const msg = (e as Error).message
    // 未配置个人模型连接时，明确告知并可改用直接检测入口
    notice.value = /连接|能力|AGENT_MODEL/.test(msg)
      ? `Agent 规划不可用：${msg}。可先在「设置 → 个人连接」配置模型，或使用下方「直接检测」。`
      : `生成计划失败：${msg}`
  }
}

/** 确认计划，随后若产生动作确认再继续；真实状态以快照为准 */
async function confirmRun() {
  if (!task.value) return
  if (phase.value === 'running') return
  phase.value = 'running'
  notice.value = ''
  try {
    const taskId = task.value.id
    const snapshot = await getAdapter().confirmPlan(taskId)
    task.value = snapshot.task
    const pendingAction = snapshot.actions.find((a) => a.status === 'pending')
    if (pendingAction) {
      planAction.value = pendingAction
      phase.value = 'awaiting_action'
      notice.value = '计划已确认，还需要确认具体检测操作。'
      return
    }
    await waitForTask(taskId)
  } catch (e) {
    phase.value = 'failed'
    notice.value = '执行失败：' + (e as Error).message
  }
}

/** 确认动作（带真实版本号，避免确认过期目标） */
async function confirmTheAction() {
  if (!planAction.value) return
  phase.value = 'running'
  notice.value = ''
  try {
    const confirmed = await getAdapter().confirmAction(planAction.value.action_id, planAction.value.revision)
    planAction.value = confirmed
    if (task.value) await waitForTask(task.value.id)
    else phase.value = 'done'
  } catch (e) {
    phase.value = 'failed'
    notice.value = '确认操作失败：' + (e as Error).message
  }
}

/** 轮询真实快照：只有后端说 completed 才算完成 */
async function waitForTask(taskId: string) {
  polling.value = true
  const deadline = Date.now() + 10 * 60 * 1000
  try {
    while (Date.now() < deadline) {
      await sleep(2000)
      const snapshot = await getAdapter().getTaskSnapshot(taskId)
      if (snapshot.task) task.value = snapshot.task
      const status = snapshot.task?.status
      if (status === 'completed') {
        phase.value = 'done'
        notice.value = '检测完成，结果已进入人工复核队列。'
        await refreshResults()
        return
      }
      if (status === 'failed' || status === 'partial_failed') {
        phase.value = 'failed'
        notice.value = '检测未全部成功，请查看任务摘要：' + (snapshot.task?.summary || '无详情')
        return
      }
      if (status === 'stopped' || status === 'interrupted') {
        phase.value = 'stopped'
        notice.value = '任务已停止或中断，部分结果可能已写入。'
        return
      }
      if (status === 'awaiting_action' || status === 'awaiting_human' || status === 'awaiting_business') {
        phase.value = 'awaiting_action'
        const next = snapshot.actions.find((a) => a.status === 'pending')
        planAction.value = next ?? planAction.value
        notice.value = '任务在等待确认或等待业务处理，请按提示继续。'
        return
      }
    }
    phase.value = 'failed'
    notice.value = '等待超时，任务可能仍在后端执行，请稍后刷新查看。'
  } catch (e) {
    phase.value = 'failed'
    notice.value = '读取任务状态失败：' + (e as Error).message
  } finally {
    polling.value = false
  }
}

/** 直接检测：明确业务动作入口，真实推理并落库，不经过 LLM 规划 */
async function runDirectDetection() {
  if (!canConfirm.value) return
  phase.value = 'running'
  notice.value = ''
  results.value = []
  try {
    for (const img of images.value) {
      if (!img.file) throw new Error(`“${img.name}”缺少本机文件，请取消示例影像后重试`)
      const record = await getAdapter().detectImage({
        modelId: selectedModel.value,
        file: img.file,
        xFile: img.xFile,
        confidence: confidence.value,
        title: img.name,
      })
      results.value.push(record)
    }
    phase.value = 'done'
    notice.value = `检测完成，共生成 ${results.value.length} 条记录，已进入人工复核队列。`
    inspection.loaded = false
    await inspection.load()
  } catch (e) {
    phase.value = 'failed'
    notice.value = '检测失败：' + (e as Error).message
  }
}

async function refreshResults() {
  if (!sessionId.value) return
  try {
    const page = await getAdapter().listRecords({ page: 1, pageSize: 20 })
    results.value = page.items.slice(0, 20)
  } catch {
    /* 读取失败不影响任务状态展示 */
  }
}

async function rejectPlan() {
  if (!task.value) return
  notice.value = ''
  try {
    if (phase.value === 'awaiting_action' && planAction.value) {
      await getAdapter().rejectAction(planAction.value.action_id, '用户拒绝该检测操作')
    }
    await getAdapter().stopTask(task.value.id, '用户拒绝该计划')
    phase.value = 'stopped'
    notice.value = '已拒绝该计划，任务未执行。'
  } catch (e) {
    phase.value = 'failed'
    notice.value = '停止任务失败：' + (e as Error).message
  }
}

async function cancelRun() {
  if (!task.value) return
  notice.value = ''
  try {
    await getAdapter().stopTask(task.value.id, '用户取消当前步骤')
    phase.value = 'stopped'
    notice.value = '已向后端请求停止，部分结果可能已写入。'
  } catch (e) {
    notice.value = '停止失败：' + (e as Error).message
  }
}

function sleep(ms: number) {
  return new Promise((r) => setTimeout(r, ms))
}

function goReview() {
  router.push('/review')
}

function reset() {
  task.value = null
  planAction.value = null
  sessionId.value = ''
  results.value = []
  phase.value = 'idle'
  notice.value = ''
  notice.value = ''
}
</script>

<template>
  <div class="page">
    <div class="page-header">
      <h1 class="page-title">图片巡检</h1>
      <div class="header-meta">
        <span class="tag" :class="isMock ? '' : 'tag-accent'">
          {{ isMock ? '演示模式 · 模拟数据' : '真实模式 · 本机推理' }}
        </span>
        <span v-if="uploadedCount" class="tag num">{{ uploadedCount }} 张已上传</span>
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
          <button v-if="isMock" class="btn btn-ghost" @click="addSamples">使用示例影像</button>
          <input ref="fileInput" type="file" multiple hidden accept=".jpg,.jpeg,.png,.webp,.bmp,.gif" @change="onFileSelect" />
          <input ref="xInput" type="file" hidden accept=".jpg,.jpeg,.png,.webp,.bmp,.gif" @change="onXFileSelect" />
          <p class="dropzone-hint">
            单文件上限 100 MB · 选择后立即上传到本机项目（受保护存储），执行时按文件引用取图
          </p>
        </div>
      </div>

      <!-- 影像列表 -->
      <div v-if="images.length" class="image-list">
        <div v-for="img in images" :key="img.id" class="image-item panel">
          <img class="photo" :src="img.url" :alt="img.name" />
          <div class="image-meta">
            <div class="image-name" :title="img.name">{{ img.name }}</div>
            <div class="image-info">
              <span class="tag" :class="img.state === 'failed' ? 'tag-error' : img.state === 'uploaded' ? 'tag-success' : ''">
                {{ imageStateText(img) }}
              </span>
              <span class="num">{{ formatSize(img.size) }}</span>
            </div>
            <div class="image-info">
              <span class="tag" :class="img.xFile ? 'tag-accent' : ''">
                {{ img.xFile ? 'X: ' + img.xFile.name : '无 X 模态' }}
              </span>
              <button class="btn btn-ghost btn-sm" @click="pickX(img)">附带 X 影像</button>
              <button v-if="img.state === 'failed'" class="btn btn-ghost btn-sm" @click="uploadImage(img)">重试</button>
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
        <label v-for="m in availableModels" :key="m.id" class="model-option panel" :class="{ selected: selectedModel === m.id }">
          <input type="radio" name="model" :value="m.id" v-model="selectedModel" class="sr-only" />
          <div class="model-name">{{ m.name }} <span class="mono num">{{ m.version }}</span></div>
          <div class="model-type">
            <span class="tag" :class="m.type === 'system' ? 'tag-accent' : ''">{{ m.type === 'system' ? '系统基线 · 只读' : '个人模型' }}</span>
            <span v-if="m.metrics" class="model-metric num">mAP50 {{ m.metrics.mAP50.toFixed(3) }}</span>
          </div>
        </label>
        <div v-if="!availableModels.length" class="hint-text">
          当前没有可用模型。仅「已发布」的模型可以执行检测，请先在模型管理中发布。
        </div>
      </div>
      <div class="confidence-row">
        <span class="plan-label">置信度阈值</span>
        <input v-model.number="confidence" class="slider" type="range" min="0.05" max="0.95" step="0.05" />
        <span class="mono num">{{ confidence.toFixed(2) }}</span>
      </div>
    </div>

    <!-- 计划 / 执行 -->
    <div class="section" v-if="notice || task">
      <h2 class="section-title">检测计划</h2>
      <div
        v-if="notice"
        class="notice"
        :class="phase === 'failed' ? 'notice-error' : phase === 'done' ? 'notice-success' : ''"
      >
        {{ notice }}
      </div>

      <div v-if="task" class="panel plan-card">
        <div class="plan-row">
          <span class="plan-label">目标项目</span>
          <span class="plan-value">{{ workspace.current?.name }}</span>
        </div>
        <div class="plan-row">
          <span class="plan-label">任务状态</span>
          <span class="plan-value">{{ task.status }}</span>
        </div>
        <div v-if="task.goal" class="plan-row">
          <span class="plan-label">目标</span>
          <span class="plan-value">{{ task.goal }}</span>
        </div>
        <div class="plan-row">
          <span class="plan-label">输入影像</span>
          <span class="plan-value num">{{ images.length }} 张（已上传 {{ uploadedCount }}）</span>
        </div>
        <div class="plan-row">
          <span class="plan-label">模型版本</span>
          <span class="plan-value mono">
            {{ task.business_refs.find(b => b.kind === 'model')?.display_name || modelName }}
          </span>
        </div>
        <div v-if="task.waiting_for" class="plan-row">
          <span class="plan-label">等待条件</span>
          <span class="plan-value mono">{{ task.waiting_for.kind }}</span>
        </div>
        <div class="plan-steps">
          <div class="plan-label">步骤</div>
          <ul class="step-list">
            <li v-for="s in task.steps" :key="s.id" class="step-item" :class="s.status">
              <span class="step-dot" aria-hidden="true"></span>
              <span>{{ s.name }}</span>
              <span class="step-status">
                {{ s.status === 'done' ? '完成' : s.status === 'pending' ? '待执行'
                   : s.status === 'running' ? '执行中' : s.status === 'failed' ? '失败' : s.status }}
              </span>
            </li>
          </ul>
        </div>

        <!-- 动作确认卡片：与计划确认分开，版本来自后端 -->
        <div v-if="planAction && planAction.status === 'pending'" class="plan-note action-card">
          <div class="action-title">
            待确认操作：{{ planAction.tool_title }}
            <span class="mono num">rev {{ planAction.revision }}</span>
          </div>
          <div v-if="planAction.parameters_summary" class="action-summary">
            {{ planAction.parameters_summary }}
          </div>
          <div v-if="planAction.actual_target_projects.length" class="action-targets">
            目标项目：{{ planAction.actual_target_projects.join('、') }}
          </div>
        </div>

        <div class="plan-note">确认执行会使用以上已确认版本的输入，不会静默切换图片、项目或模型。</div>

        <div class="plan-actions" v-if="phase === 'awaiting_plan'">
          <button class="btn btn-primary" @click="confirmRun">确认计划</button>
          <button class="btn" @click="rejectPlan">拒绝</button>
          <button class="btn btn-ghost" @click="generatePlan">调整（重新生成）</button>
        </div>
        <div class="plan-actions" v-else-if="phase === 'awaiting_action' && planAction">
          <button class="btn btn-primary" @click="confirmTheAction">确认该操作</button>
          <button class="btn" @click="rejectPlan">拒绝</button>
        </div>
        <div class="plan-actions" v-else-if="phase === 'running' || phase === 'planning'">
          <span class="spinner"></span>
          <span class="running-text">
            {{ phase === 'planning' ? '助手正在规划…' : '正在执行，等待后端返回真实状态…' }}
          </span>
          <button class="btn" @click="cancelRun">停止任务</button>
        </div>
        <div class="plan-actions" v-else-if="phase === 'done'">
          <button class="btn btn-primary" @click="goReview">前往人工复核</button>
          <button class="btn" @click="reset">再次巡检</button>
        </div>
        <div class="plan-actions" v-else-if="phase === 'failed' || phase === 'stopped'">
          <button class="btn" @click="reset">重新开始</button>
        </div>
      </div>
    </div>

    <!-- 真实检测结果 -->
    <div class="section" v-if="results.length">
      <h2 class="section-title">本次检测结果</h2>
      <div class="panel">
        <div v-for="r in results" :key="r.id" class="result-row">
          <span class="result-title">{{ r.title || r.id.slice(0, 8) }}</span>
          <span class="tag num">{{ r.detection_count }} 个目标</span>
          <span class="tag">{{ r.model_name }}</span>
          <span class="tag" :class="r.human_review_status === 'pending' ? '' : 'tag-success'">
            人工复核：{{ r.human_review_status }}
          </span>
        </div>
      </div>
    </div>

    <!-- 底部操作：Agent 规划 与 明确业务动作入口，两条路都不绕过确认 -->
    <div class="section" v-if="!task">
      <div class="action-row">
        <button class="btn btn-primary" :disabled="!canConfirm || phase === 'running'" @click="generatePlan">
          由助手规划检测
        </button>
        <button class="btn" :disabled="!canConfirm || phase === 'running'" @click="runDirectDetection">
          直接检测（已选模型立即推理）
        </button>
      </div>
      <span v-if="auth.readOnlyDemo" class="hint-text">当前为只读浏览，登录后才能执行真实检测。</span>
      <span v-else-if="!canConfirm" class="hint-text">请先选择至少一张影像并选择检测模型。</span>
      <span v-else class="hint-text">
        「由助手规划」会先生成计划等待你确认；「直接检测」是你已明确指定模型与影像后的立即执行。
      </span>
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
.confidence-row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 12px;
  font-size: 12px;
  color: var(--text-secondary);
}
.slider {
  flex: 1;
  max-width: 260px;
  accent-color: var(--accent);
}
.action-row {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  align-items: center;
}
.action-card {
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 10px 12px;
  background: var(--panel-secondary);
  margin-bottom: 8px;
}
.action-title {
  font-size: 13px;
  font-weight: 600;
  display: flex;
  align-items: center;
  gap: 8px;
}
.action-summary,
.action-targets {
  font-size: 12px;
  color: var(--text-secondary);
  margin-top: 4px;
}
.result-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border-bottom: 1px solid var(--border);
  font-size: 13px;
}
.result-row:last-child {
  border-bottom: none;
}
.result-title {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
