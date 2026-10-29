<script setup lang="ts">
import { ref } from 'vue'
import { request } from '@/services/http'
import { useWorkspaceStore } from '@/stores/workspace'
import { changeResource, useResourceRefresh } from '@/utils/resourceLifecycle'
import type { ResourceKind, DeletedResource } from '@/utils/resourceLifecycle'
const props = defineProps<{ kinds: ResourceKind[] }>()
const emit = defineEmits<{ changed: [] }>()
const workspace = useWorkspaceStore()
const opened = ref(false), busy = ref(false), error = ref('')
const rows = ref<DeletedResource[]>([])
async function load() {
  if (!opened.value) return
  try { rows.value = (await request<DeletedResource[]>('/api/agent/resources/lifecycle', { query: { project_id: workspace.currentId } })).filter(row => props.kinds.includes(row.kind)); error.value = '' }
  catch (e) { error.value = (e as Error).message }
}
useResourceRefresh(load)
async function restore(row: DeletedResource) {
  if (busy.value) return
  busy.value = true
  try { if (await changeResource(row.kind, row.id, 'restore')) { await load(); emit('changed') } }
  catch (e) { error.value = (e as Error).message }
  finally { busy.value = false }
}
</script>
<template>
  <details class="panel resource-recycle" @toggle="opened = ($event.target as HTMLDetailsElement).open; load()">
    <summary>已删除资源 · 回收站</summary>
    <p>仅当前项目的数据集、模型或草稿；删除保留文件和历史来源，恢复不会启动训练或自动发布。</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <div v-for="row in rows" :key="row.kind + row.id" class="recycle-row"><span>{{ row.name }} · {{ row.kind === 'model' ? '模型' : row.kind === 'draft' ? '草稿' : '数据集' }}</span><button class="btn btn-sm" :disabled="busy" @click="restore(row)">恢复</button></div>
    <p v-if="!rows.length && !error">没有已删除资源</p>
  </details>
</template>
<style scoped>.resource-recycle{padding:16px;margin-top:20px;font-size:13px}.resource-recycle summary{cursor:pointer;font-weight:600}.resource-recycle p{color:var(--text-secondary);line-height:1.6}.recycle-row{display:flex;justify-content:space-between;gap:12px;padding:10px 0;border-top:1px solid var(--border);overflow-wrap:anywhere}</style>
