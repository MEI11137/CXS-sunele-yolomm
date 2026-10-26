<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useThemeStore } from '@/stores/theme'
import { useGuideStore } from '@/stores/guide'
import { getAdapter } from '@/services'
import type { CapabilityEntry, PersonalConnection } from '@/types'

const theme = useThemeStore()
const guide = useGuideStore()

// 外观
function setTheme(t: 'light' | 'dark') {
  theme.apply(t)
}

// ---------------------------------------------------------------- 个人连接
// Key 只提交给后端加密保存，界面永远只显示后端给的脱敏提示。
const showConnDialog = ref(false)
const connForm = ref({
  provider: 'deepseek',
  baseUrl: 'https://api.deepseek.com',
  model: 'deepseek-chat',
  apiKey: '',
})
const connState = ref<'idle' | 'testing' | 'saving' | 'error'>('idle')
const connection = ref<PersonalConnection | null>(null)
const connNotice = ref('')
const loading = ref(false)

const configured = computed(() => !!connection.value?.configured)

const capabilityRows = computed(() => {
  const caps = connection.value?.capabilities ?? {}
  const labels: Record<string, string> = {
    chat: '文本对话',
    vision: '图片理解',
    tools: '工具调用',
  }
  return Object.keys(labels).map((key) => {
    const entry: CapabilityEntry | undefined = caps[key]
    return {
      key,
      label: labels[key],
      status: entry?.status ?? 'unchecked',
      message: entry?.message ?? '',
    }
  })
})

function statusText(status: string): string {
  if (status === 'ok') return '可用'
  if (status === 'failed') return '不可用'
  if (status === 'unsupported') return '不支持'
  return '未检测'
}

function statusClass(status: string): string {
  if (status === 'ok') return 'tag-success'
  if (status === 'failed') return 'tag-error'
  return ''
}

/** 首次进入读取已保存的本人连接（服务端加密保存，不回读明文） */
async function loadConnection() {
  loading.value = true
  try {
    connection.value = await getAdapter().getConnection()
    if (connection.value?.configured) {
      connForm.value.provider = connection.value.provider || 'deepseek'
      connForm.value.baseUrl = connection.value.api_url || 'https://api.deepseek.com'
      connForm.value.model = connection.value.model || 'deepseek-chat'
    }
  } catch (e) {
    connNotice.value = '读取个人连接失败：' + (e as Error).message
    connState.value = 'error'
  } finally {
    loading.value = false
  }
}

onMounted(loadConnection)

function openConn() {
  // 打开弹窗时不回填 Key：后端只给脱敏提示，页面上也清空输入
  connForm.value.apiKey = ''
  connState.value = 'idle'
  connNotice.value = ''
  showConnDialog.value = true
}

/** 保存：Key 留空表示沿用已保存的 Key（首次配置必须填） */
async function saveConn(closeAfter = true): Promise<boolean> {
  if (!configured.value && !connForm.value.apiKey.trim()) {
    connNotice.value = '首次配置必须填写 API Key'
    connState.value = 'error'
    return false
  }
  connState.value = 'saving'
  connNotice.value = ''
  try {
    connection.value = await getAdapter().saveConnection({
      provider: connForm.value.provider,
      apiUrl: connForm.value.baseUrl,
      model: connForm.value.model,
      apiKey: connForm.value.apiKey.trim() || undefined,
    })
    // 保存成功后立刻清空表单里的 Key，避免停留在界面上
    connForm.value.apiKey = ''
    connState.value = 'idle'
    if (closeAfter) {
      showConnDialog.value = false
      connNotice.value = '个人连接已保存到本机，Key 由服务端加密存储。'
    }
    return true
  } catch (e) {
    connState.value = 'error'
    connNotice.value = '保存失败：' + (e as Error).message
    return false
  }
}

/** 测试：后端要求先有已保存连接，因此先保存再执行真实能力检测 */
async function testConnection() {
  if (connState.value === 'testing' || connState.value === 'saving') return
  connState.value = 'testing'
  connNotice.value = ''
  try {
    if (connForm.value.apiKey.trim() || !configured.value) {
      const saved = await saveConn(false)
      if (!saved) {
        connState.value = 'error'
        return
      }
    }
    connection.value = await getAdapter().checkConnection()
    connState.value = 'idle'
    const missing = connection.value?.missing_capabilities ?? []
    connNotice.value = missing.length
      ? '检测完成，以下能力仍不可用：' + missing.join('、')
      : '检测完成，三项能力均可用。'
  } catch (e) {
    connState.value = 'error'
    connNotice.value = '检测失败：' + (e as Error).message
  }
}

async function deleteConn() {
  connNotice.value = ''
  try {
    await getAdapter().deleteConnection()
    connection.value = { configured: false }
    connForm.value.apiKey = ''
    connNotice.value = '已删除当前个人连接。'
  } catch (e) {
    connState.value = 'error'
    connNotice.value = '删除失败：' + (e as Error).message
  }
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
            <div class="setting-desc">配置巡检助手使用的大模型，与检测模型相互独立；Key 由服务端加密保存</div>
          </div>
          <div class="setting-action">
            <span v-if="configured" class="tag tag-success">
              {{ connection?.provider }} · {{ connection?.model }}
            </span>
            <span v-else-if="loading" class="tag">读取中…</span>
            <button class="btn" @click="openConn">{{ configured ? '替换' : '配置' }}</button>
            <button v-if="configured" class="btn btn-danger btn-ghost" @click="deleteConn">删除</button>
          </div>
        </div>
        <div v-if="configured" class="conn-status">
          <div class="cap-row">
            <span class="cap-label">API Key</span>
            <span class="mono">{{ connection?.key_hint || '已加密保存' }}</span>
          </div>
          <div v-for="row in capabilityRows" :key="row.key" class="cap-row">
            <span class="cap-label">{{ row.label }}</span>
            <span class="tag" :class="statusClass(row.status)">
              {{ statusText(row.status) }}<template v-if="row.message"> · {{ row.message }}</template>
            </span>
          </div>
          <div v-if="connection?.failure_reason" class="cap-row">
            <span class="cap-label">失败原因</span>
            <span class="mono">{{ connection.failure_reason }}</span>
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
            <input id="apiKey" v-model="connForm.apiKey" class="input" type="password" autocomplete="off" :placeholder="configured ? '留空则沿用已保存的 Key' : '首次配置必须填写'" />
            <div class="field-hint">
              Key 由后端加密保存，接口不会回读明文，也不会进入聊天记录、URL 或日志。
              <template v-if="configured">修改其它字段时可留空 Key。</template>
            </div>
          </div>
          <div v-if="connState === 'testing' || connState === 'saving'" class="notice">
            <span class="spinner" style="display:inline-block;margin-right:8px"></span>
            {{ connState === 'saving' ? '正在保存…' : '正在执行真实能力检测…' }}
          </div>
          <div
            v-else-if="connNotice"
            class="notice"
            :class="connState === 'error' ? 'notice-error' : 'notice-success'"
          >
            {{ connNotice }}
          </div>
        </div>
        <div class="dialog-footer">
          <button class="btn" @click="showConnDialog = false">取消</button>
          <button class="btn" :disabled="connState === 'testing' || connState === 'saving'" @click="testConnection">
            测试连接
          </button>
          <button class="btn btn-primary" :disabled="connState === 'testing' || connState === 'saving'" @click="saveConn(true)">
            保存
          </button>
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
