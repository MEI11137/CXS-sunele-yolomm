<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import type { ChatMessage } from '@/types'

const props = defineProps<{ messages: ChatMessage[] }>()
const viewport = ref<HTMLDivElement | null>(null)

watch(
  () => props.messages.map((message) => `${message.id}:${message.content}`).join('\n'),
  async () => {
    await nextTick()
    const element = viewport.value
    if (element) element.scrollTop = element.scrollHeight
  },
)
</script>

<template>
  <div ref="viewport" class="timeline" aria-label="对话消息">
    <p v-if="!messages.length" class="timeline-empty">
      这里是会话的真实消息记录。助手运行时尚未接入，发送的消息会保存，但不会产生回复。
    </p>
    <article
      v-for="message in messages"
      :key="message.id"
      class="message"
      :class="`message-${message.role}`"
    >
      <span class="message-author">{{ message.role === 'user' ? '我' : '海巡助手' }}</span>
      <p class="message-text">{{ message.content }}</p>
    </article>
  </div>
</template>
