<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { errorMessage } from '@/api/http'
import { useAuthStore } from '@/stores/auth'
import { useConnectionStore } from '@/stores/connection'
import { useRuntimeStore } from '@/stores/runtime'
import { useUiStore } from '@/stores/ui'
import type { ProviderPreset } from '@/types'

const auth = useAuthStore()
const connection = useConnectionStore()
const runtime = useRuntimeStore()
const ui = useUiStore()

const form = reactive({
  provider: 'deepseek',
  base_url: '',
  model: '',
  display_name: '',
  api_key: '',
})
const formError = ref('')
const passwordForm = reactive({ current: '', next: '', confirm: '' })
const passwordError = ref('')
const passwordNotice = ref('')

const selectedProvider = computed<ProviderPreset | null>(
  () => connection.providers.find((item) => item.id === form.provider) ?? null,
)
const capabilityRows = computed(() => {
  const caps = connection.connection?.capabilities ?? {}
  return [
    { key: 'auth', label: '认证', value: Boolean(caps.auth) },
    { key: 'chat', label: 'Chat 对话', value: Boolean(caps.chat) },
    { key: 'vision', label: '视觉（传图）', value: Boolean(caps.vision) },
    { key: 'tools', label: '工具调用', value: Boolean(caps.tools) },
  ]
})
const AUTH_STATE_LABELS: Record<string, string> = {
  verified: '已用 200 回复正向确认',
  rejected: '上游 401/403 明确拒绝',
  payment_required: '上游 402：余额或计费问题，未验证',
  rate_limited: '上游 429：频率或配额受限，未验证',
  upstream_error: '上游 5xx：认证状态未验证',
  network_error: '网络不可达：未验证',
  unverified: '未取得正向证据：未验证',
  superseded: '检查期间配置被修改，结论已丢弃',
}
const authState = computed(() => String(connection.connection?.capabilities?.auth_state ?? ''))
const authStateLabel = computed(() => AUTH_STATE_LABELS[authState.value] ?? '')
const evidence = computed(() => connection.connection?.capabilities?.evidence ?? {})
const checked = computed(() => Boolean(connection.connection?.checked_at) && !connection.connection?.capabilities_stale)

function syncForm(): void {
  const current = connection.connection
  if (!current) {
    const preset = connection.providers[0]
    form.provider = preset?.id ?? 'custom'
    form.base_url = preset?.base_url ?? ''
    form.model = preset?.models[0] ?? ''
    form.display_name = ''
    form.api_key = ''
    return
  }
  form.provider = current.provider
  form.base_url = current.base_url
  form.model = current.model
  form.display_name = current.display_name
  form.api_key = ''
}

watch(
  () => connection.connection,
  () => syncForm(),
)

function applyPreset(): void {
  const preset = selectedProvider.value
  if (!preset) return
  if (!form.base_url || connection.providers.some((item) => item.base_url === form.base_url)) {
    form.base_url = preset.base_url
  }
  if (!form.model && preset.models.length) form.model = preset.models[0]
}

async function save(): Promise<void> {
  formError.value = ''
  if (!form.base_url.trim() || !form.model.trim()) {
    formError.value = '接口地址与模型 ID 都必须填写'
    return
  }
  if (!connection.connection && !form.api_key.trim()) {
    formError.value = '首次保存必须填写 API Key'
    return
  }
  const ok = await connection.save({
    provider: form.provider,
    base_url: form.base_url.trim(),
    model: form.model.trim(),
    display_name: form.display_name.trim(),
    api_key: form.api_key.trim() || undefined,
  })
  if (ok) form.api_key = ''
}

async function changePassword(): Promise<void> {
  passwordError.value = ''
  passwordNotice.value = ''
  if (passwordForm.next !== passwordForm.confirm) {
    passwordError.value = '两次输入的新密码不一致'
    return
  }
  try {
    await auth.changePassword(passwordForm.current, passwordForm.next)
    passwordForm.current = ''
    passwordForm.next = ''
    passwordForm.confirm = ''
    passwordNotice.value = '密码已更新，其它设备上的登录状态已失效'
  } catch (failure) {
    passwordError.value = errorMessage(failure)
  }
}

onMounted(async () => {
  await connection.loadProviders()
  await connection.load()
  syncForm()
})
</script>

<template>
  <section class="page">
    <header class="page-header">
      <div>
        <h1>设置</h1>
        <p class="subtitle">个人模型连接、账号安全与外观偏好</p>
      </div>
    </header>

    <section class="card">
      <h2>个人模型连接</h2>
      <p class="body-text">
        连接只属于当前账号（{{ auth.user?.username }}），Key 加密保存在服务器，接口只回显掩码。
        能力必须实测：<strong>厂商和模型名都不能证明它支持视觉或工具调用</strong>。
      </p>

      <div v-if="connection.connection" class="connection-summary">
        <dl class="kv compact">
          <div><dt>接口</dt><dd class="mono">{{ connection.connection.base_url }}</dd></div>
          <div><dt>模型 ID</dt><dd class="mono">{{ connection.connection.model }}</dd></div>
          <div>
            <dt>显示名</dt>
            <dd>{{ connection.connection.display_name || '（未填）' }}</dd>
          </div>
          <div><dt>Key</dt><dd class="mono">{{ connection.connection.key_hint }}</dd></div>
          <div>
            <dt>能力</dt>
            <dd>
              <span v-if="connection.connection.capabilities_stale" class="tag tag-off">未检查 / 已失效</span>
              <span v-else class="tag tag-on">已检查</span>
              <span v-if="connection.connection.checked_at" class="hint">
                {{ connection.connection.checked_at }}
              </span>
            </dd>
          </div>
        </dl>
        <p v-if="connection.connection.capabilities_stale" class="hint">
          更换 Key、接口地址或模型 ID 后，旧的能力结论立即作废；请重新检查。
        </p>
      </div>

      <div class="form-grid">
        <label class="field">
          <span>服务商预设</span>
          <select v-model="form.provider" @change="applyPreset">
            <option v-for="item in connection.providers" :key="item.id" :value="item.id">
              {{ item.label }}
            </option>
          </select>
        </label>
        <label class="field">
          <span>接口地址（base_url）</span>
          <input v-model="form.base_url" placeholder="https://api.deepseek.com/v1" spellcheck="false" />
        </label>
        <label class="field">
          <span>模型 ID（真实 ID，不是展示名）</span>
          <input v-model="form.model" placeholder="deepseek-chat" spellcheck="false" />
        </label>
        <label class="field">
          <span>显示名（可选，仅本机展示）</span>
          <input v-model="form.display_name" placeholder="例如 我的 DeepSeek" />
        </label>
        <label class="field">
          <span>API Key</span>
          <input
            v-model="form.api_key"
            type="password"
            autocomplete="off"
            :placeholder="connection.connection ? `已保存 ${connection.connection.key_hint}，留空表示不修改` : '首次保存必填'"
          />
        </label>
      </div>
      <p v-if="selectedProvider?.note" class="hint">{{ selectedProvider.note }}</p>
      <p v-if="formError" class="form-error">{{ formError }}</p>
      <p v-if="connection.error" class="form-error">{{ connection.error }}</p>
      <p v-if="connection.notice" class="banner banner-ok">{{ connection.notice }}</p>

      <div class="form-actions">
        <button class="primary" type="button" :disabled="connection.loading" @click="save">
          {{ connection.connection ? '保存修改' : '保存连接' }}
        </button>
        <button
          class="ghost"
          type="button"
          :disabled="!connection.connection || connection.checking"
          @click="connection.check()"
        >
          {{ connection.checking ? '检查中…' : '检查连接与能力' }}
        </button>
        <button
          v-if="connection.connection"
          class="ghost danger-text"
          type="button"
          :disabled="connection.loading"
          @click="connection.remove()"
        >
          删除连接
        </button>
      </div>
      <p class="hint">
        检查会依次请求 chat、视觉、工具三项，单项超时（连接 6 秒 / 读取 25 秒）后直接失败，
        <strong>不会自动重试</strong>，避免产生不可控费用。
      </p>

      <div v-if="connection.connection" class="cap-grid">
        <div v-for="row in capabilityRows" :key="row.key" class="cap" :class="{ ok: row.value, unknown: !checked }">
          <span class="cap-name">{{ row.label }}</span>
          <span class="cap-state">
            {{ !checked ? '未检查' : row.value ? '通过' : '未通过' }}
          </span>
        </div>
      </div>
      <p v-if="checked && authStateLabel" class="hint">
        认证判定：{{ authStateLabel }}。只有拿到 200 且返回真实回复才算“认证通过”；
        402/429/5xx 都不会被当作 Key 已被接受。
      </p>

      <details v-if="connection.connection && Object.keys(evidence).length" class="evidence">
        <summary>查看检查证据</summary>
        <table class="table">
          <thead>
            <tr>
              <th>步骤</th>
              <th>HTTP</th>
              <th>耗时(ms)</th>
              <th>结束原因</th>
              <th>结果说明</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(item, key) in evidence" :key="key">
              <td>{{ item.step ?? key }}</td>
              <td>{{ item.status }}</td>
              <td>{{ item.latency_ms }}</td>
              <td>{{ item.finish_reason ?? '—' }}</td>
              <td>{{ item.note ?? item.outcome ?? '—' }}</td>
            </tr>
          </tbody>
        </table>
        <p class="hint">证据只保留状态、耗时、环节与固定说明；上游返回的原文不会落库或回显。</p>
      </details>
    </section>

    <section class="card">
      <h2>账号安全</h2>
      <p class="body-text">修改后其它设备上的登录状态会立即失效，当前页面保持登录。</p>
      <div class="form-grid">
        <label class="field">
          <span>当前密码</span>
          <input v-model="passwordForm.current" type="password" autocomplete="current-password" />
        </label>
        <label class="field">
          <span>新密码</span>
          <input v-model="passwordForm.next" type="password" autocomplete="new-password" />
        </label>
        <label class="field">
          <span>确认新密码</span>
          <input v-model="passwordForm.confirm" type="password" autocomplete="new-password" />
        </label>
      </div>
      <p v-if="passwordError" class="form-error">{{ passwordError }}</p>
      <p v-if="passwordNotice" class="banner banner-ok">{{ passwordNotice }}</p>
      <div class="form-actions">
        <button class="primary" type="button" @click="changePassword">更新密码</button>
      </div>
    </section>

    <section class="card">
      <h2>外观</h2>
      <div class="theme-row">
        <button
          class="ghost"
          type="button"
          :class="{ selected: ui.theme === 'light' }"
          @click="ui.setTheme('light')"
        >
          浅色
        </button>
        <button
          class="ghost"
          type="button"
          :class="{ selected: ui.theme === 'dark' }"
          @click="ui.setTheme('dark')"
        >
          深色
        </button>
      </div>
      <p class="hint">偏好只保存在这台电脑的浏览器里，不影响账号与服务器数据。</p>
    </section>

    <section class="card">
      <h2>关于</h2>
      <dl class="kv">
        <div><dt>版本</dt><dd>{{ runtime.meta?.version ?? '—' }}</dd></div>
        <div><dt>阶段</dt><dd>{{ runtime.meta?.stage ?? '—' }}</dd></div>
        <div><dt>登录方式</dt><dd>HttpOnly Cookie（服务端可吊销）</dd></div>
      </dl>
      <ul class="feature-list">
        <li v-for="[key, enabled] in Object.entries(runtime.meta?.features ?? {})" :key="key">
          <span class="mono">{{ key }}</span>
          <span class="tag" :class="enabled ? 'tag-on' : 'tag-off'">{{ enabled ? '已实现' : '未实现' }}</span>
        </li>
      </ul>
      <p class="hint">
        数据目录、端口与密钥路径属于服务端内部配置，不会通过任何接口返回。
      </p>
    </section>
  </section>
</template>
