<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { getAdapter } from '@/services'
import type { ToolInfo } from '@/services'
import McpSettings from '@/components/McpSettings.vue'
import DirectorySettings from '@/components/DirectorySettings.vue'

// 这里只展示后端真实提供的工具与授权记录。
// MCP 服务器的连接管理（握手、发现工具、逐工具放行）目前**没有 HTTP 接口**，
// 因此页面不提供"连接"按钮，也不假装已连接。
const tools = ref<ToolInfo[]>([])
const grants = ref<Array<Record<string, unknown>>>([])
const loading = ref(false)
const loadError = ref('')

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const [t, g] = await Promise.all([getAdapter().listTools(), getAdapter().listGrants()])
    tools.value = t
    grants.value = g
  } catch (e) {
    loadError.value = '读取工具清单失败：' + (e as Error).message
  } finally {
    loading.value = false
  }
}

onMounted(load)

/** 工具名去掉内部前缀，便于阅读；真实名保留在 title 里 */
function shortName(name: string): string {
  return name.replace(/^business_/, '')
}

function riskLabel(risk: string): string {
  return risk === 'critical' ? '训练 / 发布' : risk === 'write' ? '写入' : risk === 'external' ? '外部调用' : '只读'
}

function riskClass(risk: string): string {
  return risk === 'critical' ? 'tag-error' : risk === 'write' ? 'tag-warn' : risk === 'external' ? 'tag-error' : 'tag-success'
}
</script>

<template>
  <div class="page">
    <McpSettings @changed="load" />
    <DirectorySettings />
    <div class="page-header">
      <h1 class="page-title">扩展与连接</h1>
      <span class="tag num">{{ tools.length }} 个工具 · {{ grants.length }} 条授权</span>
    </div>

    <div v-if="loadError" class="notice notice-error">{{ loadError }}</div>

    <section class="section">
      <h2 class="section-title">当前可用工具（同一份清单也给模型看）</h2>
      <div v-if="loading" class="empty panel"><div class="empty-desc">正在读取…</div></div>
      <div v-else-if="!tools.length" class="empty panel">
        <div class="empty-title">没有可用工具</div>
        <div class="empty-desc">业务后端未注入或当前账号没有可用工具。</div>
      </div>
      <div v-else class="panel table-wrap">
        <table class="data-table">
          <thead>
            <tr><th>工具</th><th>说明</th><th>风险</th><th>需要确认</th><th>状态</th></tr>
          </thead>
          <tbody>
            <tr v-for="t in tools" :key="t.name">
              <td class="mono" :title="t.name">{{ shortName(t.name) }}</td>
              <td class="hint">{{ t.description || t.title }}</td>
              <td><span class="tag" :class="riskClass(t.risk)">{{ riskLabel(t.risk) }}</span></td>
              <td>{{ t.requires_confirmation ? '是' : '否' }}</td>
              <td>
                <span class="tag" :class="t.available ? 'tag-success' : 'tag-error'">
                  {{ t.available ? '可用' : '不可用' }}
                </span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section class="section">
      <h2 class="section-title">外部工具授权</h2>
      <div v-if="!grants.length" class="empty panel">
        <div class="empty-title">暂无授权记录</div>
        <div class="empty-desc">
          外部工具默认逐次确认；可在上方核对并放行具体只读工具。
        </div>
      </div>
      <div v-else class="panel table-wrap">
        <table class="data-table">
          <thead>
            <tr><th>服务</th><th>工具</th><th>版本</th><th>范围</th></tr>
          </thead>
          <tbody>
            <tr v-for="(g, i) in grants" :key="i">
              <td class="mono">{{ g.server_id }}</td>
              <td class="mono">{{ g.tool_name }}</td>
              <td class="num">{{ g.tool_version }}</td>
              <td>{{ g.project_id || '当前账号' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section class="section">
      <h2 class="section-title">暂不可用的能力</h2>
      <div class="panel notice-block">
        <ul class="cap-list">
          <li>
            <strong>MCP 服务器</strong>：需自行准备本机程序或网络服务，再在上方保存、连接与发现。
          </li>
          <li>
            <strong>受控目录授权</strong>：目录类工具（inspect / search / transform）由助手在对话中按授权目录调用，
            可在上方填写完整路径并授权，候选写入须另行确认。
          </li>
        </ul>
      </div>
    </section>
  </div>
</template>

<style scoped>
.page {
  padding: var(--content-pad);
  max-width: 1000px;
  margin: 0 auto;
}
.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
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
.table-wrap {
  padding: 0;
  overflow-x: auto;
}
.data-table th {
  white-space: nowrap;
}
.data-table td {
  vertical-align: top;
}
.hint {
  font-size: 12px;
  color: var(--text-secondary);
}
.notice-block {
  padding: 16px;
}
.cap-list {
  margin: 0;
  padding-left: 18px;
  font-size: 13px;
  line-height: 1.7;
  color: var(--text-secondary);
}
</style>
