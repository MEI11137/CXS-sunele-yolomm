<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import ChatTimeline from './ChatTimeline.vue'
import { turnStatusLabel } from '@/lib/timeline'
import { useAgentStore } from '@/stores/agent'
import { useConnectionStore } from '@/stores/connection'
import { useWorkspaceStore } from '@/stores/workspace'
import type { PermissionMode } from '@/types'

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

const input = ref('')
const textarea = ref<HTMLTextAreaElement | null>(null)
const timeline = ref<InstanceType<typeof ChatTimeline> | null>(null)

const sessionId = computed(() => String(route.params.sessionId ?? ''))
const projectId = computed(() => String(route.params.projectId ?? ''))
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
  },
  { immediate: true },
)

onMounted(() => {
  void connection.load()
  if (!agent.ready) void agent.loadCapabilities()
  void agent.loadPreferences()
})

async function send(): Promise<void> {
  const text = input.value.trim()
  if (!text || !sessionId.value) return
  const ok = await agent.send(sessionId.value, text)
  if (ok) input.value = ''
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
  <aside class="assistant" :class="props.variant === 'center' ? 'assistant-center' : 'assistant-dock'" aria-label="海巡助手">
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
        @keydown.enter.exact.prevent="send"
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
        <span class="hint assistant-runtime">{{ runtimeLine }}</span>
        <button class="primary" type="button" :disabled="!canSend" @click="send">
          {{ agent.sending ? '发送中…' : '发送' }}
        </button>
      </div>
      <p v-if="agent.preferences?.mode_hint" class="hint">{{ agent.preferences.mode_hint }}</p>
    </footer>
  </aside>
</template>
