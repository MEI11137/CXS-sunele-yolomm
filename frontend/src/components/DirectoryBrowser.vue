<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { errorMessage, http } from '@/api/http'
import type { BrowseResult, DirectoryEntry } from '@/types'

/**
 * 服务器目录浏览器。
 * 只能看到后端授予当前账号的根目录（按账号授予，不是全站共享），不会列出整块磁盘。
 */
const props = defineProps<{ startPath?: string }>()
const emit = defineEmits<{ (event: 'select', path: string): void; (event: 'close'): void }>()

const current = ref('')
const base = ref('')
const entries = ref<DirectoryEntry[]>([])
const blocked = ref(0)
const loading = ref(false)
const error = ref('')

async function browse(path = ''): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const url = path ? `/directories/browse?path=${encodeURIComponent(path)}` : '/directories/browse'
    const data = await http.get<BrowseResult>(url)
    current.value = data.path ?? ''
    base.value = data.base ?? ''
    entries.value = data.entries
    blocked.value = data.blocked ?? 0
  } catch (failure) {
    error.value = errorMessage(failure)
  } finally {
    loading.value = false
  }
}

function enter(entry: DirectoryEntry): void {
  if (entry.is_dir) void browse(entry.path)
}

function up(): void {
  if (!current.value || current.value === base.value) return
  const parent = current.value.replace(/[\\/][^\\/]+$/, '')
  if (!parent || parent.length < base.value.length) {
    void browse(base.value)
    return
  }
  void browse(parent)
}

onMounted(() => {
  void browse(props.startPath ?? '')
})
</script>

<template>
  <div class="browser">
    <div class="browser-bar">
      <button class="ghost small" type="button" :disabled="!current || current === base" @click="up">
        上一级
      </button>
      <code class="browser-path">{{ current || '选择授权根目录' }}</code>
    </div>
    <p v-if="error" class="form-error">{{ error }}</p>
    <ul v-if="entries.length" class="browser-list">
      <li v-for="entry in entries" :key="entry.path">
        <button class="entry" type="button" :disabled="!entry.is_dir" @click="enter(entry)">
          <span class="entry-icon" aria-hidden="true">{{ entry.is_dir ? '📁' : '📄' }}</span>
          <span class="entry-name">{{ entry.name }}</span>
          <span v-if="entry.is_link" class="tag tag-off">链接</span>
          <span v-else-if="!entry.is_dir" class="entry-size">{{ Math.ceil(entry.size / 1024) }} KB</span>
        </button>
      </li>
    </ul>
    <p v-else-if="!loading" class="empty-hint">这个目录下没有可显示的条目。</p>
    <p v-if="blocked" class="hint">
      有 {{ blocked }} 个链接指向授权范围之外，已隐藏且不可进入。
    </p>
    <div class="browser-actions">
      <button class="primary" type="button" :disabled="!current || loading" @click="emit('select', current)">
        选择当前目录
      </button>
      <button class="ghost" type="button" @click="emit('close')">取消</button>
    </div>
  </div>
</template>
