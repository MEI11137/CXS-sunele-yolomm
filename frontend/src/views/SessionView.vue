<script setup lang="ts">
import AssistantPanel from '@/components/AssistantPanel.vue'

/**
 * 会话页 = 项目主对话。
 *
 * 这里**不再**放“会话名 / 项目名”页头：项目名在顶部工具栏，当前会话在左侧列表，
 * 助手面板自己也只有消息区 + 输入框，避免同一件事在一屏里说三遍（v1.6 的做法）。
 */
</script>

<template>
  <section class="chat-page">
    <AssistantPanel variant="center" />
  </section>
</template>
