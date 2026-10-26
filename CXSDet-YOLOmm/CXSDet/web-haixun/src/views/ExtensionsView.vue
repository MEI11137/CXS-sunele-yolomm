<script setup lang="ts">
import { ref } from 'vue'

// MCP / 目录授权（由第三人实现，此处为界面 + 模拟状态）
const mcps = ref([
  { id: 'mcp1', name: '本地文件 MCP', type: '本机', status: 'disconnected', tools: [] as { name: string; desc: string; readonly: boolean; authorized: boolean }[] },
  { id: 'mcp2', name: '目录浏览工具', type: '本机', status: 'disconnected', tools: [] as { name: string; desc: string; readonly: boolean; authorized: boolean }[] },
])

const dirs = ref<{ path: string; scope: string; readonly: boolean; authorized: boolean }[]>([])
const dirNotice = ref('')

function connectMcp(m: typeof mcps.value[0]) {
  // 模拟：列出工具清单
  m.tools = [
    { name: 'list_files', desc: '列出目录文件', readonly: true, authorized: false },
    { name: 'read_file', desc: '读取文件内容', readonly: true, authorized: false },
    { name: 'write_file', desc: '写入文件', readonly: false, authorized: false },
  ]
  m.status = 'connected'
}

function disconnectMcp(m: typeof mcps.value[0]) {
  m.status = 'disconnected'
  m.tools = []
}

function authorizeTool(_m: typeof mcps.value[0], tool: { name: string; desc: string; readonly: boolean; authorized: boolean }) {
  if (!tool.readonly) {
    dirNotice.value = '仅允许逐个放行经核对的只读工具；写入类工具需额外确认。'
    return
  }
  tool.authorized = true
}

function addDir() {
  // 模拟：目录选择仅授权该目录，不等于获得整台电脑权限
  dirs.value.push({
    path: 'D:/demo/叶片数据集',
    scope: '仅该目录',
    readonly: true,
    authorized: true,
  })
  dirNotice.value = '目录已授权（只读）。浏览器选择目录不等于服务器获得整台电脑的文件访问权限。'
}
</script>

<template>
  <div class="page">
    <div class="page-header">
      <h1 class="page-title">扩展与连接</h1>
      <span class="tag">交互原型 · 模拟数据</span>
    </div>

    <!-- MCP 连接 -->
    <section class="section">
      <h2 class="section-title">MCP / 工具连接</h2>
      <div class="mcp-list">
        <div v-for="m in mcps" :key="m.id" class="panel mcp-card">
          <div class="mcp-head">
            <div class="mcp-name">{{ m.name }}</div>
            <div class="mcp-meta">
              <span class="tag">{{ m.type }}</span>
              <span class="tag" :class="m.status === 'connected' ? 'tag-success' : ''">
                {{ m.status === 'connected' ? '已连接' : '未连接' }}
              </span>
            </div>
          </div>
          <div v-if="m.tools.length" class="tool-list">
            <div class="tool-item" v-for="t in m.tools" :key="t.name">
              <div class="tool-info">
                <span class="tool-name mono">{{ t.name }}</span>
                <span class="tool-desc">{{ t.desc }}</span>
                <span class="tag" :class="t.readonly ? 'tag-accent' : ''">{{ t.readonly ? '只读' : '可写' }}</span>
              </div>
              <button
                v-if="!t.authorized"
                class="btn btn-sm"
                :disabled="!t.readonly"
                @click="authorizeTool(m, t)"
              >
                {{ t.readonly ? '授权' : '需额外确认' }}
              </button>
              <span v-else class="tag tag-success">已授权</span>
            </div>
          </div>
          <div class="mcp-actions">
            <button v-if="m.status === 'disconnected'" class="btn btn-sm" @click="connectMcp(m)">连接</button>
            <button v-else class="btn btn-sm btn-danger" @click="disconnectMcp(m)">断开</button>
          </div>
        </div>
      </div>
    </section>

    <!-- 目录授权 -->
    <section class="section">
      <h2 class="section-title">目录授权</h2>
      <div class="panel dir-card">
        <p class="dir-desc">授权目录用于文件检查、修改预览与候选输出。目录授权仅针对所选目录，不等于授予整台电脑的文件访问权限。</p>
        <button class="btn" @click="addDir">选择目录</button>
        <div v-if="dirs.length" class="dir-list">
          <div v-for="d in dirs" :key="d.path" class="dir-item">
            <span class="mono">{{ d.path }}</span>
            <span class="tag tag-accent">只读</span>
            <button class="btn btn-sm btn-danger btn-ghost" @click="dirs = dirs.filter(x => x.path !== d.path)">移除</button>
          </div>
        </div>
      </div>
    </section>

    <div v-if="dirNotice" class="notice">{{ dirNotice }}</div>
  </div>
</template>

<style scoped>
.page {
  padding: var(--content-pad);
  max-width: 800px;
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
.mcp-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.mcp-card {
  padding: 16px;
}
.mcp-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}
.mcp-name {
  font-size: 14px;
  font-weight: 600;
}
.mcp-meta {
  display: flex;
  gap: 6px;
}
.tool-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 12px;
}
.tool-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 10px;
  background: var(--panel-secondary);
  border-radius: var(--radius-sm);
}
.tool-info {
  display: flex;
  align-items: center;
  gap: 8px;
}
.tool-name {
  font-size: 12px;
}
.tool-desc {
  font-size: 12px;
  color: var(--text-secondary);
}
.mcp-actions {
  display: flex;
  justify-content: flex-end;
}
.dir-card {
  padding: 16px;
}
.dir-desc {
  font-size: 13px;
  color: var(--text-secondary);
  line-height: 1.6;
  margin: 0 0 12px;
}
.dir-list {
  margin-top: 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.dir-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  background: var(--panel-secondary);
  border-radius: var(--radius-sm);
  font-size: 12px;
}
</style>
