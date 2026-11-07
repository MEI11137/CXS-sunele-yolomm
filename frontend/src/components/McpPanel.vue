<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import * as api from '@/api/business'
import { errorMessage } from '@/api/http'
import type { McpCallAudit, McpServer } from '@/types'

/**
 * 外部 MCP 配置面板。
 *
 * 这里刻意只做"用户本人配置"：模型侧没有新增服务、改地址、写凭据的任何工具。
 * 发现到的工具**默认待授权**，页面上逐项允许/拒绝/撤销——远端声明的 readOnlyHint
 * 只是展示标记，不构成任何授权（"只读自动放行"已删除）。
 * 本机 stdio 服务只有管理员能配置（会在评委机器上运行宿主程序）。
 */

const props = defineProps<{
  projects: { id: string; name: string }[]
  /** 最近进入的项目：从侧栏进设置时路由没有 projectId，用它避免把配置写到首个项目。 */
  currentProjectId?: string
}>()

const projectId = ref('')
/** 用户手动切换过项目后，不再被外部 props 改写。 */
const pickedByUser = ref(false)
const servers = ref<McpServer[]>([])
const error = ref('')
const notice = ref('')
const working = ref('')
const audit = ref<Record<string, McpCallAudit[]>>({})
const callArgs = ref<Record<string, string>>({})
const callResult = ref<Record<string, { ok: boolean; text: string }>>({})

const form = ref({
  name: '',
  transport: 'stdio' as 'stdio' | 'http',
  command: '',
  args: '',
  cwd_rel: '',
  url: '',
  secrets: '',
  enabled: true,
})

function parseSecrets(text: string, transport: 'stdio' | 'http'): Record<string, string> {
  const out: Record<string, string> = {}
  for (const raw of text.split('\n')) {
    const line = raw.trim()
    if (!line || line.startsWith('#')) continue
    const index = transport === 'stdio' ? line.indexOf('=') : line.search(/[:=]/)
    if (index <= 0) continue
    const key = line.slice(0, index).trim()
    const value = line.slice(index + 1).trim()
    if (key && value) out[key] = value
  }
  return out
}

async function load(): Promise<void> {
  if (!projectId.value) {
    servers.value = []
    return
  }
  error.value = ''
  try {
    const data = await api.listMcpServers(projectId.value)
    servers.value = data.servers
  } catch (failure) {
    error.value = errorMessage(failure)
  }
}

watch(projectId, () => void load())

function preferredProject(): string {
  const wanted = String(props.currentProjectId ?? '')
  if (wanted && props.projects.some((item) => item.id === wanted)) return wanted
  return props.projects[0]?.id ?? ''
}

onMounted(async () => {
  projectId.value = preferredProject()
  if (projectId.value) await load()
})

// 清单/当前项目晚到时补一次初始选择；用户已手动选择则不打扰。
watch(
  () => [props.currentProjectId, props.projects.length] as const,
  () => {
    if (pickedByUser.value || projectId.value) return
    projectId.value = preferredProject()
  },
)

async function create(): Promise<void> {
  error.value = ''
  notice.value = ''
  if (!form.value.name.trim()) {
    error.value = '服务名称不能为空'
    return
  }
  if (form.value.transport === 'stdio' && !form.value.command.trim()) {
    error.value = 'stdio 服务必须填可执行文件（绝对路径或 PATH 里的名字）'
    return
  }
  if (form.value.transport === 'http' && !form.value.url.trim()) {
    error.value = 'http 服务必须填地址'
    return
  }
  working.value = 'create'
  try {
    const payload: api.McpServerInput = {
      name: form.value.name.trim(),
      transport: form.value.transport,
      enabled: form.value.enabled,
    }
    if (form.value.transport === 'stdio') {
      payload.command = form.value.command.trim()
      payload.args = form.value.args
        .split('\n')
        .map((line) => line.trim())
        .filter(Boolean)
      if (form.value.cwd_rel.trim()) payload.cwd_rel = form.value.cwd_rel.trim()
      const env = parseSecrets(form.value.secrets, 'stdio')
      if (Object.keys(env).length) payload.env = env
    } else {
      payload.url = form.value.url.trim()
      const headers = parseSecrets(form.value.secrets, 'http')
      if (Object.keys(headers).length) payload.headers = headers
    }
    const created = await api.createMcpServer(projectId.value, payload)
    notice.value = `已保存「${created.server.name}」：先发现工具，再逐项授权。`
    form.value = {
      name: '',
      transport: form.value.transport,
      command: '',
      args: '',
      cwd_rel: '',
      url: '',
      secrets: '',
      enabled: true,
    }
    await load()
  } catch (failure) {
    error.value = errorMessage(failure)
  } finally {
    working.value = ''
  }
}

async function discover(server: McpServer): Promise<void> {
  error.value = ''
  notice.value = ''
  working.value = server.id
  try {
    const result = await api.discoverMcpServer(projectId.value, server.id)
    if (!result.ok) {
      error.value = `${result.error?.code ?? 'MCP_DISCOVERY_FAILED'}：${result.error?.message ?? ''}`
    } else {
      const added = result.new_tools?.length ?? 0
      notice.value = `发现 ${result.server.tools.length} 个工具${added ? `（新增 ${added} 个，默认待授权）` : ''}`
    }
    await load()
  } catch (failure) {
    error.value = errorMessage(failure)
  } finally {
    working.value = ''
  }
}

async function decide(server: McpServer, tool: string, decision: 'allow' | 'deny' | 'revoke'): Promise<void> {
  error.value = ''
  try {
    await api.authorizeMcpTool(projectId.value, server.id, tool, decision)
    await load()
  } catch (failure) {
    error.value = errorMessage(failure)
  }
}

async function toggle(server: McpServer, patch: Record<string, boolean>): Promise<void> {
  error.value = ''
  try {
    await api.updateMcpServer(projectId.value, server.id, patch)
    await load()
  } catch (failure) {
    error.value = errorMessage(failure)
  }
}

async function remove(server: McpServer): Promise<void> {
  error.value = ''
  try {
    await api.deleteMcpServer(projectId.value, server.id)
    notice.value = `已删除「${server.name}」及其工具授权`
    await load()
  } catch (failure) {
    error.value = errorMessage(failure)
  }
}

async function call(server: McpServer, tool: string): Promise<void> {
  error.value = ''
  const key = `${server.id}:${tool}`
  let args: Record<string, unknown> = {}
  const raw = (callArgs.value[key] ?? '').trim()
  if (raw) {
    try {
      args = JSON.parse(raw)
    } catch {
      error.value = '参数必须是 JSON 对象'
      return
    }
  }
  working.value = key
  try {
    const result = await api.callMcpTool(projectId.value, server.id, tool, args)
    callResult.value[key] = { ok: result.ok, text: result.text || result.error?.message || '' }
    const calls = await api.listMcpCalls(projectId.value, server.id)
    audit.value[server.id] = calls.calls
  } catch (failure) {
    callResult.value[key] = { ok: false, text: errorMessage(failure) }
  } finally {
    working.value = ''
  }
}

async function loadAudit(server: McpServer): Promise<void> {
  try {
    const calls = await api.listMcpCalls(projectId.value, server.id)
    audit.value[server.id] = calls.calls
  } catch (failure) {
    error.value = errorMessage(failure)
  }
}
</script>

<template>
  <section class="card">
    <h2>外部 MCP</h2>
    <p class="body-text">
      手动接入**本机 stdio 服务**或**网络服务**：地址、命令与凭据都由你填写，凭据加密保存、接口只回掩码。
      发现到的工具<strong>默认待授权</strong>，逐项允许后才可调用；把权限调到 full 也<strong>不会</strong>自动放行外部工具。
      本机 stdio 服务只支持<strong>管理员</strong>配置（会在评委机器上运行宿主程序）；远端声明的"只读"只是提示，不是授权。
    </p>

    <div class="form-grid">
      <label class="field">
        <span>项目</span>
        <select v-model="projectId" @change="pickedByUser = true">
          <option v-for="item in projects" :key="item.id" :value="item.id">{{ item.name }}</option>
        </select>
      </label>
      <label class="field">
        <span>服务名称</span>
        <input v-model="form.name" placeholder="例如 本机巡检助手" />
      </label>
      <label class="field">
        <span>传输方式</span>
        <select v-model="form.transport">
          <option value="stdio">stdio（本机进程）</option>
          <option value="http">http（网络地址）</option>
        </select>
      </label>
    </div>

    <template v-if="form.transport === 'stdio'">
      <div class="form-grid">
        <label class="field">
          <span>可执行文件（绝对路径或 PATH 里的名字）</span>
          <input v-model="form.command" placeholder="C:\Python\python.exe" spellcheck="false" />
        </label>
        <label class="field">
          <span>工作目录（可选，项目内相对路径）</span>
          <input v-model="form.cwd_rel" placeholder="例如 uploads" spellcheck="false" />
        </label>
      </div>
      <label class="field">
        <span>参数（每行一个，按列表传给进程，不经过 shell）</span>
        <textarea v-model="form.args" rows="3" placeholder="-m&#10;my_mcp_server" />
      </label>
      <label class="field">
        <span>环境变量（每行 KEY=VALUE，会加密保存）</span>
        <textarea v-model="form.secrets" rows="2" placeholder="API_TOKEN=..." />
      </label>
    </template>
    <template v-else>
      <label class="field">
        <span>地址（https；明文 http 只允许本机）</span>
        <input v-model="form.url" placeholder="https://example.com/mcp" spellcheck="false" />
      </label>
      <label class="field">
        <span>请求头（每行 Name: value，会加密保存）</span>
        <textarea v-model="form.secrets" rows="2" placeholder="Authorization: Bearer ..." />
      </label>
    </template>

    <div class="form-actions">
      <label class="checkbox">
        <input v-model="form.enabled" type="checkbox" />
        <span>保存后启用（启用才允许调用）</span>
      </label>
      <button class="primary" type="button" :disabled="!projectId || working === 'create'" @click="create">
        保存服务
      </button>
    </div>

    <p v-if="error" class="form-error">{{ error }}</p>
    <p v-if="notice" class="banner banner-ok">{{ notice }}</p>

    <p v-if="projectId && !servers.length" class="hint">这个项目还没有配置外部 MCP 服务。</p>

    <div v-for="server in servers" :key="server.id" class="server">
      <div class="server-head">
        <strong>{{ server.name }}</strong>
        <span class="tag" :class="server.status === 'connected' ? 'tag-on' : 'tag-off'">
          {{ server.status === 'connected' ? '已连接' : server.status === 'error' ? '连接失败' : server.status === 'disabled' ? '未启用' : '未发现' }}
        </span>
        <span class="mono hint">{{ server.transport === 'stdio' ? server.command : server.url }}</span>
        <span class="row-actions">
          <button class="ghost" type="button" :disabled="working === server.id" @click="discover(server)">
            {{ working === server.id ? '发现中…' : '发现工具' }}
          </button>
          <button class="ghost" type="button" @click="toggle(server, { enabled: !server.enabled })">
            {{ server.enabled ? '停用' : '启用' }}
          </button>
          <button class="ghost danger-text" type="button" @click="remove(server)">删除</button>
        </span>
      </div>
      <p class="hint">
        凭据：{{ server.secret_keys.length ? server.secret_keys.map((key) => `${key}=${server.secret_hints[key]}`).join('；') : '未配置' }}
        · {{ server.note }}
      </p>
      <p v-if="server.last_error" class="form-error">最近错误：{{ server.last_error }}</p>
      <p v-if="server.admin_only" class="hint">本机 stdio 服务：仅管理员可配置与调用。</p>

      <table v-if="server.tools.length" class="table">
        <thead>
          <tr>
            <th>工具</th>
            <th>只读</th>
            <th>授权</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="tool in server.tools" :key="tool.name">
            <td>
              <span class="mono">{{ tool.name }}</span>
              <span class="hint">{{ tool.description }}</span>
            </td>
            <td>{{ tool.read_only ? '是' : '否' }}</td>
            <td>
              <span class="tag" :class="tool.authorization === 'allowed' ? 'tag-on' : 'tag-off'">
                {{ tool.authorization === 'allowed' ? '已允许' : tool.authorization === 'denied' ? '已拒绝' : '待授权' }}
              </span>
            </td>
            <td class="row-actions">
              <button class="ghost" type="button" @click="decide(server, tool.name, 'allow')">允许</button>
              <button class="ghost" type="button" @click="decide(server, tool.name, 'deny')">拒绝</button>
              <button class="ghost" type="button" @click="decide(server, tool.name, 'revoke')">撤销</button>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-else class="hint">还没有发现工具。点「发现工具」连一次外部服务。</p>

      <details class="try">
        <summary>试调用（只用已授权工具）</summary>
        <div v-for="tool in server.tools.filter((item) => item.callable)" :key="`try-${tool.name}`" class="try-row">
          <span class="mono">{{ tool.name }}</span>
          <input v-model="callArgs[`${server.id}:${tool.name}`]" placeholder='参数 JSON，例如 {"topic":"热斑"}' />
          <button class="ghost" type="button" :disabled="working === `${server.id}:${tool.name}`" @click="call(server, tool.name)">
            调用
          </button>
          <pre v-if="callResult[`${server.id}:${tool.name}`]" class="result" :class="{ bad: !callResult[`${server.id}:${tool.name}`]!.ok }">{{ callResult[`${server.id}:${tool.name}`]!.text }}</pre>
        </div>
        <p v-if="!server.tools.some((item) => item.callable)" class="hint">还没有已授权的工具。</p>
        <button class="ghost" type="button" @click="loadAudit(server)">查看调用记录</button>
        <ul v-if="audit[server.id]?.length" class="audit">
          <li v-for="item in audit[server.id]" :key="item.id">
            <span class="mono">{{ item.tool }}</span>
            <span class="tag" :class="item.ok ? 'tag-on' : 'tag-off'">{{ item.ok ? '成功' : '失败' }}</span>
            <span class="hint">{{ item.duration_ms }}ms · {{ item.created_at }}</span>
            <span v-if="item.error" class="hint">{{ item.error }}</span>
          </li>
        </ul>
      </details>
    </div>
  </section>
</template>

<style scoped>
.server {
  margin-top: 14px;
  padding: 10px;
  border: 1px solid var(--border, #e5e7eb);
  border-radius: 8px;
}
.server-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.row-actions {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  align-items: center;
  margin-left: auto;
}
.checkbox {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
}
.try {
  margin-top: 10px;
}
.try-row {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
  margin: 6px 0;
}
.try-row input {
  flex: 1;
  min-width: 200px;
}
.result {
  width: 100%;
  max-height: 160px;
  overflow: auto;
  margin: 4px 0 0;
  padding: 8px;
  border-radius: 8px;
  background: var(--panel-secondary);
  color: inherit;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-word;
}
.result.bad {
  border-left: 3px solid #dc2626;
}
.audit {
  margin: 6px 0 0;
  padding-left: 16px;
  font-size: 12px;
}
.field textarea {
  width: 100%;
  font-family: inherit;
  font-size: 13px;
}
</style>
