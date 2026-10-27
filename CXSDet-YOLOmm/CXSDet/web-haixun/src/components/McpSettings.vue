<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { getAdapter } from '@/services'
import { useWorkspaceStore } from '@/stores/workspace'
import type { ToolInfo } from '@/services'
const emit = defineEmits<{ changed: [] }>()
const workspace = useWorkspaceStore()
const rows = ref<Array<Record<string, unknown>>>([])
const tools = ref<ToolInfo[]>([])
const grants = ref<Array<Record<string, unknown>>>([])
const form = ref({ id: '', name: '', transport: 'stdio', command: '', endpoint: '', arguments: '[]', credential: '', enabled: true })
const busy = ref(false)
const error = ref('')
async function load() {
  const api = getAdapter()
  const [r, t, g] = await Promise.all([api.listMcpConnections(), api.listTools(), api.listGrants()])
  rows.value = r; tools.value = t.filter(x => x.source === 'mcp'); grants.value = g
}
async function run(action: () => Promise<unknown>) {
  if (busy.value) return
  busy.value = true; error.value = ''
  try { await action(); await load(); emit('changed') }
  catch (e) { error.value = (e as Error).message }
  finally { busy.value = false }
}
function edit(r: Record<string, unknown>) {
  form.value = { id: String(r.id), name: String(r.name), transport: String(r.transport), command: String(r.command || ''), endpoint: String(r.endpoint || ''), arguments: JSON.stringify(r.args || []), credential: '', enabled: r.enabled === true }
}
async function save() {
  await run(async () => {
    const args: unknown = JSON.parse(form.value.arguments)
    if (!Array.isArray(args) || args.some(x => typeof x !== 'string')) throw new Error('参数应为 JSON 字符串数组，如 ["server.py"]')
    if (!window.confirm('确认保存该 MCP 配置？本机程序会在发现/调用时由后端启动。请使用已核对过的服务；网站权限不能替外部程序限制其电脑权限。')) return
    const r = await getAdapter().saveMcpConnection({ ...form.value, id: form.value.id || undefined, arguments: args })
    form.value.id = String(r.id); form.value.credential = ''
  })
}
async function grant(t: ToolInfo) {
  if (!window.confirm(`我已核对「${t.title}」为只读且不计费，允许当前项目自动调用该版本。连接或工具变化后须重新授权。`)) return
  await run(() => getAdapter().grantTool({ server_id: t.serverId, tool_name: t.name, tool_version: t.version, project_id: workspace.currentId }))
}
onMounted(() => run(load))
</script>
<template>
  <section class="panel mcp">
    <h2>MCP 连接</h2><p v-if="error" role="alert">{{ error }}</p>
    <form @submit.prevent="save">
      <label>名称 <input v-model="form.name" class="input" required /></label>
      <label>传输 <select v-model="form.transport" class="select"><option value="stdio">本机 stdio</option><option value="http">Streamable HTTP</option></select></label>
      <label v-if="form.transport === 'stdio'">程序完整路径 <input v-model="form.command" class="input" required /></label>
      <label v-else>服务地址 <input v-model="form.endpoint" class="input" type="url" required /></label>
      <label v-if="form.transport === 'stdio'">参数（JSON 数组）<textarea v-model="form.arguments" class="input" /></label>
      <label>个人凭据（留空保留；HTTP 为 Bearer，本机为 MCP_API_KEY）<input v-model="form.credential" class="input" type="password" autocomplete="off" /></label>
      <label><input v-model="form.enabled" type="checkbox" />启用</label>
      <button class="btn" :disabled="busy">保存配置</button>
      <button type="button" class="btn" @click="form.id = ''; form.name = ''; form.credential = ''">新连接</button>
    </form>
    <div v-for="r in rows" :key="String(r.id)" class="row">
      <strong>{{ r.name }}</strong> · {{ r.transport }} · {{ r.status }} · 版本 {{ r.revision }}
      <button class="btn btn-sm" :disabled="busy" @click="edit(r)">编辑 / 启停</button>
      <button class="btn btn-sm" :disabled="busy || r.enabled !== true" @click="run(() => getAdapter().discoverMcp(String(r.id)))">连接并发现工具</button>
    </div>
    <div v-for="t in tools" :key="t.name" class="row">
      <strong>{{ t.title }}</strong><p>{{ t.description }}</p>
      <span>{{ t.risk }} · {{ t.version }} · 默认逐次确认</span>
      <button v-if="t.risk === 'read' && !t.billable" class="btn btn-sm" :disabled="busy || !workspace.currentId" @click="grant(t)">核对并放行此只读工具</button>
    </div>
    <div v-for="g in grants" :key="String(g.id)" class="row">{{ g.tool_name }} · {{ g.tool_version }}<button class="btn btn-sm" :disabled="busy" @click="run(() => getAdapter().revokeGrant(String(g.id)))">撤销放行</button></div>
  </section>
</template>
<style scoped>.mcp { padding: 16px; margin-bottom: 20px; } label { display: block; margin: 10px 0; } .row { padding: 12px 0; border-top: 1px solid var(--border); } button { margin: 4px; }</style>
