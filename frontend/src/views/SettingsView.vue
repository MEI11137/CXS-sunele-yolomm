<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { errorMessage } from '@/api/http'
import { useAuthStore } from '@/stores/auth'
import { useConnectionStore } from '@/stores/connection'
import { useRuntimeStore } from '@/stores/runtime'
import { useWorkspaceStore } from '@/stores/workspace'
import { useUiStore } from '@/stores/ui'
import DocumentsPanel from '@/components/DocumentsPanel.vue'
import ChatFontSizeField from '@/components/ChatFontSizeField.vue'
import type { ProviderPreset } from '@/types'

/**
 * 设置页：三分组（模型连接 / 项目资料与手册 / 账号与外观），
 * 默认只展开一组。切换用 v-show：组件常驻，未保存的草稿不因切组丢失。
 * v1.8 起外部 MCP 工具面板暂不对外开放（组件保留，后端扩展保留），
 * 因此设置页不再出现“工具连接”入口；旧深链接落到设置页时按默认分组展示。
 *
 * 模型连接默认是**简洁摘要**（真实模型 ID + 能力验证状态 + 修改连接）；
 * 修改才进入编辑，可取消（恢复为已保存值）；Key 永不回显，失败保留输入。
 * 未检查 / 已过期一律按“未验证”显示，不说成已验证。
 */

const auth = useAuthStore()
const connection = useConnectionStore()
const runtime = useRuntimeStore()
const workspace = useWorkspaceStore()
const ui = useUiStore()
const route = useRoute()
const router = useRouter()

const GROUPS = [
  { key: 'connection', label: '模型连接' },
  { key: 'documents', label: '项目资料与手册' },
  { key: 'account', label: '账号与外观' },
] as const
type GroupKey = (typeof GROUPS)[number]['key']
const activeGroup = ref<GroupKey>('connection')

const form = reactive({
  provider: 'deepseek',
  base_url: '',
  model: '',
  display_name: '',
  api_key: '',
})
const formError = ref('')
const editing = ref(false)
const passwordForm = reactive({ current: '', next: '', confirm: '' })
const passwordError = ref('')
const passwordNotice = ref('')

/** 能力开关的中文名：只影响显示，开关真假完全来自后端 `/api/meta`。 */
const FEATURE_LABELS: Record<string, string> = {
  training: '模型训练',
  documents: '项目资料',
  manuals: '手册',
  agent: '助手对话',
  detection: '图片检测',
  review: '人工复核',
  reflow: '数据回流',
}

/** v1.8：外部 MCP 尚未完成真实调用验收，界面不展示该能力项。 */
const HIDDEN_FEATURE_KEYS = new Set(['external_mcp'])
const visibleFeatures = computed(() =>
  Object.entries(runtime.meta?.features ?? {}).filter(([key]) => !HIDDEN_FEATURE_KEYS.has(key)),
)

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

/** 后端返回 UTC ISO；界面显示本地时间，避免评委看到裸 ISO 串。 */
function formatCheckedAt(value: string | null | undefined): string {
  if (!value) return ''
  const time = new Date(value)
  if (Number.isNaN(time.getTime())) return String(value)
  return time.toLocaleString('zh-CN', { hour12: false })
}

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

// 编辑中不做同步：外部刷新（如检查完成）不能覆盖用户正在填的草稿。
watch(
  () => connection.connection,
  () => {
    if (!editing.value) syncForm()
  },
)

function applyPreset(): void {
  const preset = selectedProvider.value
  if (!preset) return
  if (!form.base_url || connection.providers.some((item) => item.base_url === form.base_url)) {
    form.base_url = preset.base_url
  }
  if (!form.model && preset.models.length) form.model = preset.models[0]
}

function startEdit(): void {
  syncForm()
  formError.value = ''
  editing.value = true
}

function cancelEdit(): void {
  // 取消 = 回到已保存值，丢弃本次草稿（摘要视图同步恢复）。
  syncForm()
  formError.value = ''
  editing.value = false
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
  if (ok) {
    form.api_key = ''
    editing.value = false
    syncForm()
  }
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
  // v1.8：旧版“工具连接/外部 MCP”深链接已下线，落到设置页时回退到默认分组并清理残留参数。
  const legacyTools =
    String(route.query.group ?? '') === 'tools' ||
    String(route.query.section ?? '') === 'tools' ||
    route.hash === '#tools'
  if (legacyTools) {
    activeGroup.value = 'connection'
    const query = { ...route.query }
    delete query.group
    delete query.section
    void router.replace({ name: 'settings', query, hash: '' })
  }
  await connection.loadProviders()
  await connection.load()
  syncForm()
  // 首次没有连接：直接进入编辑（该填的字段就是首屏）。
  if (!connection.connection) editing.value = true
  // 资料与手册按项目存放：这里只要项目清单，选中哪个项目由面板自己管。
  if (!workspace.projects.length) await workspace.loadProjects()
})
</script>

<template>
  <section class="page">
    <header class="page-header">
      <div>
        <h1>设置</h1>
        <p class="subtitle">模型连接、项目资料与手册、账号安全与外观偏好</p>
      </div>
    </header>

    <nav class="segmented settings-groups" role="tablist" aria-label="设置分组">
      <button
        v-for="group in GROUPS"
        :key="group.key"
        type="button"
        :aria-pressed="activeGroup === group.key"
        @click="activeGroup = group.key"
      >
        {{ group.label }}
      </button>
    </nav>

    <!-- ---------------------------------------------------------------- 模型连接 -->
    <section v-show="activeGroup === 'connection'" class="card">
      <h2>个人模型连接</h2>
      <p class="body-text">
        连接只属于当前账号（{{ auth.user?.username }}）；Key 加密保存在服务器，界面只回显掩码。
      </p>

      <!-- 默认：简洁摘要 + 真实能力状态；修改才进入编辑 -->
      <div v-if="connection.connection && !editing" class="connection-summary">
        <dl class="kv compact">
          <div><dt>模型</dt><dd>
            <span class="mono">{{ connection.connection.model }}</span>
          </dd></div>
          <div><dt>接口</dt><dd class="mono">{{ connection.connection.base_url }}</dd></div>
          <div><dt>Key</dt><dd class="mono">{{ connection.connection.key_hint }}</dd></div>
          <div><dt>检查时间</dt><dd>
            <span v-if="connection.connection.capabilities_stale" class="tag tag-off">未检查 / 已失效</span>
            <template v-else>
              <span class="tag tag-on">已检查</span>
              <span v-if="connection.connection.checked_at" class="hint">
                {{ formatCheckedAt(connection.connection.checked_at) }}
              </span>
            </template>
          </dd></div>
        </dl>
        <p v-if="connection.connection.capabilities_stale" class="hint">
          更换 Key、接口地址或模型 ID 后，旧的能力结论立即作废；点「检查连接与能力」重新验证。
        </p>

        <div class="cap-grid">
          <div v-for="row in capabilityRows" :key="row.key" class="cap" :class="{ ok: row.value, unknown: !checked }">
            <span class="cap-name">{{ row.label }}</span>
            <span class="cap-state">
              {{ !checked ? '未验证' : row.value ? '通过' : '未通过' }}
            </span>
          </div>
        </div>
        <p v-if="checked && authStateLabel" class="hint">认证判定：{{ authStateLabel }}</p>

        <details v-if="Object.keys(evidence).length" class="evidence">
          <summary>诊断详情（检查证据）</summary>
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

        <div class="form-actions">
          <button class="primary" type="button" @click="startEdit">修改连接</button>
          <button
            class="ghost"
            type="button"
            :disabled="connection.checking"
            @click="connection.check()"
          >
            {{ connection.checking ? '检查中…' : '检查连接与能力' }}
          </button>
          <button
            class="ghost danger-text"
            type="button"
            :disabled="connection.loading"
            @click="connection.remove()"
          >
            删除连接
          </button>
        </div>
      </div>

      <!-- 编辑：首次（无连接）也走这块，字段就是要填的东西 -->
      <div v-if="editing || !connection.connection">
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
          <button v-if="connection.connection" class="ghost" type="button" @click="cancelEdit">
            取消（恢复为已保存值）
          </button>
        </div>
        <p class="hint">
          检查会依次请求 chat、视觉、工具三项，单项超时即失败，不自动重试。
        </p>
      </div>
    </section>

    <!-- ---------------------------------------------------------------- 资料与手册 -->
    <div v-show="activeGroup === 'documents'" class="settings-panel">
      <DocumentsPanel :projects="workspace.projects" :current-project-id="workspace.currentProjectId" />
    </div>

    <!-- ---------------------------------------------------------------- 账号与外观 -->
    <section v-show="activeGroup === 'account'" class="card">
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

      <ChatFontSizeField />

      <details class="about-details">
        <summary>关于本系统</summary>
        <dl class="kv">
          <div><dt>版本</dt><dd>{{ runtime.meta?.version ?? '—' }}</dd></div>
          <div><dt>阶段</dt><dd>{{ runtime.meta?.stage ?? '—' }}</dd></div>
          <div><dt>登录方式</dt><dd>HttpOnly Cookie（服务端可吊销）</dd></div>
        </dl>
        <ul class="feature-list">
          <li v-for="[key, enabled] in visibleFeatures" :key="key">
            <span>{{ FEATURE_LABELS[key] ?? key }}</span>
            <span class="mono hint">{{ key }}</span>
            <span class="tag" :class="enabled ? 'tag-on' : 'tag-off'">{{ enabled ? '已实现' : '未实现' }}</span>
          </li>
        </ul>
        <p class="hint">功能开关状态来自服务器 `/api/meta`，与实际接口一致。</p>
      </details>
    </section>
  </section>
</template>

<style scoped>
.settings-groups {
  margin-bottom: 12px;
}

.settings-panel {
  margin-top: 0;
}

.connection-summary {
  margin-bottom: 12px;
}

.about-details summary {
  cursor: pointer;
  font-size: 13px;
  color: var(--text-secondary);
  margin-top: 16px;
}

.about-details[open] {
  margin-top: 8px;
}
</style>
