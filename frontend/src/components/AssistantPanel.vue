<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import ChatTimeline from './ChatTimeline.vue'
import { uploadAsset } from '@/api/business'
import { errorMessage } from '@/api/http'
import { useAgentStore } from '@/stores/agent'
import { useBusinessStore } from '@/stores/business'
import { useConnectionStore } from '@/stores/connection'
import { useSelectionStore } from '@/stores/selection'
import { useUiStore } from '@/stores/ui'
import { useWorkspaceStore } from '@/stores/workspace'
import type { AssetSummary, PermissionMode } from '@/types'

/**
 * 对话面板：主区（center）与业务页右侧（dock）共用一份实现。
 *
 * 结构照 v1.6：
 *   消息区（居中 850px 阅读宽度，无外框）
 *   待确认动作（固定在输入框上方，长内容可折叠）
 *   一个**一体式输入框**：附件 + 权限 + 模型 + 发送/停止都在同一框内。
 *
 * 这里不再重复显示“新会话/项目名/海巡助手·某会话”这类标题：
 * 项目上下文在顶部工具栏，当前会话在左侧会话列表。
 */
const props = withDefaults(
  defineProps<{
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
const selection = useSelectionStore()
const ui = useUiStore()

const input = ref('')
const textarea = ref<HTMLTextAreaElement | null>(null)
const timeline = ref<InstanceType<typeof ChatTimeline> | null>(null)
const composing = ref(false)
const attachments = ref<AssetSummary[]>([])
const attachmentError = ref('')
const uploading = ref(false)
const dragging = ref(false)
const filePicker = ref<HTMLInputElement | null>(null)
/**
 * “确保会话 → 发送”这一整段的进行中标志。
 *
 * 同一帧里的两次点击（或点击 + 回车）在第一个 await 之前都会通过那些轻量校验，
 * 于是各自建一条会话：第一条真正发出去，第二条变成空会话还把页面切走。
 * 锁必须在这里立起来 —— 不能等 ``agent.sending``，那已经晚到会话创建之后了。
 */
const sendingDraft = ref(false)

const projectId = computed(() => String(route.params.projectId ?? ''))
/**
 * 当前要跟谁对话。业务页路由没有 sessionId，但助手不能因此变空：
 * 回退到“上次看的会话”→“该项目最新会话”。
 */
const sessionId = computed(() => {
  const fromRoute = String(route.params.sessionId ?? '')
  if (fromRoute) return fromRoute
  const stored = workspace.currentSessionId
  const list = workspace.sessionsByProject[projectId.value] ?? []
  if (stored && list.some((item) => item.id === stored)) return stored
  return list[0]?.id ?? ''
})
const project = computed(() => (projectId.value ? workspace.projectById(projectId.value) : null))
/**
 * 当前项目下由识别记录/复核队列“自动注册”的界面选择。
 * 发送时冻结快照交给后端校验，用户无需把 ID 打进正文或点“交给助手”新建会话。
 */
const activeSelection = computed(() => selection.forProject(projectId.value))
const selectedCount = computed(() => activeSelection.value?.recordIds.length ?? 0)
const selectionLabel = computed(() => activeSelection.value?.page ?? '')
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

/** 模型连接只有一个事实来源：连接里有没有保存过。 */
const hasConnection = computed(() => Boolean(connection.connection))
const modeValue = computed(() => agent.preferences?.mode ?? 'request')
const modeLabel = computed(
  () => agent.modes.find((item) => item.id === modeValue.value)?.label ?? '请求批准',
)
/** 权限说明按需展开（hover/focus 才看），不占底部一整行。 */
const modeHint = computed(() => agent.preferences?.mode_hint ?? '')

/**
 * 未就绪时只留一句可执行的提示：
 * * 没连模型 → “连接模型”；
 * * 没绑目录 → “绑定文件夹”。
 * 不做红字叠加，也不在别处再写一遍“已连接/未配置”。
 */
const blockedReason = computed(() => {
  if (!project.value) return '先在左侧选择一个项目'
  if (!hasConnection.value) return '还没有连接模型'
  if (!project.value.root_path) return '这个项目还没有绑定文件夹'
  return ''
})
const canSend = computed(
  () =>
    Boolean(project.value) &&
    Boolean(input.value.trim() || attachments.value.length) &&
    !agent.sending &&
    !sendingDraft.value &&
    !running.value &&
    hasConnection.value &&
    !blockedReason.value,
)
const EXAMPLES = [
  '帮我看看这个项目里有哪些数据集和权重',
  '检测 images 里的图片，并核查置信度最低的那几张',
  '把已复核的样本回流到数据集',
]

watch(
  sessionId,
  (value) => {
    workspace.selectSession(value)
    if (!value) return
    void workspace.loadMessages(value)
    void agent.resumeSession(value)
    void agent.refreshSessionActions(value)
    // 业务进度按**会话**监听：与这一轮是否结束无关。
    business.watchSession(projectId.value, value)
    void business.refreshJobs(projectId.value, value)
    void business.loadRecords(projectId.value, { session_id: value }, true)
  },
  { immediate: true },
)

const cards = computed(() => (sessionId.value ? business.cardsFor(sessionId.value) : []))

// 附件属于当前项目：换项目就清空，绝不把 A 项目的图片带到 B 项目。
watch(projectId, (id) => {
  attachments.value = []
  attachmentError.value = ''
  // 选择上下文只在注册它的项目内有效；切项目必须清掉，避免把旧项目 ID 发给新项目。
  if (selection.context && selection.context.projectId !== id) selection.clear()
})

/** 队列/记录页的“交给助手”只聚焦输入框并保留选择，不新建会话、不跳页。 */
function focusComposer(): void {
  void nextTick(() => {
    textarea.value?.focus()
    textarea.value?.scrollIntoView({ block: 'center' })
  })
}

onMounted(() => {
  void connection.load()
  if (!agent.ready) void agent.loadCapabilities()
  void agent.loadPreferences()
  window.addEventListener('hx17:focus-composer', focusComposer)
})

onUnmounted(() => {
  window.removeEventListener('hx17:focus-composer', focusComposer)
})

/** 输入框随内容长高，最多 200px，长草稿在框内滚动。 */
watch(input, () =>
  nextTick(() => {
    const element = textarea.value
    if (!element) return
    element.style.height = 'auto'
    element.style.height = `${Math.min(element.scrollHeight, 200)}px`
  }),
)

function onEnter(event: KeyboardEvent): void {
  if (composing.value || event.isComposing) return
  void send()
}

/**
 * 还没有会话时先建一条（用户点发送才建），再把这句发出去。
 * 刷新页面不会创建会话，只有真的发了第一条消息才建。
 */
async function ensureSession(): Promise<string> {
  if (sessionId.value) return sessionId.value
  const id = projectId.value
  if (!id) return ''
  const text = input.value.trim()
  const title = text.length > 24 ? `${text.slice(0, 24)}…` : text || '新会话'
  const created = await workspace.createSession(id, title)
  if (!created) return ''
  await router.replace({ name: 'session', params: { projectId: id, sessionId: created.id } })
  return created.id
}

async function send(): Promise<void> {
  if (sendingDraft.value) return
  const text = input.value.trim()
  if ((!text && !attachments.value.length) || blockedReason.value) return
  // 先把这次要发的附件和选择快照记下来：等待期间用户改动不影响已发出的这一条。
  const assetIds = attachments.value.map((item) => item.id)
  const snapshot = activeSelection.value
  const selectionPayload = snapshot
    ? { page: snapshot.page, recordIds: [...snapshot.recordIds] }
    : null
  // 正文只留一句可读提示；精确 ID 由 ui_context 结构化传给后端，模型不靠正文猜。
  const body = text || '请查看我上传的图片。'
  const content = snapshot
    ? `${body}\n\n（已附带 ${snapshot.recordIds.length} 条选中记录上下文）`
    : body
  sendingDraft.value = true
  try {
    const target = await ensureSession()
    if (!target) return
    const ok = await agent.send(target, content, assetIds, selectionPayload)
    if (ok) {
      input.value = ''
      attachments.value = []
      attachmentError.value = ''
      timeline.value?.toLatest()
    }
  } finally {
    // 失败/取消都必须放开：草稿与附件保持原样，用户可以直接重试。
    sendingDraft.value = false
  }
}

async function addFiles(files: FileList | File[] | null | undefined): Promise<void> {
  if (!files) return
  if (!projectId.value) {
    attachmentError.value = '先在左侧选择一个项目'
    return
  }
  // 还没有会话也能先传：资产按**项目**存，session_id 只是可选归属。
  // 选图本身不会创建会话；真正发送时 ensureSession 才建第一条。
  attachmentError.value = ''
  const images = Array.from(files).filter((file) => file.type.startsWith('image/'))
  if (!images.length) {
    attachmentError.value = '只支持图片；ZIP/PT 请在数据集或模型页导入'
    return
  }
  uploading.value = true
  try {
    for (const file of images.slice(0, 4 - attachments.value.length)) {
      const asset = await uploadAsset(projectId.value, file, {
        sessionId: sessionId.value || undefined,
      })
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

/** 历史轮次的执行详情：点开才拉，且只拉这一轮自己的事件。 */
async function loadTurnDetail(turnId: string): Promise<void> {
  await agent.loadTurnEvents(turnId, true)
}

/** 终态已到但正文没读到：再读一次消息（网络慢时的可恢复入口）。 */
async function reloadMessages(): Promise<void> {
  if (!sessionId.value) return
  await workspace.loadMessages(sessionId.value, true)
}

function pick(text: string): void {
  input.value = text
  void nextTick(() => textarea.value?.focus())
}

function navigate(target: unknown): void {
  if (!target) return
  void router.push(target as Parameters<typeof router.push>[0])
}

function goConnect(): void {
  void router.push({ name: 'settings' })
}

function goBind(): void {
  void router.replace({ query: { ...route.query, details: '1' } })
}

async function newSession(): Promise<void> {
  if (!projectId.value) return
  const created = await workspace.createSession(projectId.value, '新会话')
  if (!created) return
  await router.push({ name: 'session', params: { projectId: projectId.value, sessionId: created.id } })
}
</script>

<template>
  <aside
    class="assistant"
    :class="props.variant === 'center' ? 'assistant-center' : 'assistant-dock'"
    aria-label="海巡助手"
    @dragover.prevent="dragging = true"
    @dragleave="dragging = false"
    @drop.prevent="onDrop"
  >
    <!-- 只有停靠态保留一条极简标题栏；主区不重复项目/会话标题。 -->
    <header v-if="props.variant === 'dock'" class="dock-head">
      <span class="dock-title">助手</span>
      <div class="dock-head-actions">
        <button class="icon-btn" type="button" aria-label="新建会话" title="新建会话" @click="newSession">
          ＋
        </button>
        <button class="icon-btn" type="button" aria-label="收起助手" title="收起助手" @click="ui.toggleDock()">
          ›
        </button>
      </div>
    </header>

    <ChatTimeline
      ref="timeline"
      :session-id="sessionId"
      :project-id="projectId"
      :messages="workspace.messages"
      :turn="turn"
      :events="events"
      :business-cards="cards"
      :poll-issue="turn ? agent.issueFor(turn.id) : ''"
      :has-older="workspace.hasOlderMessages"
      :events-by-turn="agent.events"
      @pick="pick"
      @navigate="navigate"
      @older="loadOlder"
      @detail="loadTurnDetail"
      @reload="reloadMessages"
    >
      <template #empty>
        <div class="chat-empty">
          <div class="empty-symbol" aria-hidden="true">✳</div>
          <h2>今天想检查什么？</h2>
          <p>描述目标，或添加图片、数据集和项目资料。<br />海巡助手会先看实际情况，再和你一起推进。</p>
          <p v-if="project && !project.root_path" class="empty-bind">
            这个项目还没绑定文件夹，
            <button class="link" type="button" @click="goBind">去绑定</button>
          </p>
          <div class="empty-examples">
            <button
              v-for="example in EXAMPLES"
              :key="example"
              type="button"
              @click="pick(example)"
            >
              {{ example }}
            </button>
          </div>
        </div>
      </template>
    </ChatTimeline>

    <p v-if="workspace.error" class="chat-banner" role="alert">
      {{ workspace.error }}
      <button class="link" type="button" @click="workspace.error = ''">知道了</button>
    </p>
    <p v-else-if="agent.error" class="chat-banner" role="alert">
      {{ agent.error }}
      <button class="link" type="button" @click="agent.error = ''">知道了</button>
    </p>
    <p v-else-if="agent.notice" class="chat-notice">{{ agent.notice }}</p>

    <!-- 确认区固定在输入框上方；长内容用 details 收起，不占满屏。 -->
    <section v-if="pendingActions.length" class="approval-dock" aria-label="待确认动作">
      <details v-for="action in pendingActions" :key="action.id" class="approval-item" open>
        <summary class="approval-head">
          <span class="tag tag-admin">{{ action.risk === 'destructive' ? '高风险' : '写入' }}</span>
          <span class="approval-title">{{ action.title }}</span>
          <span class="hint">待确认</span>
        </summary>
        <pre v-if="action.preview" class="approval-preview mono">{{ action.preview }}</pre>
        <div class="approval-actions">
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
      </details>
    </section>

    <!-- 一体式输入框：附件 / 权限 / 模型 / 发送都在同一个框内 -->
    <footer class="composer" :class="{ dragging }">
      <div class="composer-box">
        <!-- 选择上下文来自识别记录/复核队列的自动注册；普通发送即冻结快照，无需另建会话。 -->
        <div v-if="activeSelection" class="selection-context" role="status">
          <span>
            已附带 <strong>{{ selectedCount }}</strong> 条选中记录上下文（{{ selectionLabel }}）
          </span>
          <button class="link" type="button" title="清除选择并同步取消页面勾选" @click="selection.clear(projectId)">
            清除选择
          </button>
        </div>
        <div v-if="attachments.length || uploading || attachmentError" class="attach-list">
          <span v-for="asset in attachments" :key="asset.id" class="attach-chip">
            <img :src="asset.url" :alt="asset.filename" />
            <span class="mono">{{ asset.filename }}</span>
            <button class="icon-btn tiny" type="button" aria-label="移除附件" @click="removeAttachment(asset.id)">
              ×
            </button>
          </span>
          <span v-if="uploading" class="hint">上传中…</span>
          <span v-if="attachmentError" class="hint form-error">{{ attachmentError }}</span>
        </div>

        <textarea
          ref="textarea"
          v-model="input"
          rows="1"
          class="composer-textarea"
          :placeholder="running ? '这一轮还在进行；可以点停止后再发新消息' : '描述你的目标，或拖入图片…'"
          :disabled="running"
          aria-label="发送给海巡助手的消息"
          @keydown.enter.exact.prevent="onEnter"
          @compositionstart="composing = true"
          @compositionend="composing = false"
          @paste="onPaste"
        ></textarea>

        <div class="composer-toolbar">
          <button
            class="icon-btn"
            type="button"
            aria-label="添加图片"
            :title="projectId ? '添加图片' : '先在左侧选择一个项目'"
            :disabled="uploading || !projectId"
            @click="filePicker?.click()"
          >
            ＋
          </button>
          <input
            ref="filePicker"
            type="file"
            accept="image/*"
            multiple
            hidden
            @change="onPick"
          />

          <label class="chip" :title="`权限：${modeLabel}${modeHint ? ' · ' + modeHint : ''}`">
            <span class="sr-only">权限模式</span>
            <select
              :value="modeValue"
              :disabled="!agent.ready || !agent.modes.length || running"
              @change="agent.setMode(($event.target as HTMLSelectElement).value as PermissionMode)"
            >
              <option v-for="mode in agent.modes" :key="mode.id" :value="mode.id">{{ mode.label }}</option>
              <option v-if="!agent.modes.length" value="request">请求批准</option>
            </select>
          </label>

          <button
            v-if="!hasConnection"
            class="chip chip-action"
            type="button"
            title="到设置里连接个人模型"
            @click="goConnect"
          >
            连接模型
          </button>
          <label v-else class="chip" :title="'当前模型'">
            <span class="sr-only">模型</span>
            <select
              :value="agent.model ?? ''"
              :disabled="!agent.models.length || running"
              @change="agent.setModel(($event.target as HTMLSelectElement).value)"
            >
              <option v-if="!agent.models.length" :value="agent.model ?? ''">{{ agent.model ?? '默认模型' }}</option>
              <option v-for="item in agent.models" :key="item" :value="item">{{ item }}</option>
            </select>
          </label>

          <span class="composer-spacer"></span>

          <button
            v-if="running && canStop"
            class="send-button stop"
            type="button"
            aria-label="停止本轮"
            title="停止本轮"
            @click="stop"
          >
            ■
          </button>
          <button
            v-else
            class="send-button"
            type="button"
            aria-label="发送"
            title="发送"
            :disabled="!canSend"
            @click="send"
          >
            ↑
          </button>
        </div>
      </div>

      <p class="composer-hint">
        Enter 发送 · Shift + Enter 换行<span v-if="running"> · 正在处理，可停止</span>
        <span v-else> · 重要结果请核实</span>
      </p>
      <!-- 没连模型时上面已经有“连接模型”入口，这里不重复说一遍。 -->
      <p v-if="blockedReason && hasConnection" class="composer-note">
        {{ blockedReason }}
        <button v-if="project && !project.root_path" class="link" type="button" @click="goBind">去绑定</button>
      </p>
    </footer>
  </aside>
</template>
