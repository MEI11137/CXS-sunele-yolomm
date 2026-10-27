<script setup lang="ts">
import { ref, watch } from 'vue'
import { getAdapter } from '@/services'
import { useWorkspaceStore } from '@/stores/workspace'
const workspace = useWorkspaceStore()
const directories = ref<Array<Record<string, unknown>>>([])
const root = ref('')
const label = ref('')
const error = ref('')
const busy = ref(false)
let generation = 0
async function load() {
  const token = ++generation
  if (!workspace.currentId) return
  try { const result = await getAdapter().listDirectories(workspace.currentId); if (token === generation) { directories.value = result; error.value = '' } }
  catch (e) { if (token === generation) error.value = (e as Error).message }
}
watch(() => workspace.currentId, () => { directories.value = []; void load() }, { immediate: true })
async function authorize() {
  if (busy.value || !window.confirm(`允许当前项目的受控文件工具读取目录 ${root.value}，并生成新的数据候选？原数据不覆盖；外部 MCP 仍按自身权限运行。`)) return
  busy.value = true
  try { await getAdapter().authorizeDirectory(workspace.currentId, root.value, label.value); root.value = ''; await load() }
  catch (e) { error.value = (e as Error).message }
  finally { busy.value = false }
}
async function disable(id: string) {
  if (busy.value) return
  busy.value = true
  try { await getAdapter().disableDirectory(id); await load() }
  catch (e) { error.value = (e as Error).message }
  finally { busy.value = false }
}
</script>
<template><section class="panel directories">
  <h2>当前项目的本机目录授权</h2><p v-if="error" role="alert">{{ error }}</p>
  <form @submit.prevent="authorize"><label>完整目录路径 <input v-model="root" class="input" required /></label><label>名称 <input v-model="label" class="input" /></label><button class="btn" :disabled="busy || !workspace.currentId">授权此目录</button></form>
  <ul><li v-for="d in directories" :key="String(d.id)">{{ d.label || d.root }} · {{ d.id }} · {{ d.enabled ? '已授权' : '已停用' }}<button v-if="d.enabled" class="btn btn-sm" :disabled="busy" @click="disable(String(d.id))">撤销目录授权</button></li></ul>
  <p>在助手中引用目录 ID，可检查、搜索和生成整理候选。文件写入仍需确认；不会开放任意 Shell 或脚本执行。</p>
</section></template>
<style scoped>.directories { padding: 16px; margin-bottom: 20px; } label { display: block; margin-bottom: 8px; } li { overflow-wrap: anywhere; }</style>
