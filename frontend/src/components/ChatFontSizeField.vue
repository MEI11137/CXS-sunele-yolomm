<script setup lang="ts">
import { computed } from 'vue'
import {
  CHAT_FONT_DEFAULT,
  CHAT_FONT_MAX,
  CHAT_FONT_MIN,
  useUiStore,
} from '@/stores/ui'

/**
 * 设置页「会话字号」控件：12–24px 原生下拉，默认 14。
 *
 * 只写本机 localStorage（`hx17-chat-font-size`），不走后端、不影响账号；
 * 字号通过 ui store 下发 CSS 变量 --chat-font-size，样式集中在 styles.css。
 * 这个组件同时被设置页和 QA fixture 复用，避免两边行为不一致。
 */
const ui = useUiStore()

const sizes = computed(() =>
  Array.from({ length: CHAT_FONT_MAX - CHAT_FONT_MIN + 1 }, (_, index) => CHAT_FONT_MIN + index),
)

const sizeModel = computed({
  get: () => String(ui.chatFontSize),
  set: (value: string) => ui.setChatFontSize(Number(value)),
})

const atDefault = computed(() => ui.chatFontSize === CHAT_FONT_DEFAULT)
</script>

<template>
  <div class="chat-font-block">
    <div class="chat-font-row">
      <label class="field">
        <span>会话字号</span>
        <select v-model="sizeModel" aria-label="会话字号">
          <option v-for="size in sizes" :key="size" :value="String(size)">{{ size }} px</option>
        </select>
      </label>
      <button
        class="ghost small"
        type="button"
        :disabled="atDefault"
        title="恢复为默认的 14 px"
        @click="ui.resetChatFontSize()"
      >
        恢复默认
      </button>
      <p class="chat-font-hint">仅影响对话正文和输入区；只保存在当前浏览器</p>
    </div>
    <p class="chat-font-preview">
      预览：助手回复和你输入的消息将使用这个字号。
      <span class="preview-muted">只保存在当前浏览器。</span>
    </p>
  </div>
</template>
