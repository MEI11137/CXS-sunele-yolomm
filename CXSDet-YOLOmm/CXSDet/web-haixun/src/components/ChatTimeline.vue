<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { ChatMessage } from '@/types'

const props = defineProps<{ messages: ChatMessage[]; conversationKey: string }>()
const viewport = ref<HTMLDivElement | null>(null)
const content = ref<HTMLDivElement | null>(null)
const following = ref(true)
const away = ref(false)
const unread = ref(false)
const copied = ref('')
const copyError = ref('')
const lastUserId = computed(() => [...props.messages].reverse().find(msg => msg.role === 'user')?.id)
let observer: ResizeObserver | undefined
let disposed = false
let copyTimer: ReturnType<typeof setTimeout> | undefined

function onScroll() {
  const el = viewport.value
  if (!el) return
  // 上翻第一小段也应解除跟随；宽松阈值会在短会话里重新吸回底部。
  const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight <= 2
  away.value = !nearBottom
  following.value = nearBottom
  if (nearBottom) unread.value = false
}
function pauseFollow() { following.value = false }
function onWheel(e: WheelEvent) { if (e.deltaY < 0) pauseFollow() }
function onKey(e: KeyboardEvent) {
  if (['ArrowUp', 'PageUp', 'Home'].includes(e.key)) pauseFollow()
}
async function followContent() {
  // 保留“用户正在读历史”的决定；异步消息/工具更新不能重新打开跟随。
  if (!following.value) {
    await nextTick()
    if (viewport.value) away.value = viewport.value.scrollHeight - viewport.value.scrollTop - viewport.value.clientHeight > 2
    unread.value = away.value
    return
  }
  await nextTick()
  if (disposed || !following.value || !viewport.value) return
  viewport.value.scrollTop = viewport.value.scrollHeight
  away.value = false
  unread.value = false
}
function jumpToLatest() { following.value = true; void followContent() }
function jumpToTop() { following.value = false; viewport.value?.scrollTo({ top: 0 }) }
watch(() => props.conversationKey, () => { following.value = true; unread.value = false; void followContent() })
watch(() => props.messages.map(msg => `${msg.id}:${msg.content}:${msg.streaming}`).join('\n'), followContent, { flush: 'post' })
onMounted(() => {
  // 捕捉工具卡片、折叠详情和窗口尺寸变化，而不是只盯消息条数。
  observer = new ResizeObserver(() => { void followContent() })
  if (content.value) observer.observe(content.value)
  if (viewport.value) observer.observe(viewport.value)
  jumpToLatest()
})
onBeforeUnmount(() => { disposed = true; observer?.disconnect(); clearTimeout(copyTimer) })
async function copy(msg: ChatMessage) {
  try {
    await navigator.clipboard.writeText(msg.content)
    copied.value = msg.id
    copyError.value = ''
    clearTimeout(copyTimer)
    copyTimer = setTimeout(() => { copied.value = '' }, 1800)
  } catch { copyError.value = '复制失败，请选中文字后手动复制。' }
}
defineExpose({ jumpToLatest, jumpToTop })
</script>

<template>
  <div class="chat-timeline">
    <div ref="viewport" class="chat-scroll" tabindex="0" aria-label="对话消息" @scroll.passive="onScroll" @wheel.passive="onWheel" @touchstart.passive="pauseFollow" @keydown="onKey">
      <div ref="content" class="chat-column">
        <div v-if="!messages.length" class="chat-empty">
          <div class="empty-symbol" aria-hidden="true">✳</div>
          <h2>今天想检查什么？</h2>
          <p>描述目标，或添加图片、数据集和项目资料。<br />海巡助手会先检查实际情况，再与你一起推进。</p>
        </div>
        <template v-for="msg in messages" :key="msg.id">
          <article class="chat-message" :class="`chat-${msg.role}`" :aria-label="msg.role === 'user' ? '我的消息' : '海巡助手回复'">
            <div v-if="msg.role === 'assistant'" class="message-author"><span class="assistant-symbol" aria-hidden="true">✳</span>海巡助手</div>
            <div class="message-body">
              <div v-if="msg.attachments?.length" class="message-attachments"><span v-for="file in msg.attachments" :key="file.id" class="message-file">▧ {{ file.name }}</span></div>
              <div class="message-text">{{ msg.content }}<span v-if="msg.streaming" class="stream-cursor" aria-hidden="true">▍</span></div>
            </div>
            <div v-if="msg.content && !msg.streaming" class="message-actions"><button :aria-label="msg.role === 'user' ? '复制我的消息' : '复制助手回复'" @click="copy(msg)"><svg viewBox="0 0 20 20" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.4"><rect x="7" y="7" width="10" height="10" rx="2"/><path d="M12 7V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v5a2 2 0 0 0 2 2h2"/></svg><span>{{ copied === msg.id ? '已复制' : '复制' }}</span></button></div>
          </article>
          <slot v-if="msg.id === lastUserId" name="progress"></slot>
        </template>
        <slot v-if="!lastUserId" name="progress"></slot>
        <p v-if="copyError" class="copy-error" role="status">{{ copyError }}</p>
      </div>
    </div>
    <button v-if="away" class="jump-latest" aria-label="回到最新消息" @click="jumpToLatest"><svg viewBox="0 0 20 20" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M10 3v13m-5-5 5 5 5-5" /></svg><span v-if="unread">有新消息</span></button>
  </div>
</template>

<style scoped>
.chat-timeline { flex: 1; min-height: 0; min-width: 0; position: relative; }
.chat-scroll { height: 100%; overflow-y: auto; overflow-x: hidden; overscroll-behavior: contain; overflow-anchor: none; scrollbar-gutter: stable; }
.chat-scroll:focus-visible { box-shadow: inset 0 0 0 1px var(--border-control); }
.chat-column { max-width: 850px; margin: 0 auto; padding: 28px 24px 44px; display: flex; flex-direction: column; gap: 16px; }
.chat-empty { padding: clamp(30px, 8vh, 90px) 0 36px; color: var(--text-secondary); text-align: center; }
.empty-symbol { color: var(--text); font-size: 38px; margin-bottom: 16px; }
.chat-empty h2 { font-size: 24px; color: var(--text); font-weight: 550; letter-spacing: -.5px; margin: 0 0 12px; }
.chat-empty p { font-size: 13px; line-height: 1.9; margin: 0; }
.chat-message { min-width: 0; width: 100%; display: flex; flex-direction: column; align-items: flex-start; }
.chat-user { align-items: flex-end; margin-top: 12px; }
.message-body { max-width: 100%; min-width: 0; }
.chat-user .message-body { max-width: 88%; padding: 12px 18px; border-radius: 18px; background: var(--panel-secondary); }
.message-text { white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.85; font-size: 14px; color: var(--text); }
.message-author { font-size: 12px; font-weight: 600; display: flex; align-items: center; gap: 8px; margin: 0 0 12px; color: var(--text-secondary); }
.assistant-symbol { font-size: 20px; color: var(--text); }
.chat-assistant { padding: 4px 0; }
.message-actions { margin-top: 8px; height: 25px; }
.message-actions button { display: flex; align-items: center; gap: 5px; border: 0; border-radius: 5px; background: transparent; color: var(--text-secondary); cursor: pointer; padding: 4px 6px; font-size: 11px; }
.message-actions button:hover { color: var(--text); background: var(--panel-secondary); }
.chat-user .message-actions { opacity: 0; }
.chat-user:hover .message-actions, .chat-user:focus-within .message-actions { opacity: 1; }
.message-attachments { display: flex; flex-wrap: wrap; gap: 7px; margin-bottom: 10px; }
.message-file { border: 1px solid var(--border); border-radius: 8px; font-size: 12px; padding: 7px 10px; }
.stream-cursor { color: var(--text-secondary); animation: blink 1s step-end infinite; }
.jump-latest { position: absolute; bottom: 14px; left: 50%; transform: translateX(-50%); display: flex; align-items: center; justify-content: center; gap: 6px; min-width: 36px; height: 36px; padding: 0 10px; border: 1px solid var(--border-control); border-radius: 22px; color: var(--text); background: var(--panel); box-shadow: var(--shadow-sm); cursor: pointer; font-size: 11px; z-index: 2; }
.jump-latest:hover { background: var(--panel-secondary); }
.copy-error { font-size: 12px; color: var(--warn-text); }
@keyframes blink { 50% { opacity: 0; } }
@media (prefers-reduced-motion: reduce) { .stream-cursor { animation: none; } }
@media (max-width: 768px) { .chat-column { padding: 20px 16px 40px; } .chat-user .message-actions { opacity: 1; } }
</style>
