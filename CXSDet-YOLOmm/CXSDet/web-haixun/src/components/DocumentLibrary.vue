<script setup lang="ts">
import { ref, watch, onBeforeUnmount } from 'vue'
import { getAdapter } from '@/services'
import { useWorkspaceStore } from '@/stores/workspace'
const workspace = useWorkspaceStore()
const documents = ref<Array<Record<string, unknown>>>([])
const hits = ref<Array<Record<string, unknown>>>([])
const documentId = ref('')
const query = ref('')
const file = ref<File | null>(null)
const error = ref('')
const busy = ref(false)
const sourceUrl = ref('')
const sourceDocument = ref('')
function clearSource() { if (sourceUrl.value) URL.revokeObjectURL(sourceUrl.value); sourceUrl.value = ''; sourceDocument.value = '' }
onBeforeUnmount(clearSource)
async function source(d: Record<string, unknown>) {
  try {
    const api = getAdapter()
    const asset = await api.getFileMetadata(String(d.asset_id))
    const url = await api.fileObjectUrl(asset.bucket, asset.object_key)
    clearSource(); sourceUrl.value = url; sourceDocument.value = String(d.id)
  } catch (e) { error.value = (e as Error).message }
}
let generation = 0
async function load() {
  const token = ++generation
  if (!workspace.currentId) { documents.value = []; return }
  try {
    const rows = await getAdapter().listDocuments(workspace.currentId)
    if (token === generation) documents.value = rows
  } catch (e) { if (token === generation) error.value = (e as Error).message }
}
watch(() => workspace.currentId, () => { clearSource(); documentId.value = ''; hits.value = []; error.value = ''; void load() }, { immediate: true })
async function parse() {
  if (!file.value || !workspace.currentId || busy.value) return
  busy.value = true; error.value = ''
  try {
    const api = getAdapter()
    const asset = await api.uploadProjectFile(workspace.currentId, file.value)
    await api.parseDocument(asset.file_id, file.value.name)
    file.value = null
    await load()
  } catch (e) { error.value = (e as Error).message; await load() }
  finally { busy.value = false }
}
async function search() {
  if (!documentId.value || !query.value.trim() || busy.value) return
  busy.value = true; error.value = ''; hits.value = []
  try { hits.value = ((await getAdapter().searchDocument(documentId.value, query.value)).hits ?? []) as Array<Record<string, unknown>> }
  catch (e) { error.value = (e as Error).message }
  finally { busy.value = false }
}
</script>
<template>
  <section class="panel library">
    <h2>项目资料 · PDF / Word</h2>
    <p>上传后保存文本和原文定位；扫描页会提示需要识别，DOCX 按段落和表格定位。可在助手中明确引用文档 ID 提问。</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <label>资料文件 <input type="file" accept=".pdf,.docx" @change="file = ($event.target as HTMLInputElement).files?.[0] || null" /></label>
    <button class="btn" :disabled="busy || !file || !workspace.currentId" @click="parse">上传并解析</button>
    <ul><li v-for="d in documents" :key="String(d.id)"><strong>{{ d.title }}</strong> · {{ d.status }} · {{ d.page_count }} 页 · <code>{{ d.id }}</code><button class="btn btn-sm" @click="source(d)">加载原文</button><a v-if="sourceDocument === d.id && sourceUrl" :href="sourceUrl" target="_blank" rel="noopener">打开原文件</a><p v-if="d.warnings">{{ d.warnings }}</p><p v-if="d.error">{{ d.error }}</p></li></ul>
    <form @submit.prevent="search">
      <select v-model="documentId" class="select" aria-label="检索资料"><option value="">选择资料</option><option v-for="d in documents" :key="String(d.id)" :value="String(d.id)">{{ d.title }}</option></select>
      <input v-model="query" class="input" placeholder="输入检索词" aria-label="检索词" />
      <button class="btn" :disabled="busy || !documentId || !query.trim()">检索原文</button>
    </form>
    <article v-for="h in hits" :key="String(h.block)"><strong>{{ h.citation }}</strong><p>{{ h.excerpt }}</p><small>文档 {{ h.document_id }} · 版本 {{ h.document_version }}</small><a v-if="sourceDocument === h.document_id && sourceUrl && h.page_no" :href="sourceUrl + '#page=' + h.page_no" target="_blank" rel="noopener">查看原文此页</a></article>
    <p v-if="!busy && query && documentId && !hits.length">暂无匹配片段。可换一个检索词；未找到不表示文档没有该内容。</p>
  </section>
</template>
<style scoped>.library { padding: 16px; margin-bottom: 20px; } article { border-top: 1px solid var(--border); padding: 10px 0; } button { margin: 8px; } code { overflow-wrap: anywhere; }</style>
