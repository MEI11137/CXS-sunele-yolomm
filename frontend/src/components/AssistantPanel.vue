<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import ChatTimeline from './ChatTimeline.vue'
import { useConnectionStore } from '@/stores/connection'
import { useRuntimeStore } from '@/stores/runtime'
import { useWorkspaceStore } from '@/stores/workspace'

const route = useRoute()
const workspace = useWorkspaceStore()
const connection = useConnectionStore()
const runtime = useRuntimeStore()

const input = ref('')
const sending = ref(false)

const sessionId = computed(() => String(route.params.sessionId ?? ''))
const projectId = computed(() => String(route.params.projectId ?? ''))
const session = computed(() => {
  if (!sessionId.value) return null
  return (workspace.sessionsByProject[projectId.value] ?? []).find((item) => item.id === sessionId.value) ?? null
})
const runtimeReady = computed(() => runtime.featureEnabled('agent_runtime'))
const canSend = computed(
  () => Boolean(sessionId.value) && input.value.trim().length > 0 && !sending.value && !runtimeReady.value,
)

watch(
  sessionId,
  (value) => {
    if (value) void workspace.loadMessages(value)
  },
  { immediate: true },
)

onMounted(() => {
  void connection.load()
})

async function send(): Promise<void> {
  const text = input.value.trim()
  if (!text || !sessionId.value) return
  sending.value = true
  const ok = await workspace.sendMessage(sessionId.value, text)
  sending.value = false
  if (ok) input.value = ''
}
</script>

<template>
  <aside class="assistant" aria-label="海巡助手">
    <header class="assistant-header">
      <div class="assistant-title"><span aria-hidden="true">✳</span><strong>海巡助手</strong></div>
      <span class="badge">{{ runtimeReady ? '运行中' : '未接入' }}</span>
    </header>

    <div class="assistant-toolbar">
      <label class="field">
        <span>权限模式</span>
        <select disabled title="WB17-02 接入后可用">
          <option>请求确认</option>
        </select>
      </label>
      <label class="field">
        <span>模型</span>
        <select disabled>
          <option>
            {{
              connection.connection
                ? `${connection.connection.model}${connection.connection.capabilities_stale ? '（能力待检查）' : ''}`
                : '未配置个人连接'
            }}
          </option>
        </select>
      </label>
    </div>

    <div v-if="!session" class="assistant-placeholder">
      <p class="empty-hint">
        先在左侧选择或新建一个会话。会话属于具体项目，消息会保存到该项目的历史记录里。
      </p>
    </div>

    <template v-else>
      <ChatTimeline :messages="workspace.messages" />
      <p v-if="workspace.lastNotice" class="assistant-notice">{{ workspace.lastNotice }}</p>
    </template>

    <footer class="assistant-input">
      <textarea
        v-model="input"
        rows="2"
        :placeholder="session ? '描述你的目标，例如检查该目录下的光伏图片…' : '请先选择会话'"
        :disabled="!session || runtimeReady"
        aria-label="发送给海巡助手的消息"
        @keydown.enter.exact.prevent="send"
      ></textarea>
      <div class="assistant-actions">
        <span class="hint">
          {{
            runtimeReady
              ? '运行时已接入'
              : '运行时未接入：消息只保存，不会请求模型、不会调用工具，也不产生费用。'
          }}
        </span>
        <button class="primary" type="button" :disabled="!canSend" @click="send">
          {{ sending ? '保存中…' : '发送' }}
        </button>
      </div>
    </footer>
  </aside>
</template>
