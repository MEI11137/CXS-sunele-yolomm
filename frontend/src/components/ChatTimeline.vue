<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { renderMarkdown } from '@/lib/markdown'
import {
  formatDuration,
  hasPersistedAnswer,
  isTerminalStatus,
  latestNotice,
  nearBottom,
  streamingText,
  toolCards,
  toolFoldSummary,
  toolStateLabel,
  turnStatusLabel,
} from '@/lib/timeline'
import { recordStatusLabel } from '@/lib/business'
import type { BusinessCard } from '@/lib/business'
import type { ChatMessage, Turn, TurnEvent } from '@/types'

const props = defineProps<{
  /** 当前会话 ID：用来核对传进来的消息确实属于这个会话。 */
  sessionId: string
  /** 当前项目：附件缩略图按项目资产接口取，跨项目/跨账号会 404。 */
  projectId?: string
  messages: ChatMessage[]
  turn: Turn | null
  events: TurnEvent[]
  /** 会话级业务卡片（检测/巡检/核查进度）：与轮次是否结束无关。 */
  businessCards?: BusinessCard[]
  pollIssue?: string
  /** 还有更早的消息可以加载（由面板负责真正去取）。 */
  hasOlder?: boolean
  /** turnId -> 该轮事件：历史轮次的“执行详情”按需展开时读这里。 */
  eventsByTurn?: Record<string, TurnEvent[]>
}>()

const emit = defineEmits<{
  pick: [text: string]
  navigate: [route: unknown]
  older: []
  /** 展开某一轮的历史执行详情（面板负责拉取该 turn 的事件）。 */
  detail: [turnId: string]
  /** 终态已到但正文还没读到：请面板再读一次消息。 */
  reload: []
}>()

const viewport = ref<HTMLDivElement | null>(null)
/** 是否跟随最新。只有在**用户真实手势**或点“回到最新”时才改变。 */
const stuck = ref(true)
/** 上翻后又有新内容：按钮上显示“有新消息”，但不抢滚动。 */
const unread = ref(false)
const copiedId = ref<string | number>('')
const codeNotice = ref('')
/** 图片加载失败（损坏/已无权限/跨项目）：显示占位而不是空白。 */
const brokenAssets = ref<Set<string>>(new Set())
/** 终态已到、正文还在路上：超过一小段时间才提示，避免正常交接时闪一下。 */
const handoverSlow = ref(false)
/** 每个会话记住自己的滚动位置：切回来接着看，不与别的会话互相带动。 */
const scrollBySession = new Map<string, number>()
let noticeTimer: ReturnType<typeof setTimeout> | undefined
let handoverTimer: ReturnType<typeof setTimeout> | undefined
/** 最近一次真实手势的时间戳：只有它附近的 scroll 才代表用户意图。 */
let gestureAt = 0
let pointerDown = false
/** 内容变化后要还原到的位置（暂停跟随时使用）。 */
let pinnedTop = 0
let firstSeen: string | number | null = null

function flashCodeNotice(text: string): void {
  codeNotice.value = text
  clearTimeout(noticeTimer)
  noticeTimer = setTimeout(() => { codeNotice.value = '' }, 1600)
}

async function copyMessage(id: string | number, text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text)
    copiedId.value = id
    setTimeout(() => { if (copiedId.value === id) copiedId.value = '' }, 1600)
  } catch {
    flashCodeNotice('复制失败，请选中文字手动复制')
  }
}

/** 代码块：点一下整块复制（v-html 里没法挂组件，用事件委托）。 */
function onClickBody(event: MouseEvent): void {
  const target = event.target as HTMLElement | null
  const pre = target?.closest('pre')
  if (!pre) return
  const text = pre.innerText ?? ''
  if (!text) return
  navigator.clipboard
    .writeText(text)
    .then(() => flashCodeNotice('代码已复制'))
    .catch(() => flashCodeNotice('复制失败，请手动选择'))
}

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
/**
 * 实时区（工具进度 + 流式正文）是否显示。
 *
 * 进行中当然显示；**已完成但正文还没落库**时也继续显示——这一步是交接，
 * 不是结束。若此时先隐藏，正文会短暂消失、内容高度骤降，浏览器把视口顶到
 * “看起来像底部”的位置，用户上翻的阅读位置就丢了。等落库消息到达后
 * （``persisted`` 变真）自然切换，正文连续、不重复。
 */
const showLive = computed(() => {
  const turn = props.turn
  if (!turn || persisted.value) return false
  if (!isTerminalStatus(turn.status)) return true
  // 已完成：只有本来就有可见内容时才继续显示，避免刷新后凭空多出一行“已完成”。
  return turn.status === 'completed' && (Boolean(streamed.value) || cards.value.length > 0)
})
/** 终态但正文仍在读取：给出可恢复说明，而不是空白或假装还在生成。 */
const handover = computed(
  () => Boolean(props.turn) && terminal.value && !persisted.value && props.turn?.status === 'completed',
)
/** 终态、没有落库回答、也没有正文可延续（失败/停止）：把过程折叠保留下来。 */
const showRoundDetail = computed(
  () =>
    Boolean(props.turn) &&
    terminal.value &&
    !persisted.value &&
    props.turn?.status !== 'completed' &&
    (cards.value.length > 0 || Boolean(streamed.value)),
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

// --------------------------------------------------------------------- 附件

/** 消息里记录的图片资产 ID（后端 meta.asset_ids，按当前账号/项目鉴权）。 */
function assetIds(message: ChatMessage): string[] {
  const raw = message.meta?.asset_ids
  if (!Array.isArray(raw)) return []
  return raw.map((item) => String(item)).filter(Boolean)
}

type PersistedQuestion = { question: string; options: string[] }

/**
 * 历史回答里落库的提问。
 *
 * 轮次跑完、最终回答落库后，实时区会收起；如果提问只存在于 turn_events，
 * 用户就只看到“问题已发给你”却找不到问题。后端把最后一次提问写进消息 meta，
 * 这里从 meta 再渲染一次，刷新后也还在。
 */
function messageQuestion(message: ChatMessage): PersistedQuestion | null {
  if (message.role !== 'assistant') return null
  const raw = message.meta?.question
  if (!raw || typeof raw !== 'object') return null
  const data = raw as { question?: unknown; options?: unknown }
  const text = typeof data.question === 'string' ? data.question.trim() : ''
  if (!text) return null
  const options = Array.isArray(data.options)
    ? data.options.map((item) => String(item)).filter(Boolean).slice(0, 5)
    : []
  return { question: text, options }
}

/**
 * 缩略图地址：走已鉴权的资产内容接口，**不信任**消息里的任何路径。
 * 跨账号/跨项目会 404，损坏的文件由后端 SHA 校验拒绝——两种都在界面上给占位。
 */
function assetUrl(assetId: string): string {
  if (!props.projectId) return ''
  const project = encodeURIComponent(props.projectId)
  return `/api/projects/${project}/assets/${encodeURIComponent(assetId)}/content`
}

function assetBroken(assetId: string): boolean {
  return brokenAssets.value.has(assetId)
}

function markAssetBroken(assetId: string): void {
  const next = new Set(brokenAssets.value)
  next.add(assetId)
  brokenAssets.value = next
}

// --------------------------------------------------------------------- 历史执行详情

/** 这条回复对应的轮次（落库时写进 meta.turn_id）。 */
function turnIdOf(message: ChatMessage): string {
  return String(message.meta?.turn_id ?? '')
}

/** 该轮的工具调用次数（落库时记在 meta.tool_calls，不用为此多拉一次接口）。 */
function toolCountOf(message: ChatMessage): number {
  const raw = message.meta?.tool_calls
  return typeof raw === 'number' && raw > 0 ? raw : 0
}

function detailCards(message: ChatMessage) {
  const turnId = turnIdOf(message)
  if (!turnId) return []
  return toolCards(props.eventsByTurn?.[turnId] ?? [])
}

function detailLoaded(message: ChatMessage): boolean {
  const turnId = turnIdOf(message)
  return Boolean(turnId && props.eventsByTurn?.[turnId]?.length)
}

/** 展开时才去拉这一轮的真实事件：默认收起，不给每轮加一整块卡片。 */
function onToggleDetail(event: Event, message: ChatMessage): void {
  const target = event.target as HTMLDetailsElement | null
  if (!target?.open) return
  const turnId = turnIdOf(message)
  if (!turnId || detailLoaded(message)) return
  emit('detail', turnId)
}

// --------------------------------------------------------------------- 滚动

/** 用户真实手势开始：之后的 scroll 才算“用户想滚”，布局变化造成的不算。 */
function markGesture(): void {
  gestureAt = Date.now()
  const element = viewport.value
  if (element) pinnedTop = element.scrollTop
}

function recentGesture(): boolean {
  return pointerDown || Date.now() - gestureAt < 900
}

function onPointerDown(): void {
  pointerDown = true
  markGesture()
}

function onPointerUp(): void {
  pointerDown = false
  markGesture()
}

function onScroll(): void {
  const element = viewport.value
  if (!element) return
  const height = element.scrollHeight
  const atBottom = nearBottom({
    scrollHeight: height,
    scrollTop: element.scrollTop,
    clientHeight: element.clientHeight,
  })
  if (atBottom) {
    // 内容变短（实时区交接、历史重排）会把视口顶到“看起来贴底”的位置。
    // 那不是用户想回最新，保持原来的暂停状态与阅读位置。
    if (!recentGesture()) return
    stuck.value = true
    unread.value = false
    return
  }
  if (recentGesture()) {
    stuck.value = false
    pinnedTop = element.scrollTop
  }
}

function onWheel(event: WheelEvent): void {
  markGesture()
  if (event.deltaY < 0) stuck.value = false
}

function onKeydown(event: KeyboardEvent): void {
  if (!['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End'].includes(event.key)) return
  markGesture()
  if (['ArrowUp', 'PageUp', 'Home'].includes(event.key)) stuck.value = false
}

function toLatest(smooth = false): void {
  const element = viewport.value
  if (!element) return
  // 长对话里“回到最新”要**立刻**到位：动画跨几千像素时又慢又容易被当成没反应，
  // 只有短距离才用平滑滚动。
  const distance = element.scrollHeight - element.clientHeight - element.scrollTop
  const animate = smooth && distance > 0 && distance < element.clientHeight * 2
  element.scrollTo({ top: element.scrollHeight, behavior: animate ? 'smooth' : 'auto' })
  stuck.value = true
  unread.value = false
  pinnedTop = element.scrollHeight
}

watch(contentKey, async () => {
  const element = viewport.value
  const beforeHeight = element?.scrollHeight ?? 0
  const first = scoped.value[0]?.id ?? null
  const prepended = first !== null && firstSeen !== null && first !== firstSeen
  firstSeen = first
  await nextTick()
  // 用户上翻查看历史时不要抢滚动条；只有贴着底部才跟随新内容。
  if (stuck.value) {
    toLatest()
    return
  }
  const node = viewport.value
  if (!node) return
  // 暂停跟随：内容变化不能移动视口。在顶部插入更早的消息时，
  // 把插入的高度补回去，视觉上仍停在原来那句话上。
  const delta = node.scrollHeight - beforeHeight
  if (prepended && delta > 0) pinnedTop += delta
  const maxTop = Math.max(0, node.scrollHeight - node.clientHeight)
  node.scrollTop = Math.min(Math.max(0, pinnedTop), maxTop)
  pinnedTop = node.scrollTop
  if (props.turn || props.events.length) unread.value = true
})

watch(
  handover,
  (value) => {
    clearTimeout(handoverTimer)
    if (!value) {
      handoverSlow.value = false
      return
    }
    handoverTimer = setTimeout(() => { handoverSlow.value = true }, 1500)
  },
  { immediate: true },
)

/**
 * 切会话：先把上一个会话的滚动位置存下来，再恢复这个会话上次的位置。
 * 没存过就贴底——刚打开的会话应当看到最新一条。
 */
watch(
  () => props.sessionId,
  async (_next, previous) => {
    const element = viewport.value
    if (element && previous) scrollBySession.set(String(previous), element.scrollTop)
    stuck.value = true
    unread.value = false
    firstSeen = null
    await nextTick()
    const saved = scrollBySession.get(String(props.sessionId))
    if (!viewport.value) return
    if (saved !== undefined) {
      viewport.value.scrollTop = saved
      pinnedTop = saved
      stuck.value = nearBottom({
        scrollHeight: viewport.value.scrollHeight,
        scrollTop: viewport.value.scrollTop,
        clientHeight: viewport.value.clientHeight,
      })
    } else {
      toLatest()
    }
  },
)

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
    <div
      ref="viewport"
      class="timeline"
      aria-label="对话消息"
      tabindex="0"
      @scroll.passive="onScroll"
      @wheel.passive="onWheel"
      @keydown="onKeydown"
      @pointerdown="onPointerDown"
      @pointerup="onPointerUp"
      @pointercancel="onPointerUp"
    >
      <div class="chat-column">
      <button v-if="hasOlder" class="load-older" type="button" @click="emit('older')">
        加载更早的消息
      </button>
      <div v-if="!scoped.length && !turn" class="timeline-empty">
        <slot name="empty">
          <p>还没有消息。直接描述你要做的事，例如「看看这个目录里有哪些图片」。</p>
        </slot>
      </div>

      <article
        v-for="message in scoped"
        :key="message.id"
        class="message"
        :class="`message-${message.role}`"
      >
        <div v-if="message.role === 'assistant'" class="message-author">
          <span aria-hidden="true">✳</span>海巡助手
        </div>
        <div
          v-if="message.role === 'assistant'"
          class="message-text markdown-body"
          v-html="renderMarkdown(message.content)"
          @click="onClickBody"
        ></div>
        <p v-else class="message-text">{{ message.content }}</p>

        <!-- 落库后的提问：避免轮次结束后“问题已发给你”却看不到问题。 -->
        <div v-if="messageQuestion(message)" class="message-question" role="status">
          <p class="message-question-text">{{ messageQuestion(message)?.question }}</p>
          <button
            v-for="option in messageQuestion(message)?.options ?? []"
            :key="option"
            class="ghost small"
            type="button"
            @click="emit('pick', option)"
          >
            {{ option }}
          </button>
        </div>

        <!-- 用户发出的图片：从消息 meta 的资产引用读，刷新后仍然在。 -->
        <div v-if="assetIds(message).length" class="message-attachments">
          <template v-for="assetId in assetIds(message)" :key="assetId">
            <a
              v-if="!assetBroken(assetId)"
              class="message-thumb"
              :href="assetUrl(assetId)"
              target="_blank"
              rel="noopener"
              :title="'查看原图'"
            >
              <img :src="assetUrl(assetId)" alt="消息附件" loading="lazy" @error="markAssetBroken(assetId)" />
            </a>
            <span v-else class="message-thumb broken" title="图片不可用：文件损坏、已被删除或当前账号无权访问">
              图片不可用
            </span>
          </template>
        </div>

        <!-- 历史轮次的执行详情：默认收起；未拉事件前不判断成败，展开后才按 turn_id 读取。 -->
        <details
          v-if="message.role === 'assistant' && toolCountOf(message) > 0"
          class="turn-detail"
          @toggle="onToggleDetail($event, message)"
        >
          <summary class="turn-detail-head">
            <template v-if="detailLoaded(message)">{{ toolFoldSummary(detailCards(message)) }}</template>
            <template v-else>工具调用 {{ toolCountOf(message) }} 次 · 执行记录</template>
            <span class="turn-detail-hint">{{ detailLoaded(message) ? '查看执行详情' : '展开后读取' }}</span>
          </summary>
          <ul v-if="detailCards(message).length" class="tool-list">
            <li v-for="card in detailCards(message)" :key="card.callId" class="tool-card">
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
          <p v-else class="hint">
            正在读取这一轮的执行记录…如果一直停在这里，可能是记录已被清理或当前账号无权查看。
          </p>
        </details>

        <div v-if="message.content" class="message-actions">
          <button type="button" @click="copyMessage(message.id, message.content)">
            {{ copiedId === message.id ? '已复制' : '复制' }}
          </button>
        </div>
      </article>

      <!-- 进行中的这一轮：工具进度 + 流式正文。终态后交给落库消息，避免显示两遍。 -->
      <section v-if="showLive" class="live-turn" aria-live="polite">
        <div class="live-head">
          <span class="live-dot" :class="{ busy: !terminal }" aria-hidden="true"></span>
          <span class="live-status">{{ turn ? turnStatusLabel(turn.status) : '' }}</span>
          <span v-if="turn?.mode" class="live-meta">
            {{ turn.mode === 'full' ? '完全访问' : turn.mode === 'assist' ? '帮我批准' : '请求批准' }}
          </span>
          <span v-if="turn?.model" class="live-meta mono">{{ turn.model }}</span>
        </div>

        <!--
          工具过程整体折叠：默认只显示"几次调用 · 最近一次的状态和标题"。
          工具返回的摘要是排查用的，展开才看，不能当成回答上屏。
        -->
        <details v-if="cards.length" class="tool-fold">
          <summary class="tool-fold-head">{{ toolFoldSummary(cards) }}</summary>
          <ul class="tool-list">
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
        </details>

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

      <!--
        本轮业务：默认收成一行摘要（DSH 式折叠），点开才是紧凑列表。
        它**不是**最终回答——回答只来自落库消息，所以这里不会重复渲染一遍结论。
      -->
      <details v-if="businessCards && businessCards.length" class="business-fold" aria-label="业务进度">
        <summary class="business-fold-head">
          <span class="business-fold-title">本轮业务</span>
          <span class="business-fold-count">{{ businessCards.length }} 项</span>
          <span class="business-fold-chevron" aria-hidden="true"></span>
        </summary>
        <ul class="business-fold-list">
          <li v-for="card in businessCards" :key="card.jobId" class="business-row">
            <details class="business-row-details">
              <summary class="business-row-head">
                <span class="business-dot" :data-state="card.status" aria-hidden="true"></span>
                <span class="business-row-title">{{ card.title }}</span>
                <span class="business-row-summary">{{ card.summary }}</span>
                <span v-if="card.recordIds.length" class="business-row-count">{{ card.recordIds.length }} 条</span>
              </summary>
              <ul v-if="card.items.length" class="business-items">
                <li v-for="item in card.items" :key="item.recordId">
                  <span class="mono">…{{ item.recordId.slice(-6) }}</span>
                  <span>{{ recordStatusLabel(item.status) }}</span>
                  <span v-if="item.hitCount !== null">命中 {{ item.hitCount }}</span>
                  <span v-if="item.classes">{{ item.classes }}</span>
                  <span v-if="item.error" class="form-error">{{ item.error }}</span>
                </li>
              </ul>
              <ul v-if="card.detail.length" class="business-items">
                <li v-for="(line, index) in card.detail" :key="index" class="mono">{{ line }}</li>
              </ul>
              <p v-if="!card.items.length && !card.detail.length" class="hint">
                这个作业还没有逐条明细；进度更新时会自动补上。
              </p>
            </details>
          </li>
        </ul>
      </details>

      <!-- 终态已到、正文还在读取：给可恢复说明，不是空白也不是假装还在生成。 -->
      <p v-if="handoverSlow" class="hint handover-hint" role="status">
        回答已经生成，正在从服务端读取…
        <button class="link" type="button" @click="emit('reload')">重新读取</button>
      </p>

      <p v-if="failure" class="banner banner-warn" role="status">{{ failure }}</p>
      <p v-if="notice" class="hint">{{ notice }}</p>
      <p v-if="pollIssue" class="form-error" role="alert">{{ pollIssue }}</p>
      </div>
    </div>

    <p v-if="codeNotice" class="code-notice" role="status">{{ codeNotice }}</p>
    <button v-if="!stuck" class="to-latest" type="button" @click="toLatest(true)">
      {{ unread ? '有新消息' : '回到最新' }} ↓
    </button>
  </div>
</template>
