<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import ChatTimeline from './ChatTimeline.vue'
import { turnStatusLabel } from '@/lib/timeline'
import { uploadAsset } from '@/api/business'
import { errorMessage } from '@/api/http'
import { useAgentStore } from '@/stores/agent'
import { useBusinessStore } from '@/stores/business'
import { useConnectionStore } from '@/stores/connection'
import { useWorkspaceStore } from '@/stores/workspace'
import type { AssetSummary, PermissionMode } from '@/types'

const props = withDefaults(
  defineProps<{
    /** center：对话在项目主区；dock：业务页把对话挤到右侧 360px。 */
    variant?: 'center' | 'dock'
  }>(),
  { variant: 'dock' },
)

const route = useRoute()
const router = useRouter()
const workspace = useWorkspaceStore()
const connection = useConnectionStore()
const agent = useAgentStore()
const business = useBusinessStore()

const input = ref('')
const textarea = ref<HTMLTextAreaElement | null>(null)
const timeline = ref<InstanceType<typeof ChatTimeline> | null>(null)
/** 输入法组词中：这时的 Enter 是在选字，不能当成发送。 */
const composing = ref(false)
const attachments = ref<AssetSummary[]>([])
const attachmentError = ref('')
const uploading = ref(false)
const dragging = ref(false)
const filePicker = ref<HTMLInputElement | null>(null)

const projectId = computed(() => String(route.params.projectId ?? ''))
/**
 * 当前要跟谁对话。
 *
 * 业务页（识别记录/数据集/模型）的路由里没有 sessionId，但右侧助手**不能因此变空**：
 * 依次回退到“上次正在看的会话”“该项目最新的会话”，保证切到业务页还能继续问。
 */
const sessionId = computed(() => {
  const fromRoute = String(route.params.sessionId ?? '')
  if (fromRoute) return fromRoute
  const stored = workspace.currentSessionId
  const list = workspace.sessionsByProject[projectId.value] ?? []
  if (stored && list.some((item) => item.id === stored)) return stored
  return list[0]?.id ?? ''
})
const session = computed(() => {
  if (!sessionId.value) return null
  return (workspace.sessionsByProject[projectId.value] ?? []).find((item) => item.id === sessionId.value) ?? null
})
const project = computed(() => (projectId.value ? workspace.projectById(projectId.value) : null))
const turn = computed(() => (sessionId.value ? agent.activeTurn(sessionId.value) : null))
const events = computed(() => (turn.value ? agent.turnEvents(turn.value.id) : []))
const pendingActions = computed(() => (sessionId.value ? agent.pending(sessionId.value) : []))
const running = computed(
  () =>
    Boolean(turn.value) &&
    turn.value!.status !== 'completed' &&
    turn.value!.status !== 'failed' &&
    turn.value!.status !== 'stopped',
)
const stopping = computed(() => agent.stopping)
const canStop = computed(() => running.value && !stopping.value)

const blockedReason = computed(() => {
  if (!session.value) return ''
  if (!connection.connection) return '还没有保存个人模型连接，先到设置页配置'
  if (!project.value?.root_path) return '该项目还没有绑定服务器目录'
  return ''
})
const canSend = computed(
  () =>
    Boolean(session.value) &&
    input.value.trim().length > 0 &&
    !agent.sending &&
    !running.value &&
    !blockedReason.value,
)
/** 产品区只显示“能不能用 + 用的哪个模型”，不暴露引擎实现细节。 */
const runtimeLine = computed(() => {
  if (!agent.ready) return '助手未就绪：缺少可用的模型连接'
  return agent.model ? `模型 ${agent.model}` : '已连接'
})

watch(
  sessionId,
  (value) => {
    workspace.selectSession(value)
    if (!value) return
    void workspace.loadMessages(value)
    void agent.resumeSession(value)
    void agent.refreshSessionActions(value)
    // 业务进度按**会话**监听：与这一轮是否结束无关。
    // 长任务常常比对话更久，关掉监听会让用户以为任务丢了。
    business.watchSession(projectId.value, value)
    void business.refreshJobs(projectId.value, value)
    void business.loadRecords(projectId.value, { session_id: value }, true)
  },
  { immediate: true },
)

const cards = computed(() => (sessionId.value ? business.cardsFor(sessionId.value) : []))

onMounted(() => {
  void connection.load()
  if (!agent.ready) void agent.loadCapabilities()
  void agent.loadPreferences()
})

/** Enter 发送、Shift+Enter 换行；输入法组词期间一律不发送。 */
function onEnter(event: KeyboardEvent): void {
  if (composing.value || event.isComposing) return
  void send()
}

async function send(): Promise<void> {
  const text = input.value.trim()
  if (!text || !sessionId.value) return
  const ok = await agent.send(
    sessionId.value,
    text,
    attachments.value.map((item) => item.id),
  )
  if (ok) {
    input.value = ''
    attachments.value = []
    attachmentError.value = ''
  }
}

async function addFiles(files: FileList | File[] | null | undefined): Promise<void> {
  if (!files || !sessionId.value) return
  attachmentError.value = ''
  const images = Array.from(files).filter((file) => file.type.startsWith('image/'))
  if (!images.length) {
    attachmentError.value = '只支持图片；ZIP/PT 请在数据集或模型页导入'
    return
  }
  uploading.value = true
  try {
    for (const file of images.slice(0, 4 - attachments.value.length)) {
      const asset = await uploadAsset(projectId.value, file, { sessionId: sessionId.value })
      if (!attachments.value.some((item) => item.id === asset.id)) {
        attachments.value = [...attachments.value, asset]
      }
    }
  } catch (failure) {
    attachmentError.value = errorMessage(failure)
  } finally {
    uploading.value = false
    if (filePicker.value) filePicker.value.value = ''
  }
}

function onPick(event: Event): void {
  void addFiles((event.target as HTMLInputElement).files)
}

function onPaste(event: ClipboardEvent): void {
  const items = event.clipboardData?.items
  if (!items) return
  const files: File[] = []
  for (const item of items) {
    if (item.kind === 'file') {
      const file = item.getAsFile()
      if (file) files.push(file)
    }
  }
  if (files.length) {
    event.preventDefault()
    void addFiles(files)
  }
}

function onDrop(event: DragEvent): void {
  dragging.value = false
  void addFiles(event.dataTransfer?.files)
}

function removeAttachment(assetId: string): void {
  attachments.value = attachments.value.filter((item) => item.id !== assetId)
}

async function stop(): Promise<void> {
  if (!turn.value) return
  await agent.stop(turn.value.id)
}

async function loadOlder(): Promise<void> {
  if (!sessionId.value) return
  await workspace.loadOlderMessages(sessionId.value)
  await nextTick()
}

function pick(text: string): void {
  input.value = text
  void nextTick(() => textarea.value?.focus())
}

function navigate(target: unknown): void {
  if (!target) return
  void router.push(target as Parameters<typeof router.push>[0])
}
</script>

<template>
  <aside
    class="assistant"
    :class="[props.variant === 'center' ? 'assistant-center' : 'assistant-dock', { dragging }]"
    aria-label="海巡助手"
    @dragover.prevent="dragging = true"
    @dragleave="dragging = false"
    @drop.prevent="onDrop"
  >
    <header class="assistant-header">
      <div class="assistant-title">
        <span aria-hidden="true">✳</span><strong>海巡助手</strong>
        <span v-if="session" class="assistant-scope">· {{ session.title }}</span>
      </div>
      <span class="badge" :class="{ ok: agent.ready }">{{ agent.ready ? '可用' : '不可用' }}</span>
    </header>

    <div v-if="!session" class="assistant-placeholder">
      <p class="empty-hint">
        先在左侧选择或新建一个会话。会话属于具体项目，消息会保存到该项目的历史记录里。
      </p>
    </div>

    <template v-else>
      <div class="assistant-toolbar">
        <button v-if="workspace.hasOlderMessages" class="link" type="button" @click="loadOlder">
          加载更早的消息
        </button>
      </div>
      <ChatTimeline
        ref="timeline"
        :key="sessionId"
        :session-id="sessionId"
        :messages="workspace.messages"
        :turn="turn"
        :events="events"
        :business-cards="cards"
        :poll-issue="turn ? agent.issueFor(turn.id) : ''"
        @pick="pick"
        @navigate="navigate"
      />
    </template>

    <p v-if="agent.error" class="banner banner-error assistant-error" role="alert">
      {{ agent.error }}
      <button class="link" type="button" @click="agent.error = ''">知道了</button>
    </p>
    <p v-else-if="agent.notice" class="assistant-notice">{{ agent.notice }}</p>

    <!-- 输入框上方的固定动作区：本轮状态、待确认动作（确认区固定在输入框上方） -->
    <section v-if="session" class="assistant-dock-actions" aria-label="本轮状态与待确认动作">
      <div v-if="turn" class="dock-status">
        <span class="live-dot" :class="{ busy: running }" aria-hidden="true"></span>
        <span>{{ turnStatusLabel(turn.status) }}</span>
        <span v-if="turn.stop_requested && running" class="hint">已请求停止…</span>
        <button v-if="canStop" class="ghost small" type="button" @click="stop">停止本轮</button>
      </div>

      <article v-for="action in pendingActions" :key="action.id" class="dock-action">
        <p class="dock-action-title">
          <span class="tag tag-admin">{{ action.risk === 'destructive' ? '高风险' : '写入' }}</span>
          {{ action.title }}
        </p>
        <p v-if="action.preview" class="dock-action-preview mono">{{ action.preview }}</p>
        <div class="dock-action-buttons">
          <button
            class="primary small"
            type="button"
            :disabled="agent.deciding === action.id"
            @click="agent.approve(action.id)"
          >
            确认执行
          </button>
          <button
            class="ghost small"
            type="button"
            :disabled="agent.deciding === action.id"
            @click="agent.reject(action.id)"
          >
            拒绝
          </button>
        </div>
      </article>
    </section>

    <footer class="assistant-input">
      <div v-if="attachments.length || uploading || attachmentError" class="attachments">
        <span v-for="asset in attachments" :key="asset.id" class="attachment-chip">
          <img :src="asset.url" :alt="asset.filename" />
          <span class="mono">{{ asset.filename }}</span>
          <button class="link small" type="button" @click="removeAttachment(asset.id)">移除</button>
        </span>
        <span v-if="uploading" class="hint">上传中…</span>
        <span v-if="attachmentError" class="form-error">{{ attachmentError }}</span>
      </div>
      <textarea
        ref="textarea"
        v-model="input"
        rows="2"
        :placeholder="
          session
            ? running
              ? '这一轮还在进行；可以点上面的“停止本轮”后再发新消息'
              : '描述你的目标，例如看看这个目录里有哪些图片'
            : '请先选择会话'
        "
        :disabled="!session || running"
        aria-label="发送给海巡助手的消息"
        @keydown.enter.exact.prevent="onEnter"
        @compositionstart="composing = true"
        @compositionend="composing = false"
        @paste="onPaste"
      ></textarea>
      <p v-if="blockedReason" class="hint form-error">{{ blockedReason }}</p>
      <div class="assistant-actions">
        <label class="inline-field">
          <span class="hint">权限</span>
          <select
            :value="agent.preferences?.mode ?? 'request'"
            :disabled="!agent.ready || !agent.modes.length"
            @change="agent.setMode(($event.target as HTMLSelectElement).value as PermissionMode)"
          >
            <option v-for="mode in agent.modes" :key="mode.id" :value="mode.id">{{ mode.label }}</option>
            <option v-if="!agent.modes.length" value="request">请求批准</option>
          </select>
        </label>
        <label class="inline-field">
          <span class="hint">模型</span>
          <select
            :value="agent.model ?? ''"
            :disabled="!agent.models.length"
            @change="agent.setModel(($event.target as HTMLSelectElement).value)"
          >
            <option v-if="!agent.models.length" value="">未配置个人连接</option>
            <option v-for="item in agent.models" :key="item" :value="item">{{ item }}</option>
          </select>
        </label>
        <label class="button ghost small">
          {{ uploading ? '上传中…' : '图片' }}
          <input
            ref="filePicker"
            type="file"
            accept="image/*"
            multiple
            hidden
            :disabled="uploading || !session"
            @change="onPick"
          />
        </label>
        <span class="hint assistant-runtime">{{ runtimeLine }}</span>
        <button class="primary" type="button" :disabled="!canSend" @click="send">
          {{ agent.sending ? '发送中…' : '发送' }}
        </button>
      </div>
      <p v-if="agent.preferences?.mode_hint" class="hint">{{ agent.preferences.mode_hint }}</p>
    </footer>
  </aside>
</template>
