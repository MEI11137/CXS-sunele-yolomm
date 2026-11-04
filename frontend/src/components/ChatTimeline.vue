<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { renderMarkdown } from '@/lib/markdown'
import {
  formatDuration,
  hasPersistedAnswer,
  isTerminalStatus,
  latestNotice,
  liveSectionVisible,
  nearBottom,
  streamingText,
  toolCards,
  toolStateLabel,
  turnStatusLabel,
} from '@/lib/timeline'
import type { ChatMessage, Turn, TurnEvent } from '@/types'

const props = defineProps<{
  /** 当前会话 ID：用来核对传进来的消息确实属于这个会话。 */
  sessionId: string
  messages: ChatMessage[]
  turn: Turn | null
  events: TurnEvent[]
  pollIssue?: string
}>()

const emit = defineEmits<{
  pick: [text: string]
  navigate: [route: unknown]
}>()

const viewport = ref<HTMLDivElement | null>(null)
const stuck = ref(true)

/**
 * 只渲染属于当前会话的消息。
 *
 * 上层如果因为缓存/竞态把别的会话的消息传进来，这里也不会串台：
 * 宁可少显示，也不要把 A 会话的回答显示在 B 会话里。
 */
const scoped = computed(() =>
  props.sessionId
    ? props.messages.filter((item) => !item.session_id || item.session_id === props.sessionId)
    : props.messages,
)

const cards = computed(() => toolCards(props.events))
const streamed = computed(() => streamingText(props.events))
const terminal = computed(() => Boolean(props.turn && isTerminalStatus(props.turn.status)))
/** 这一轮的最终回答是否已经作为消息落库（落库后就不再重复渲染流式文本）。 */
const persisted = computed(
  () => Boolean(props.turn) && hasPersistedAnswer(scoped.value, props.turn?.id ?? ''),
)
const showLive = computed(
  () => Boolean(props.turn) && liveSectionVisible({ status: props.turn!.status, persisted: persisted.value }),
)
/** 终态但没有落库回答（失败/停止）：把这一轮的过程折叠保留下来，便于追溯。 */
const showRoundDetail = computed(
  () => Boolean(props.turn) && terminal.value && !persisted.value && (cards.value.length > 0 || Boolean(streamed.value)),
)
const notice = computed(() => {
  // 轮次已经结束：“已请求停止，等待引擎确认…”这类中间提示就过期了，不再展示。
  if (terminal.value) return ''
  return latestNotice(props.events)
})
const failure = computed(() => {
  const turn = props.turn
  if (!turn || !isTerminalStatus(turn.status)) return ''
  if (turn.status === 'stopped') return '本轮已停止，已完成的工具结果保留。'
  if (turn.status === 'failed') return turn.error_message || '本轮失败，没有可展示的最终回答。'
  return ''
})
const questions = computed(() => {
  const list: { seq: number; question: string; options: string[] }[] = []
  for (const event of props.events) {
    if (event.type !== 'question') continue
    const question = String(event.payload?.question ?? '')
    if (!question) continue
    const options = Array.isArray(event.payload?.options)
      ? (event.payload.options as unknown[]).map((item) => String(item)).filter(Boolean)
      : []
    list.push({ seq: event.seq, question, options })
  }
  return list.slice(-1)
})

const navigations = computed(() => {
  const list: { seq: number; page: string; route: unknown }[] = []
  for (const event of props.events) {
    if (event.type !== 'ui.navigate') continue
    list.push({ seq: event.seq, page: String(event.payload?.page ?? ''), route: event.payload?.route })
  }
  return list.slice(-1)
})

const contentKey = computed(
  () =>
    `${scoped.value.length}:${scoped.value.map((item) => item.content.length).join(',')}:` +
    `${props.events.length}:${streamed.value.length}:${showLive.value}`,
)

function onScroll(): void {
  const element = viewport.value
  if (!element) return
  stuck.value = nearBottom({
    scrollHeight: element.scrollHeight,
    scrollTop: element.scrollTop,
    clientHeight: element.clientHeight,
  })
}

function toLatest(smooth = false): void {
  const element = viewport.value
  if (!element) return
  element.scrollTo({ top: element.scrollHeight, behavior: smooth ? 'smooth' : 'auto' })
  stuck.value = true
}

watch(contentKey, async () => {
  await nextTick()
  // 用户上翻查看历史时不要抢滚动条；只有贴着底部才跟随新内容。
  if (stuck.value) toLatest()
})

watch(
  () => navigations.value.length && navigations.value[navigations.value.length - 1].seq,
  (seq) => {
    if (!seq) return
    const last = navigations.value[navigations.value.length - 1]
    if (last?.route) emit('navigate', last.route)
  },
)

onMounted(async () => {
  await nextTick()
  toLatest()
})

defineExpose({ toLatest })
</script>

<template>
  <div class="timeline-wrap">
    <div ref="viewport" class="timeline" aria-label="对话消息" @scroll.passive="onScroll">
      <p v-if="!scoped.length && !turn" class="timeline-empty">
        还没有消息。直接描述你要做的事，例如「看看这个目录里有哪些图片」。
      </p>

      <article
        v-for="message in scoped"
        :key="message.id"
        class="message"
        :class="`message-${message.role}`"
      >
        <span class="message-author">{{ message.role === 'user' ? '我' : '海巡助手' }}</span>
        <div
          v-if="message.role === 'assistant'"
          class="message-text markdown-body"
          v-html="renderMarkdown(message.content)"
        ></div>
        <p v-else class="message-text">{{ message.content }}</p>
      </article>

      <!-- 进行中的这一轮：工具进度 + 流式正文。终态后交给落库消息，避免显示两遍。 -->
      <section v-if="showLive" class="live-turn" aria-live="polite">
        <div class="live-head">
          <span class="live-dot" :class="{ busy: true }" aria-hidden="true"></span>
          <span class="live-status">{{ turn ? turnStatusLabel(turn.status) : '' }}</span>
          <span v-if="turn?.mode" class="live-meta">
            {{ turn.mode === 'full' ? '完全访问' : turn.mode === 'assist' ? '帮我批准' : '请求批准' }}
          </span>
          <span v-if="turn?.model" class="live-meta mono">{{ turn.model }}</span>
        </div>

        <ul v-if="cards.length" class="tool-list">
          <li v-for="card in cards" :key="card.callId" class="tool-card">
            <details>
              <summary>
                <span class="tool-state" :data-state="card.state">{{ toolStateLabel(card.state) }}</span>
                <span class="tool-title">{{ card.title }}</span>
                <span v-if="card.durationMs !== null" class="tool-time">{{ formatDuration(card.durationMs) }}</span>
              </summary>
              <pre v-if="card.preview" class="tool-preview">{{ card.preview }}</pre>
              <p v-else class="hint">这个工具没有输出摘要。</p>
            </details>
          </li>
        </ul>

        <div v-if="streamed" class="message message-assistant">
          <span class="message-author">海巡助手</span>
          <div class="message-text markdown-body" v-html="renderMarkdown(streamed)"></div>
        </div>

        <p v-for="item in questions" :key="item.seq" class="live-question">
          {{ item.question }}
          <button
            v-for="option in item.options"
            :key="option"
            class="ghost small"
            type="button"
            @click="emit('pick', option)"
          >
            {{ option }}
          </button>
        </p>
      </section>

      <!-- 终态但没有落库回答（失败/停止）：过程仍可查，不假装成功。 -->
      <details v-else-if="showRoundDetail" class="live-turn round-detail">
        <summary class="live-head">
          <span class="live-dot" aria-hidden="true"></span>
          <span class="live-status">{{ turn ? turnStatusLabel(turn.status) : '' }}</span>
          <span class="live-meta">本轮过程：{{ cards.length }} 次工具调用</span>
        </summary>
        <ul v-if="cards.length" class="tool-list">
          <li v-for="card in cards" :key="card.callId" class="tool-card">
            <details>
              <summary>
                <span class="tool-state" :data-state="card.state">{{ toolStateLabel(card.state) }}</span>
                <span class="tool-title">{{ card.title }}</span>
                <span v-if="card.durationMs !== null" class="tool-time">{{ formatDuration(card.durationMs) }}</span>
              </summary>
              <pre v-if="card.preview" class="tool-preview">{{ card.preview }}</pre>
              <p v-else class="hint">这个工具没有输出摘要。</p>
            </details>
          </li>
        </ul>
        <div v-if="streamed" class="message message-assistant">
          <span class="message-author">海巡助手（未保存的输出）</span>
          <div class="message-text markdown-body" v-html="renderMarkdown(streamed)"></div>
        </div>
      </details>

      <p v-if="failure" class="banner banner-warn" role="status">{{ failure }}</p>
      <p v-if="notice" class="hint">{{ notice }}</p>
      <p v-if="pollIssue" class="form-error" role="alert">{{ pollIssue }}</p>
    </div>

    <button v-if="!stuck" class="to-latest" type="button" @click="toLatest(true)">回到最新 ↓</button>
  </div>
</template>
