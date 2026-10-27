<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted } from 'vue'
import { useSessionStore } from '@/stores/session'
import { useWorkspaceStore } from '@/stores/workspace'
import { getAdapter } from '@/services'
import type { ChatMessage, Attachment, ResourceKind } from '@/types'
import AgentProgress from './AgentProgress.vue'

const props = defineProps<{ open: boolean; mobile?: boolean }>()
const emit = defineEmits<{ toggle: [] }>()

const session = useSessionStore()
const workspace = useWorkspaceStore()

const input = ref('')
const textareaEl = ref<HTMLTextAreaElement | null>(null)
const listEl = ref<HTMLDivElement | null>(null)
const abortCtrl = ref<AbortController | null>(null)
const mobileOpen = ref(false)

const attachments = ref<Attachment[]>([])
const showSessionList = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)
const attachNotice = ref('')

const isCompact = computed(() => !props.open && !props.mobile)

watch(
  () => workspace.currentId,
  async (id) => {
    if (id) {
      session.sessions = []
      session.currentId = ''
      await session.load(id)
    }
  },
  { immediate: true },
)

const messages = computed<ChatMessage[]>(() => session.current()?.messages ?? [])

function scrollToBottom() {
  nextTick(() => {
    if (listEl.value) listEl.value.scrollTop = listEl.value.scrollHeight
  })
}

async function send() {
  const text = input.value.trim()
  if (!text && attachments.value.length === 0) return
  if (session.streaming) return

  attachNotice.value = ''
  // 附件必须先上传并登记成项目资源，未完成时不允许发送
  const pending = attachments.value.filter(
    (a) => a.state === 'selected' || a.state === 'uploading',
  )
  if (pending.length) {
    attachNotice.value = '还有附件正在上传，请稍候'
    return
  }
  const failed = attachments.value.filter((a) => a.state === 'failed')
  if (failed.length) {
    attachNotice.value = '有附件上传失败，请重试或移除后再发送'
    return
  }

  if (!session.current()) {
    await session.createSession(workspace.currentId || '', '新会话')
  }

  const ready = attachments.value.filter((a) => a.state === 'uploaded' && a.resourceId)
  const refs = ready.map((a) => ({
    id: a.resourceId!,
    kind: kindOf(a.name),
    display_name: a.name,
  }))
  // 只回显附件名，真实内容由后端按 resource_refs 取，不把文件名当输入
  const shown: Attachment[] = ready.map((a) => ({ ...a, file: undefined }))

  // 后端 text 必填：只有附件时给一句明确指令，而不是拼文件名
  const content = text || '请查看我上传的附件。'
  input.value = ''
  attachments.value = []
  scrollToBottom()

  abortCtrl.value = new AbortController()
  const sendingSessionId = session.currentId
  await session.send(
    content,
    {
      resourceRefs: refs,
      onDelta: (delta) => {
        const cur = session.sessions.find(s => s.id === sendingSessionId)
        if (cur && cur.messages.length) {
          const last = cur.messages[cur.messages.length - 1]
          if (last.streaming) last.content += delta
        }
        scrollToBottom()
      },
      signal: abortCtrl.value.signal,
    },
    shown,
  )
  abortCtrl.value = null
  scrollToBottom()
}

// 停止只中断本次等待，并同步请求后端停止任务（不假装已经停下）
async function stop() {
  abortCtrl.value?.abort()
  try {
    const tasks = await getAdapter().listTasks(workspace.currentId)
    const active = tasks.find(t => t.session_id === session.currentId && !['completed','stopped','failed','partial_failed'].includes(t.status))
    const taskId = active?.id
    if (!taskId) { attachNotice.value = '没有查到可停止的活动任务，请核对执行状态。'; return }
    await getAdapter().stopTask(taskId, '用户在助手面板停止')
  } catch (e) {
    attachNotice.value = '中断已发送，但后端停止失败：' + (e as Error).message
  }
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault()
    send()
  }
}

function onPaste(e: ClipboardEvent) {
  const items = e.clipboardData?.items
  if (!items) return
  for (const item of items) {
    if (item.type.startsWith('image/')) {
      const f = item.getAsFile()
      if (f) addAttachment(f)
      e.preventDefault()
    }
  }
}

function onDrop(e: DragEvent) {
  e.preventDefault()
  const files = e.dataTransfer?.files
  if (files) {
    for (const f of Array.from(files)) addAttachment(f)
  }
}

function onFileSelect(e: Event) {
  const files = (e.target as HTMLInputElement).files
  if (files) {
    for (const f of Array.from(files)) addAttachment(f)
  }
  ;(e.target as HTMLInputElement).value = ''
}

function kindOf(name: string): ResourceKind {
  const ext = name.split('.').pop()?.toLowerCase() ?? ''
  if (['jpg', 'jpeg', 'png', 'webp', 'bmp'].includes(ext)) return 'image'
  if (ext === 'zip') return 'dataset'
  if (ext === 'pt') return 'model'
  return 'manual'
}

/** 上传并登记为项目资源；失败保留 File 供重试。 */
async function uploadAttachment(a: Attachment) {
  const projectId = workspace.currentId
  if (!projectId) {
    a.state = 'failed'
    a.error = '请先选择工作区'
    return
  }
  if (!a.file) {
    a.state = 'failed'
    a.error = '本机文件已丢失，请重新选择'
    return
  }
  a.state = 'uploading'
  a.error = ''
  try {
    const asset = await getAdapter().uploadProjectFile(projectId, a.file)
    const resource = await getAdapter().registerResource(projectId, {
      kind: kindOf(a.name),
      displayName: a.name,
      metadata: { file_id: asset.file_id, content_type: asset.content_type },
    })
    a.fileId = asset.file_id
    a.resourceId = resource.id
    a.state = 'uploaded'
  } catch (e) {
    a.state = 'failed'
    a.error = (e as Error).message
  }
}

function addAttachment(f: File) {
  const ext = f.name.split('.').pop()?.toLowerCase() ?? ''
  const needsConvert = ext === 'doc'
  const item: Attachment = {
    id: 'a-' + Date.now() + '-' + Math.random().toString(36).slice(2, 6),
    name: f.name,
    type: needsConvert ? 'doc（需转 docx）' : ext,
    size: f.size,
    source: 'local',
    state: 'selected',
    // File 本体必须留下，否则上传失败无法重试、也无法真正上传
    file: f,
  }
  attachments.value.push(item)
  void uploadAttachment(item)
}

async function retryAttachment(a: Attachment) {
  await uploadAttachment(a)
}

function removeAttachment(id: string) {
  attachments.value = attachments.value.filter((x) => x.id !== id)
}

function attachLabel(a: Attachment): string {
  if (a.state === 'uploading') return '上传中…'
  if (a.state === 'uploaded') return '已上传'
  if (a.state === 'failed') return a.error ? `失败：${a.error}` : '上传失败'
  return '待上传'
}

function formatSize(n: number) {
  if (n < 1024) return n + ' B'
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB'
  return (n / 1024 / 1024).toFixed(1) + ' MB'
}

async function newSession() {
  await session.createSession(workspace.currentId || '', '新会话')
  showSessionList.value = false
  scrollToBottom()
}

onMounted(scrollToBottom)
</script>

<template>
  <!-- 桌面：固定右侧栏 -->
  <div v-if="!mobile" class="assistant" :class="{ collapsed: isCompact }" data-guide="assistant">
    <template v-if="isCompact">
      <button class="expand-btn" aria-label="展开巡检助手" @click="emit('toggle')">
        <span class="expand-icon" aria-hidden="true">
          <svg viewBox="0 0 16 16" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.5">
            <path d="M4 8h8M8 4l4 4-4 4" />
          </svg>
        </span>
        <span class="expand-label">巡检助手</span>
      </button>
    </template>
    <template v-else>
      <header class="assistant-header">
        <div class="assistant-title">
          <span class="dot-online" aria-hidden="true"></span>
          <span>巡检助手</span>
        </div>
        <div class="assistant-actions">
          <button class="icon-btn" aria-label="新建会话" @click="newSession">
            <svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M8 3v10M3 8h10" /></svg>
          </button>
          <button class="icon-btn" aria-label="会话列表" @click="showSessionList = !showSessionList">
            <svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 3h12M2 8h12M2 13h12" /></svg>
          </button>
          <button class="icon-btn" aria-label="收起助手" @click="emit('toggle')">
            <svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M12 4l-4 4 4 4" /></svg>
          </button>
        </div>
      </header>

      <div v-if="showSessionList" class="session-list panel">
        <div class="session-list-title">会话历史</div>
        <button
          v-for="s in session.sessions"
          :key="s.id"
          class="session-item"
          :class="{ current: s.id === session.currentId }"
          @click="session.setCurrent(s.id); showSessionList = false"
        >
          {{ s.title }} <span class="session-time num">{{ new Date(s.created_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) }}</span>
        </button>
        <div v-if="!session.sessions.length" class="session-empty">暂无会话</div>
      </div>

      <div ref="listEl" class="assistant-messages">
        <AgentProgress />
        <div v-if="!messages.length" class="assistant-empty">
          <p>我是海巡智检巡检助手，可以协助你发起巡检、核对计划和处理复核。</p>
        </div>
        <div v-for="msg in messages" :key="msg.id" class="msg" :class="msg.role">
          <div class="msg-bubble">
            <div v-if="msg.attachments?.length" class="msg-attachments">
              <div v-for="a in msg.attachments" :key="a.id" class="msg-attach">{{ a.name }}</div>
            </div>
            <div class="msg-text">{{ msg.content }}</div>
            <span v-if="msg.streaming" class="cursor" aria-hidden="true">▍</span>
          </div>
        </div>
      </div>

      <footer class="assistant-input">
        <div v-if="attachments.length" class="attach-list">
          <div
            v-for="a in attachments"
            :key="a.id"
            class="attach-chip"
            :class="'attach-' + a.state"
          >
            <span class="attach-name">{{ a.name }}</span>
            <span class="attach-meta">{{ a.type }} · {{ formatSize(a.size) }} · {{ attachLabel(a) }}</span>
            <button
              v-if="a.state === 'failed'"
              class="attach-retry"
              @click="retryAttachment(a)"
            >重试</button>
            <button class="attach-remove" aria-label="移除附件" @click="removeAttachment(a.id)">✕</button>
          </div>
        </div>
        <div v-if="attachNotice" class="attach-notice">{{ attachNotice }}</div>
        <div class="input-row">
          <textarea
            ref="textareaEl"
            v-model="input"
            class="assistant-textarea"
            rows="1"
            placeholder="输入消息，Enter 发送，Shift+Enter 换行"
            @keydown="onKeydown"
            @paste="onPaste"
            @drop.prevent="onDrop"
            @dragover.prevent
          ></textarea>
          <div class="input-tools">
            <button class="icon-btn" aria-label="添加附件" @click="fileInput?.click()">
              <svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M8 12V4M5 7l3-3 3 3" /></svg>
            </button>
            <input ref="fileInput" type="file" multiple hidden accept=".jpg,.jpeg,.png,.webp,.zip,.pt,.pdf,.docx,.doc,.md,.txt" @change="onFileSelect" />
            <button
              v-if="session.streaming"
              class="icon-btn stop-btn"
              aria-label="停止生成"
              @click="stop"
            >
              <svg viewBox="0 0 16 16" width="14" height="14" fill="currentColor"><rect x="4" y="4" width="8" height="8" rx="1" /></svg>
            </button>
            <button v-else class="icon-btn send-btn" :disabled="!input.trim() && !attachments.length" aria-label="发送" @click="send">
              <svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M14 2L7 9M14 2l-4 12-3-5-5-3z" /></svg>
            </button>
          </div>
        </div>
        <div class="input-hint">Enter 发送 · Shift+Enter 换行 · 支持粘贴/拖拽图片</div>
      </footer>
    </template>
  </div>

  <!-- 移动端：可关闭抽屉 -->
  <div v-else class="assistant-mobile">
    <button v-if="!mobileOpen" class="fab" aria-label="打开巡检助手" @click="mobileOpen = true">
      <svg viewBox="0 0 16 16" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 2h12v9H5l-3 3V2z" /></svg>
    </button>
    <template v-if="mobileOpen">
      <div class="mobile-scrim" @click="mobileOpen = false"></div>
      <div class="assistant mobile-panel">
        <header class="assistant-header">
          <div class="assistant-title"><span class="dot-online" aria-hidden="true"></span><span>巡检助手</span></div>
          <button class="icon-btn" aria-label="关闭助手" @click="mobileOpen = false">
            <svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M3 3l10 10M13 3L3 13" /></svg>
          </button>
        </header>
        <div ref="listEl" class="assistant-messages">
          <AgentProgress />
          <div v-for="msg in messages" :key="msg.id" class="msg" :class="msg.role">
            <div class="msg-bubble">
              <div class="msg-text">{{ msg.content }}</div>
              <span v-if="msg.streaming" class="cursor" aria-hidden="true">▍</span>
            </div>
          </div>
        </div>
        <footer class="assistant-input">
          <div class="input-row">
            <textarea
              v-model="input"
              class="assistant-textarea"
              rows="1"
              placeholder="输入消息…"
              @keydown="onKeydown"
            ></textarea>
            <div class="input-tools">
              <button class="icon-btn send-btn" :disabled="!input.trim()" aria-label="发送" @click="send">
                <svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M14 2L7 9M14 2l-4 12-3-5-5-3z" /></svg>
              </button>
            </div>
          </div>
        </footer>
      </div>
    </template>
  </div>
</template>

<style scoped>
.assistant {
  width: var(--assistant-w);
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  background: var(--panel);
  border-left: 1px solid var(--border);
  height: 100vh;
}
.assistant.collapsed {
  width: 44px;
  align-items: center;
}
.expand-btn {
  margin-top: 16px;
  width: 36px;
  height: 36px;
  border: 1px solid var(--border-control);
  background: var(--panel);
  border-radius: var(--radius-sm);
  cursor: pointer;
  color: var(--text-secondary);
  display: flex;
  align-items: center;
  justify-content: center;
}
.expand-label {
  display: none;
}
.assistant-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 52px;
  padding: 0 12px;
  border-bottom: 1px solid var(--border);
}
.assistant-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  font-weight: 600;
}
.dot-online {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--success);
}
.assistant-actions {
  display: flex;
  gap: 2px;
}
.icon-btn {
  width: 30px;
  height: 30px;
  border: none;
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  border-radius: var(--radius-sm);
  display: flex;
  align-items: center;
  justify-content: center;
}
.icon-btn:hover {
  background: var(--bg-selected);
  color: var(--text);
}
.icon-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.send-btn {
  color: var(--accent);
}
.stop-btn {
  color: var(--danger);
}

.session-list {
  position: absolute;
  top: 52px;
  left: 0;
  right: 0;
  z-index: 20;
  max-height: 300px;
  overflow: auto;
  box-shadow: var(--shadow-md);
  border: 1px solid var(--border);
  border-top: none;
}
.session-list-title {
  font-size: 11px;
  color: var(--text-secondary);
  padding: 10px 12px 4px;
}
.session-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  width: 100%;
  padding: 9px 12px;
  border: none;
  background: transparent;
  color: var(--text);
  font-size: 13px;
  cursor: pointer;
  text-align: left;
}
.session-item:hover {
  background: var(--panel-secondary);
}
.session-item.current {
  background: var(--bg-selected);
}
.session-time {
  font-size: 11px;
  color: var(--text-secondary);
}
.session-empty {
  padding: 12px;
  font-size: 12px;
  color: var(--text-secondary);
}

.assistant-messages {
  flex: 1;
  overflow-y: auto;
  padding: 16px 12px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.assistant-empty {
  color: var(--text-secondary);
  font-size: 13px;
  text-align: center;
  margin-top: 40px;
  line-height: 1.6;
}
.msg {
  display: flex;
}
.msg.user {
  justify-content: flex-end;
}
.msg-bubble {
  max-width: 85%;
  padding: 10px 12px;
  border-radius: var(--radius-md);
  font-size: 13px;
  line-height: 1.55;
  white-space: pre-wrap;
  word-break: break-word;
}
.msg.user .msg-bubble {
  background: var(--btn-primary);
  color: var(--btn-primary-text);
  border-bottom-right-radius: 4px;
}
.msg.assistant .msg-bubble {
  background: var(--panel-secondary);
  color: var(--text);
  border-bottom-left-radius: 4px;
}
.msg-text {
  white-space: pre-wrap;
}
.cursor {
  animation: blink 0.8s step-end infinite;
  color: var(--accent);
}
@keyframes blink {
  50% {
    opacity: 0;
  }
}
.msg-attachments {
  margin-bottom: 6px;
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.msg-attach {
  font-size: 11px;
  padding: 2px 6px;
  background: var(--bg-selected);
  border-radius: 4px;
}

.assistant-input {
  border-top: 1px solid var(--border);
  padding: 10px 12px;
}
.attach-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-bottom: 8px;
}
.attach-chip {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  padding: 6px 8px;
  background: var(--panel-secondary);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}
.attach-name {
  font-weight: 500;
}
.attach-meta {
  color: var(--text-secondary);
  font-size: 11px;
}
.attach-chip.attach-uploaded {
  border-color: var(--success-border, #b7dfc4);
}
.attach-chip.attach-failed {
  border-color: var(--danger-border, #f0b8b8);
  background: var(--danger-bg, #fdf1f1);
}
.attach-retry {
  border: 1px solid var(--border-control);
  background: transparent;
  color: var(--text-secondary);
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 4px;
  cursor: pointer;
}
.attach-notice {
  font-size: 11px;
  color: var(--warn-text);
  margin-bottom: 6px;
}
.attach-remove {
  margin-left: auto;
  border: none;
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
}
.input-row {
  display: flex;
  align-items: flex-end;
  gap: 6px;
}
.assistant-textarea {
  flex: 1;
  border: 1px solid var(--border-control);
  border-radius: var(--radius-md);
  padding: 8px 10px;
  font-size: 13px;
  font-family: inherit;
  background: var(--panel);
  color: var(--text);
  resize: none;
  max-height: 120px;
  line-height: 1.5;
}
.assistant-textarea:focus {
  outline: none;
  border-color: var(--accent);
  box-shadow: var(--focus-ring);
}
.input-tools {
  display: flex;
  gap: 2px;
}
.input-hint {
  font-size: 11px;
  color: var(--text-secondary);
  margin-top: 6px;
  text-align: center;
}

/* 移动端：外层不占布局空间 */
.assistant-mobile {
  position: fixed;
  inset: 0;
  pointer-events: none;
  z-index: 960;
}
.assistant-mobile > * {
  pointer-events: auto;
}
.fab {
  position: fixed;
  right: 16px;
  bottom: 16px;
  width: 52px;
  height: 52px;
  border-radius: 50%;
  border: none;
  background: var(--btn-primary);
  color: var(--btn-primary-text);
  cursor: pointer;
  box-shadow: var(--shadow-md);
  display: flex;
  align-items: center;
  justify-content: center;
}
.mobile-scrim {
  position: fixed;
  inset: 0;
  background: var(--scrim);
  z-index: 961;
}
.mobile-panel {
  position: fixed;
  top: 0;
  right: 0;
  bottom: 0;
  left: 40px;
  z-index: 962;
  height: auto;
  box-shadow: var(--shadow-md);
}
</style>
