<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref } from 'vue'
import { getAdapter } from '@/services'
import type { LocalFolderListing } from '@/types'

const props = defineProps<{ initialPath?: string }>()
const emit = defineEmits<{ select: [path: string]; cancel: [] }>()
const listing = ref<LocalFolderListing>({ path: '', parent: null, directories: [] })
const address = ref(props.initialPath || '')
const folderName = ref('')
const loading = ref(false)
const error = ref('')
let requestVersion = 0

async function browse(path: string) {
  const version = ++requestVersion
  loading.value = true
  error.value = ''
  try {
    const result = await getAdapter().browseLocalFolders(path)
    if (version !== requestVersion) return
    listing.value = result
    address.value = result.path
  } catch (e) {
    if (version === requestVersion) error.value = e instanceof Error ? e.message : '无法读取文件夹'
  } finally {
    if (version === requestVersion) loading.value = false
  }
}

async function createFolder() {
  if (!listing.value.path || !folderName.value.trim() || loading.value) return
  loading.value = true
  error.value = ''
  const version = ++requestVersion
  try {
    const result = await getAdapter().createLocalFolder(listing.value.path, folderName.value)
    if (version !== requestVersion) return
    folderName.value = ''
    await browse(result.path)
  } catch (e) {
    if (version === requestVersion) error.value = e instanceof Error ? e.message : '新建文件夹失败'
  } finally {
    if (version === requestVersion) loading.value = false
  }
}

onMounted(() => browse(props.initialPath || ''))
onBeforeUnmount(() => { requestVersion++ })
</script>

<template>
  <section class="folder-picker" aria-label="选择本机文件夹">
    <div class="folder-row">
      <input v-model="address" class="input" aria-label="文件夹绝对路径" placeholder="例如 E:\海巡项目" @keydown.enter.prevent="browse(address)" />
      <button class="btn" :disabled="loading" @click="browse(address)">前往</button>
    </div>
    <div class="folder-row">
      <button class="btn btn-sm" :disabled="loading" @click="browse('')">磁盘</button>
      <button class="btn btn-sm" :disabled="loading || listing.parent === null" @click="browse(listing.parent || '')">上一级</button>
      <span class="folder-status">{{ loading ? '读取中…' : `${listing.directories.length} 个文件夹` }}</span>
    </div>
    <div class="folder-list" :aria-busy="loading">
      <button v-for="dir in listing.directories" :key="dir.path" class="folder-item" :disabled="loading" @click="browse(dir.path)">
        <span aria-hidden="true">📁</span> {{ dir.name }}
      </button>
      <p v-if="!loading && !listing.directories.length" class="folder-status">此处没有可浏览的子文件夹</p>
    </div>
    <div class="folder-row">
      <input v-model="folderName" class="input" aria-label="新文件夹名称" placeholder="新文件夹名称" @keydown.enter.prevent="createFolder" />
      <button class="btn" :disabled="loading || !listing.path || !folderName.trim()" @click="createFolder">新建文件夹</button>
    </div>
    <p class="folder-status">新建会立即创建真实文件夹。这里只选择目录，不会上传或搬移已有文件。</p>
    <p v-if="error" class="folder-error" role="alert">{{ error }}</p>
    <div class="folder-row folder-actions">
      <button class="btn" @click="emit('cancel')">收起</button>
      <button class="btn btn-primary" :disabled="loading || !listing.path || !!error" @click="emit('select', listing.path)">使用此文件夹</button>
    </div>
  </section>
</template>

<style scoped>
.folder-picker { border: 1px solid var(--border); border-radius: 6px; padding: 12px; margin-top: 10px; }
.folder-row { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; }
.folder-row .input { min-width: 0; flex: 1; }
.folder-row .btn { flex-shrink: 0; }
.folder-list { height: 160px; overflow: auto; border: 1px solid var(--border); margin-bottom: 10px; }
.folder-item { display: block; border: 0; background: transparent; color: var(--text); width: 100%; text-align: left; padding: 7px 10px; cursor: pointer; overflow-wrap: anywhere; }
.folder-item:hover { background: var(--panel-secondary); }
.folder-status { color: var(--text-secondary); font-size: 12px; margin: 6px 0; }
.folder-error { color: #c0392b; font-size: 12px; }
.folder-actions { justify-content: flex-end; margin-bottom: 0; }
</style>
