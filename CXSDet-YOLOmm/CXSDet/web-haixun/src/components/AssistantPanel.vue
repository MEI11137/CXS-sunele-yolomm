<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted, onBeforeUnmount } from 'vue'
import { useSessionStore } from '@/stores/session'
import { useWorkspaceStore } from '@/stores/workspace'
import { useAuthStore } from '@/stores/auth'
import { getAdapter } from '@/services'
import { useRouter } from 'vue-router'
import { projectPath } from '@/utils/projectRoutes'
import { reviewContext } from '@/utils/reviewBridge'
import type { ChatMessage, Attachment, ResourceKind } from '@/types'
import AgentProgress from './AgentProgress.vue'
import ChatTimeline from './ChatTimeline.vue'
import ChatModelPicker from './ChatModelPicker.vue'
import ChatPermissionPicker from './ChatPermissionPicker.vue'

const props = defineProps<{ open: boolean; mobile?: boolean; primary?: boolean }>()
const emit = defineEmits<{ toggle: [] }>()

const session = useSessionStore()
const workspace = useWorkspaceStore()
const auth = useAuthStore()
const router = useRouter()

const input = ref('')
const textareaEl = ref<HTMLTextAreaElement | null>(null)
const timeline = ref<InstanceType<typeof ChatTimeline> | null>(null)
const approvalHost = ref<HTMLElement | null>(null)
const taskRunning = ref(false)
const abortCtrl = ref<AbortController | null>(null)
const mobileOpen = ref(false)

const attachments = ref<Attachment[]>([])
const showSessionList = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)
const attachNotice = ref('')
const drafts = new Map<string, { text: string; attachments: Attachment[] }>()
defineExpose({ openConversation: () => { mobileOpen.value = true } })

const isCompact = computed(() => !props.open && !props.mobile)

watch(() => [auth.user?.id, workspace.currentId, session.currentId], (next, previous) => {
  if (previous && previous[0] !== next[0]) drafts.clear()
  else if (previous) drafts.set(JSON.stringify(previous), { text: input.value, attachments: attachments.value })
  const draft = drafts.get(JSON.stringify(next))
  input.value = draft?.text || ''
  attachments.value = draft?.attachments || []
  attachNotice.value = ''
  showSessionList.value = false
  taskRunning.value = false
})

const messages = computed<ChatMessage[]>(() => session.current()?.messages ?? [])
watch(() => session.queuedAttachments[workspace.currentId], values => {
  if (!values?.length) return
  attachments.value.push(...values.filter(file => !attachments.value.some(existing => existing.resourceId === file.resourceId)))
  delete session.queuedAttachments[workspace.currentId]
  if (!input.value) input.value = '请先检查这些项目附件，再引导我完成数据集导入或图片巡检。'
}, { immediate: true })
// 输入框随文字增长，长草稿在框内滚动，不撑开整个页面。
watch(input, () => nextTick(() => {
  const el = textareaEl.value
  if (el) { el.style.height = 'auto'; el.style.height = Math.min(el.scrollHeight, 200) + 'px' }
}))

function scrollToBottom() { timeline.value?.jumpToLatest() }

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

  const projectId = workspace.currentId
  if (!projectId) { attachNotice.value = '请先选择工作区'; return }
  const ready = attachments.value.filter((a) => a.state === 'uploaded' && a.resourceId)
  const refs = ready.map((a) => ({
    id: a.resourceId!,
    kind: kindOf(a.name),
    display_name: a.name,
  }))
  // 只回显附件名，真实内容由后端按 resource_refs 取，不把文件名当输入
  const shown: Attachment[] = ready.map((a) => ({ ...a, file: undefined }))

  if (!session.current() || session.current()?.project_id !== projectId) {
    try { await session.createSession(projectId, '新会话') }
    catch (e) { attachNotice.value = (e as Error).message; return }
  }
  if (workspace.currentId !== projectId) return

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
      reviewContext: reviewContext(projectId),
      onDelta: (delta) => {
        const cur = session.sessions.find(s => s.id === sendingSessionId)
        if (cur && cur.messages.length) {
          const last = cur.messages[cur.messages.length - 1]
          if (last.streaming) last.content += delta
        }
      },
      signal: abortCtrl.value.signal,
    },
    shown,
  )
  abortCtrl.value = null
  if (session.currentId === sendingSessionId) textareaEl.value?.focus()
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
  try {
    const projectId = workspace.currentId
    const chat = await session.createSession(projectId || '', '新会话')
    if (workspace.currentId === projectId && props.primary) await router.push(projectPath(projectId, 'chat/' + chat.id))
    showSessionList.value = false
    scrollToBottom()
  } catch (e) { attachNotice.value = (e as Error).message }
}

async function openHistory(id: string) {
  try { await session.selectSession(id); showSessionList.value = false; scrollToBottom() }
  catch (e) { attachNotice.value = (e as Error).message }
}

function controlTimeline(event: Event) {
  const p = (event as CustomEvent).detail
  if (p.source_project_id !== workspace.currentId || p.source_session_id !== session.currentId || p.command !== 'scroll') return
  if (p.value === 'top') timeline.value?.jumpToTop()
  else if (p.value === 'latest') timeline.value?.jumpToLatest()
}
onMounted(() => { scrollToBottom(); window.addEventListener('haixun:ui', controlTimeline) })
onBeforeUnmount(() => window.removeEventListener('haixun:ui', controlTimeline))
</script>

<template>
  <div v-if="mobile && !mobileOpen" class="assistant-mobile"><button class="fab" aria-label="打开海巡助手" @click="mobileOpen = true">✳</button></div>
  <div v-if="mobile && mobileOpen" class="mobile-scrim" @click="mobileOpen = false"></div>
  <div v-if="!mobile || mobileOpen" class="assistant" :class="{ collapsed: isCompact, primary, 'mobile-panel': mobile }" data-guide="assistant">
    <button v-if="isCompact" class="expand-btn" aria-label="展开海巡助手" @click="emit('toggle')">✳</button>
    <template v-else>
      <header class="assistant-header">
        <div class="assistant-title"><span class="assistant-mark" aria-hidden="true">✳</span><div><strong>海巡助手</strong><span class="session-caption">{{ session.current()?.title || '新的对话' }}</span></div></div>
        <div class="assistant-actions">
          <button class="icon-btn" :aria-label="`在${workspace.current?.name || '当前项目'}中新建会话`" :title="`在「${workspace.current?.name || '当前项目'}」中新建会话`" :disabled="!workspace.currentId || session.creating[workspace.currentId]" @click="newSession"><svg viewBox="0 0 20 20" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M10 4v12M4 10h12" /></svg></button>
          <button v-if="!primary" class="icon-btn" aria-label="当前项目会话列表" :aria-expanded="showSessionList" @click="showSessionList = !showSessionList"><svg viewBox="0 0 20 20" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M4 5h12M4 10h12M4 15h12" /></svg></button>
          <button v-if="mobile" class="icon-btn" aria-label="关闭助手" @click="mobileOpen = false">×</button>
          <button v-else-if="!primary" class="icon-btn" aria-label="收起助手" @click="emit('toggle')">›</button>
        </div>
      </header>
      <div v-if="!primary" class="assistant-context" :title="workspace.current?.root_directory || ''">{{ workspace.current?.name || '请先选择项目' }}</div>
      <div v-if="showSessionList" class="session-list">
        <div class="session-list-title">{{ workspace.current?.name }} · 会话历史</div>
        <button v-for="s in session.projectSessions(workspace.currentId)" :key="s.id" class="session-item" :class="{ current: s.id === session.currentId }" @click="openHistory(s.id)">{{ s.title }}<span class="session-time">{{ new Date(s.created_at).toLocaleDateString('zh-CN') }}</span></button>
        <div v-if="!session.projectSessions(workspace.currentId).length" class="session-empty">当前项目暂无会话</div>
      </div>
      <ChatTimeline ref="timeline" :messages="messages" :conversation-key="`${workspace.currentId}/${session.currentId}`">
        <template #progress><AgentProgress :approval-target="approvalHost" @running="taskRunning = $event" /></template>
      </ChatTimeline>
      <div ref="approvalHost" class="assistant-approvals"></div>
      <footer class="assistant-input" @drop.prevent="onDrop" @dragover.prevent>
        <div v-if="attachments.length" class="attach-list">
          <div v-for="a in attachments" :key="a.id" class="attach-chip" :class="'attach-' + a.state"><span class="attach-name">▧ {{ a.name }}</span><span class="attach-meta">{{ formatSize(a.size) }} · {{ attachLabel(a) }}</span><button v-if="a.state === 'failed'" class="attach-retry" @click="retryAttachment(a)">重试</button><button class="icon-btn remove-attachment" aria-label="移除附件" @click="removeAttachment(a.id)">×</button></div>
        </div>
        <div v-if="attachNotice" class="attach-notice" role="status">{{ attachNotice }}</div>
        <textarea ref="textareaEl" v-model="input" class="assistant-textarea" rows="2" aria-label="发送给海巡助手的消息" placeholder="描述你的目标，或拖入图片和文件…" @keydown="onKeydown" @paste="onPaste"></textarea>
        <div class="composer-toolbar">
          <div class="composer-options"><button class="icon-btn attach-button" aria-label="添加附件" title="添加图片、ZIP 数据集、PT 权重或资料" @click="fileInput?.click()"><svg viewBox="0 0 20 20" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M10 4v12M4 10h12" /></svg></button><input ref="fileInput" type="file" multiple hidden accept=".jpg,.jpeg,.png,.webp,.zip,.pt,.pdf,.docx,.doc,.md,.txt" @change="onFileSelect" /><ChatPermissionPicker :disabled="session.streaming || taskRunning" /><ChatModelPicker :disabled="session.streaming || taskRunning" /></div>
          <button v-if="session.streaming || taskRunning" class="send-button stop-button" aria-label="停止生成" title="停止本次执行" @click="stop"><svg viewBox="0 0 20 20" width="15" height="15" fill="currentColor"><rect x="5" y="5" width="10" height="10" rx="2" /></svg></button>
          <button v-else class="send-button" :disabled="!input.trim() && !attachments.length" aria-label="发送" title="发送消息" @click="send"><svg viewBox="0 0 20 20" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M10 16V4m-5 5 5-5 5 5" /></svg></button>
        </div>
      </footer>
      <div class="input-hint">Enter 发送 · Shift + Enter 换行<span v-if="session.streaming || taskRunning"> · 正在处理，可停止</span><span v-else> · 重要结果请核实</span></div>
    </template>
  </div>
</template>

<style scoped>
.assistant { position: relative; width: var(--assistant-w); flex-shrink: 0; display: flex; flex-direction: column; background: var(--panel); border-left: 1px solid var(--border); height: 100%; min-height: 0; min-width: 0; }
.assistant.primary { border-left: 0; width: 100%; }
.assistant.collapsed { width: 44px; align-items: center; }
.assistant-header { display: flex; align-items: center; justify-content: space-between; min-height: 62px; flex-shrink: 0; padding: 12px 20px; gap: 10px; }
.assistant-title { display: flex; align-items: center; gap: 10px; min-width: 0; }
.assistant-title > div { display: flex; flex-direction: column; min-width: 0; gap: 4px; }
.assistant-title strong { font-size: 13px; font-weight: 600; }
.assistant-mark { font-size: 24px; }
.session-caption { font-size: 11px; color: var(--text-secondary); overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.assistant-context { padding: 0 20px 8px; color: var(--text-secondary); font-size: 11px; }
.assistant-actions { display: flex; flex-shrink: 0; gap: 3px; }
.icon-btn, .expand-btn { display: flex; align-items: center; justify-content: center; border: 0; background: transparent; width: 32px; height: 32px; color: var(--text-secondary); border-radius: 8px; cursor: pointer; font-size: 23px; }
.icon-btn:hover, .expand-btn:hover { background: var(--panel-secondary); color: var(--text); }
.icon-btn:disabled { opacity: .4; cursor: not-allowed; }
.expand-btn { margin-top: 14px; }
.session-list { position: absolute; top: 60px; left: 10px; right: 10px; border: 1px solid var(--border); border-radius: 12px; background: var(--panel); z-index: 20; max-height: 320px; overflow: auto; box-shadow: var(--shadow-md); padding: 8px; }
.session-list-title, .session-empty { font-size: 11px; color: var(--text-secondary); padding: 10px 8px; }
.session-item { display: flex; align-items: center; gap: 8px; justify-content: space-between; width: 100%; padding: 10px 8px; border: 0; border-radius: 7px; background: transparent; color: var(--text); text-align: left; font-size: 12px; cursor: pointer; }
.session-item:hover, .session-item.current { background: var(--panel-secondary); }
.session-time { font-size: 10px; color: var(--text-secondary); white-space: nowrap; }
.assistant-input { flex-shrink: 0; width: calc(100% - 28px); max-width: 810px; align-self: center; margin: 0 14px; padding: 12px 12px 8px; border: 1px solid var(--border-control); border-radius: 18px; background: var(--panel-secondary); transition: border-color .12s; }
.assistant-input:focus-within { border-color: var(--text-secondary); }
.assistant-approvals { flex-shrink: 0; min-height: 0; width: calc(100% - 28px); max-width: 810px; align-self: center; margin: 0 14px 10px; }
.assistant-approvals:empty { display: none; }
.primary .assistant-approvals { width: calc(100% - 48px); margin-inline: 24px; }
.primary .assistant-input { width: calc(100% - 48px); margin: 0 24px; }
.assistant-textarea { display: block; width: 100%; min-height: 52px; max-height: 200px; overflow-y: auto; resize: none; border: 0; border-radius: 8px; background: transparent; color: var(--text); font: inherit; font-size: 14px; line-height: 1.65; padding: 2px 4px 8px; }
.assistant-textarea:focus-visible { box-shadow: none; outline: none; }
.assistant-textarea::placeholder { color: var(--text-secondary); opacity: .85; }
.composer-toolbar, .composer-options { display: flex; align-items: center; gap: 5px; min-width: 0; }
.composer-toolbar { justify-content: space-between; }
.composer-options { flex-wrap: wrap; }
.attach-button { flex-shrink: 0; }
.send-button { display: flex; align-items: center; justify-content: center; border: 0; background: var(--btn-primary); color: var(--btn-primary-text); width: 34px; height: 34px; flex-shrink: 0; border-radius: 50%; cursor: pointer; }
.send-button:disabled { opacity: .25; cursor: not-allowed; }
.send-button:not(:disabled):hover { opacity: .8; }
.stop-button { border-radius: 10px; }
.input-hint { padding: 8px 12px 12px; text-align: center; color: var(--text-secondary); font-size: 10px; flex-shrink: 0; }
.attach-list { display: flex; flex-wrap: wrap; gap: 6px; max-height: 140px; overflow: auto; margin-bottom: 8px; }
.attach-chip { display: flex; align-items: center; gap: 6px; min-width: 0; max-width: 100%; border: 1px solid var(--border); background: var(--panel); border-radius: 9px; padding: 6px 8px; font-size: 11px; }
.attach-name { max-width: 180px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.attach-meta { font-size: 10px; color: var(--text-secondary); }
.attach-failed { border-color: var(--danger); }
.attach-notice { font-size: 12px; color: var(--warn-text); padding: 0 4px 10px; }
.attach-retry { background: transparent; border: 0; color: var(--accent); cursor: pointer; }
.remove-attachment { width: 20px; height: 20px; font-size: 16px; }
.assistant:not(.primary) :deep(.message-text) { font-size: 13px; }
.assistant:not(.primary) :deep(.chat-column) { padding: 16px 16px 40px; }
.assistant-mobile { position: fixed; bottom: 16px; right: 16px; z-index: 960; }
.fab { width: 50px; height: 50px; border-radius: 50%; border: 0; font-size: 25px; background: var(--btn-primary); color: var(--btn-primary-text); box-shadow: var(--shadow-md); cursor: pointer; }
.mobile-scrim { position: fixed; inset: 0; background: var(--scrim); z-index: 961; }
.assistant.mobile-panel { position: fixed; inset: 0 0 0 24px; width: auto; height: auto; z-index: 962; box-shadow: var(--shadow-md); }
@media (max-width: 768px) { .assistant.primary { flex: 1; height: auto; } .assistant-header { padding: 10px 16px; } .primary .assistant-input, .primary .assistant-approvals { width: calc(100% - 24px); margin-inline: 12px; } .input-hint { padding-bottom: max(10px, env(safe-area-inset-bottom)); } }
</style>
