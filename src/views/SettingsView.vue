<script setup lang="ts">
import { ref } from 'vue'
import { useThemeStore } from '@/stores/theme'
import { useGuideStore } from '@/stores/guide'

const theme = useThemeStore()
const guide = useGuideStore()

// 外观
function setTheme(t: 'light' | 'dark') {
  theme.apply(t)
}

// 个人大模型连接（Key 不持久化，仅临时表单）
const showConnDialog = ref(false)
const connForm = ref({
  provider: 'deepseek',
  baseUrl: 'https://api.deepseek.com',
  model: 'deepseek-chat',
  apiKey: '',
})
const connState = ref<'idle' | 'testing' | 'saved' | 'error'>('idle')
const savedConn = ref<{ provider: string; model: string; maskedKey: string } | null>(null)
const capability = ref({ auth: false, chat: false, vision: false, tools: false })
const connNotice = ref('')

function openConn() {
  connForm.value.apiKey = ''
  connState.value = 'idle'
  connNotice.value = ''
  showConnDialog.value = true
}

async function testConnection() {
  if (!connForm.value.apiKey) {
    connNotice.value = '请先输入 API Key'
    return
  }
  connState.value = 'testing'
  capability.value = { auth: false, chat: false, vision: false, tools: false }
  // 模拟能力检查（真实接口由第二人提供）
  await new Promise((r) => setTimeout(r, 800))
  capability.value = { auth: true, chat: true, vision: false, tools: false }
  connState.value = 'saved'
  connNotice.value = '连接测试通过（模拟）。文本能力可用，视觉与工具调用需后端能力检查确认。'
}

function saveConn() {
  if (!connForm.value.apiKey) {
    connNotice.value = '请先输入 API Key'
    return
  }
  // Key 不持久化到 localStorage，仅内存脱敏展示
  savedConn.value = {
    provider: connForm.value.provider,
    model: connForm.value.model,
    maskedKey: maskKey(connForm.value.apiKey),
  }
  connState.value = 'saved'
  showConnDialog.value = false
  connNotice.value = '已保存（临时）。当前未接入后端密钥机制，关闭页面后需重新输入，Key 未持久化。'
}

function deleteConn() {
  savedConn.value = null
  connForm.value.apiKey = ''
  connNotice.value = '已删除当前个人连接。'
}

function maskKey(key: string) {
  if (key.length <= 8) return '••••••••'
  return key.slice(0, 4) + '••••••••' + key.slice(-4)
}
</script>

<template>
  <div class="page">
    <div class="page-header">
      <h1 class="page-title">设置</h1>
    </div>

    <!-- 外观 -->
    <section class="section">
      <h2 class="section-title">外观</h2>
      <div class="panel setting-card">
        <div class="setting-row">
          <div class="setting-info">
            <div class="setting-name">主题外观</div>
            <div class="setting-desc">浅色为默认，切换立即生效并保存偏好</div>
          </div>
          <div class="theme-switch" role="radiogroup" aria-label="主题外观">
            <button
              class="theme-option"
              :class="{ active: theme.theme === 'light' }"
              role="radio"
              :aria-checked="theme.theme === 'light'"
              @click="setTheme('light')"
            >
              <span class="theme-preview light" aria-hidden="true"></span>
              浅色
            </button>
            <button
              class="theme-option"
              :class="{ active: theme.theme === 'dark' }"
              role="radio"
              :aria-checked="theme.theme === 'dark'"
              @click="setTheme('dark')"
            >
              <span class="theme-preview dark" aria-hidden="true"></span>
              深色
            </button>
          </div>
        </div>
      </div>
    </section>

    <!-- 个人连接 -->
    <section class="section">
      <h2 class="section-title">个人连接</h2>
      <div class="panel setting-card">
        <div class="setting-row">
          <div class="setting-info">
            <div class="setting-name">助手大模型</div>
            <div class="setting-desc">配置巡检助手使用的大模型，与检测模型相互独立</div>
          </div>
          <div class="setting-action">
            <span v-if="savedConn" class="tag tag-success">{{ savedConn.provider }} · {{ savedConn.model }}</span>
            <button class="btn" @click="openConn">{{ savedConn ? '替换' : '配置' }}</button>
            <button v-if="savedConn" class="btn btn-danger btn-ghost" @click="deleteConn">删除</button>
          </div>
        </div>
        <div v-if="savedConn" class="conn-status">
          <div class="cap-row">
            <span class="cap-label">API Key</span>
            <span class="mono">{{ savedConn.maskedKey }}</span>
          </div>
          <div class="cap-row">
            <span class="cap-label">文本能力</span>
            <span class="tag tag-success">可用（模拟）</span>
          </div>
          <div class="cap-row">
            <span class="cap-label">视觉 / 工具调用</span>
            <span class="tag">待后端能力检查</span>
          </div>
        </div>
      </div>
    </section>

    <!-- 引导 -->
    <section class="section">
      <h2 class="section-title">使用引导</h2>
      <div class="panel setting-card">
        <div class="setting-row">
          <div class="setting-info">
            <div class="setting-name">首次使用引导</div>
            <div class="setting-desc">重新查看五步引导，结束后恢复原页面与草稿</div>
          </div>
          <button class="btn" @click="guide.replay">重新查看引导</button>
        </div>
      </div>
    </section>

    <div v-if="connNotice" class="notice" :class="connState === 'error' ? 'notice-error' : ''">
      {{ connNotice }}
    </div>

    <!-- 连接弹窗 -->
    <div v-if="showConnDialog" class="overlay" @click.self="showConnDialog = false">
      <div class="dialog" role="dialog" aria-label="配置个人大模型连接">
        <div class="dialog-header">
          <h3 class="dialog-title">配置个人大模型</h3>
          <button class="btn btn-ghost btn-sm" aria-label="关闭" @click="showConnDialog = false">✕</button>
        </div>
        <div class="dialog-body">
          <div class="field">
            <label class="field-label" for="provider">服务商</label>
            <select id="provider" v-model="connForm.provider" class="select">
              <option value="deepseek">DeepSeek（预设）</option>
              <option value="custom">自定义兼容接口</option>
            </select>
          </div>
          <div class="field">
            <label class="field-label" for="baseUrl">Base URL</label>
            <input id="baseUrl" v-model="connForm.baseUrl" class="input" type="url" placeholder="https://..." />
          </div>
          <div class="field">
            <label class="field-label" for="model">模型名称</label>
            <input id="model" v-model="connForm.model" class="input" placeholder="deepseek-chat" />
          </div>
          <div class="field">
            <label class="field-label" for="apiKey">API Key</label>
            <input id="apiKey" v-model="connForm.apiKey" class="input" type="password" autocomplete="off" placeholder="仅本次输入，不持久化存储" />
            <div class="field-hint">Key 不会进入聊天、本地持久存储、URL 或日志。当前未接入后端密钥机制，仅作临时表单输入，关闭后清除。</div>
          </div>
          <div v-if="connState === 'testing'" class="notice">
            <span class="spinner" style="display:inline-block;margin-right:8px"></span> 正在测试连接…
          </div>
          <div v-else-if="connState === 'saved'" class="notice notice-success">
            连接测试通过。文本能力可用，视觉与工具调用待后端确认。
          </div>
        </div>
        <div class="dialog-footer">
          <button class="btn" @click="showConnDialog = false">取消</button>
          <button class="btn" @click="testConnection">测试连接</button>
          <button class="btn btn-primary" :disabled="!connForm.apiKey" @click="saveConn">保存</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.page {
  padding: var(--content-pad);
  max-width: 800px;
  margin: 0 auto;
}
.page-header {
  margin-bottom: 20px;
}
.page-title {
  font-size: 24px;
  font-weight: 600;
  margin: 0;
}
.section {
  margin-bottom: 24px;
}
.section-title {
  font-size: 16px;
  font-weight: 600;
  margin: 0 0 12px;
}
.setting-card {
  padding: 4px 0;
}
.setting-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px;
}
.setting-info {
  flex: 1;
}
.setting-name {
  font-size: 14px;
  font-weight: 500;
  margin-bottom: 4px;
}
.setting-desc {
  font-size: 12px;
  color: var(--text-secondary);
}
.setting-action {
  display: flex;
  align-items: center;
  gap: 8px;
}
.theme-switch {
  display: flex;
  gap: 8px;
}
.theme-option {
  display: flex;
  align-items: center;
  gap: 8px;
  height: 36px;
  padding: 0 12px;
  border: 1px solid var(--border-control);
  background: var(--panel);
  border-radius: var(--radius-sm);
  font-size: 13px;
  cursor: pointer;
}
.theme-option.active {
  border-color: var(--accent);
  box-shadow: var(--focus-ring);
}
.theme-preview {
  width: 20px;
  height: 20px;
  border-radius: 4px;
  border: 1px solid var(--border-control);
}
.theme-preview.light {
  background: #f3f5f7;
}
.theme-preview.dark {
  background: #111111;
}
.conn-status {
  padding: 0 16px 16px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.cap-row {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 13px;
}
.cap-label {
  width: 120px;
  color: var(--text-secondary);
  font-size: 12px;
}
.field-hint {
  font-size: 11px;
  color: var(--text-secondary);
  margin-top: 6px;
  line-height: 1.5;
}
</style>
