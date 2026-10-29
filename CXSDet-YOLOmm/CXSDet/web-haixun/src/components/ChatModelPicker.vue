<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { getAdapter } from '@/services'
import { useAuthStore } from '@/stores/auth'
import type { PersonalConnection } from '@/types'

const props = defineProps<{ disabled?: boolean }>()
const auth = useAuthStore()
const open = ref(false)
const connection = ref<PersonalConnection | null>(null)
const models = ref<string[]>([])
const query = ref('')
const loading = ref(false)
const saving = ref(false)
const notice = ref('')
const picker = ref<HTMLDivElement | null>(null)
const popupStyle = ref<Record<string, string>>({})
let generation = 0
const filtered = computed(() => models.value.filter(name => (name + ' ' + modelLabel(name)).toLowerCase().includes(query.value.trim().toLowerCase())))
const custom = computed(() => query.value.trim() && !models.value.includes(query.value.trim()))
function modelLabel(id: string) {
  const official = connection.value?.provider === 'deepseek' &&
    /^https:\/\/api\.deepseek\.com\/?(?:v1\/?)?$/.test(connection.value?.api_url || '')
  return official && id === 'deepseek-flash' ? 'DeepSeek V4.1 Flash' : id
}

async function load() {
  const version = ++generation
  try {
    const result = await getAdapter().getConnection()
    if (version !== generation) return
    connection.value = result
    models.value = result.model ? [result.model] : []
    notice.value = ''
  } catch (e) { if (version === generation) notice.value = (e as Error).message }
}
async function toggle() {
  open.value = !open.value
  if (open.value) {
    query.value = ''
    await nextTick()
    if (picker.value) {
      const left = picker.value.getBoundingClientRect().left
      const width = Math.min(350, window.innerWidth - 32)
      popupStyle.value = { width: width + 'px', left: Math.max(16 - left, Math.min(0, window.innerWidth - 16 - left - width)) + 'px' }
    }
    await load()
    if (open.value && connection.value?.configured) void discover()
  }
}
async function discover() {
  if (loading.value) return
  const version = generation
  loading.value = true
  notice.value = ''
  try {
    const result = await getAdapter().listConnectionModels()
    if (version !== generation) return
    models.value = result.models
    if (result.discovery_error) notice.value = '服务未返回模型目录：' + result.discovery_error + '。可手动输入模型 ID。'
  } catch (e) { if (version === generation) notice.value = '读取目录失败：' + (e as Error).message }
  finally { loading.value = false }
}
async function select(model: string) {
  if (saving.value || props.disabled) return
  saving.value = true
  const version = generation
  try {
    // 提交前重新取本人配置，保留当前服务地址及服务端 Key。
    const current = await getAdapter().getConnection()
    if (version !== generation) return
    if (!current.configured || !current.provider || !current.api_url) throw new Error('请先在设置中连接大模型')
    const result = model === current.model ? current : await getAdapter().saveConnection({
      provider: current.provider, apiUrl: current.api_url, model,
    })
    if (version !== generation) return
    connection.value = result
    open.value = false
    notice.value = '已选择 ' + model + '，下一次对话生效。'
  } catch (e) { if (version === generation) notice.value = (e as Error).message }
  finally { saving.value = false }
}
watch(() => auth.user?.id, () => { generation++; open.value = false; connection.value = null; models.value = []; void load() })
onMounted(load)
onBeforeUnmount(() => { generation++ })
</script>

<template>
  <div ref="picker" class="model-picker" @keydown.esc="open = false">
    <button class="model-trigger" :disabled="disabled || saving" :aria-expanded="open" aria-label="选择对话模型" @click="toggle">
      <svg viewBox="0 0 20 20" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.4"><path d="m10 2 7 4v8l-7 4-7-4V6zM3 6l7 4 7-4M10 10v8" /></svg>
      <span :title="connection?.model">{{ saving ? '正在切换…' : connection?.model ? modelLabel(connection.model) : '连接模型' }}</span>
      <span aria-hidden="true">⌄</span>
    </button>
    <template v-if="open">
      <button class="picker-dismiss" aria-label="关闭模型选择" @click="open = false"></button>
      <div class="model-popover" :style="popupStyle" role="dialog" aria-label="对话模型">
        <div class="picker-heading"><strong>选择模型</strong><button class="text-button" :disabled="loading || !connection?.configured" @click="discover">{{ loading ? '读取中…' : '刷新模型列表' }}</button></div>
        <p class="picker-hint">当前账号的对话、后端引擎与 AI 核查共用此连接，下一次请求生效。</p>
        <template v-if="connection?.configured">
          <input v-model="query" class="model-search" aria-label="搜索或输入模型 ID" placeholder="搜索，或输入服务商提供的模型 ID" maxlength="200" @keydown.enter="custom ? select(query.trim()) : undefined" />
          <div class="model-options">
            <button v-for="model in filtered" :key="model" class="model-option" :disabled="saving || disabled" @click="select(model)"><span>{{ modelLabel(model) }}<small v-if="modelLabel(model) !== model" class="model-api-id">API ID：{{ model }}</small></span><span v-if="model === connection.model" class="selected-mark">✓</span></button>
            <button v-if="custom" class="model-option" :disabled="saving || disabled" @click="select(query.trim())">使用 {{ query.trim() }}</button>
          </div>
          <p class="picker-hint">列表不代表已验证图片或工具能力；可在设置中执行能力检查。</p>
        </template>
        <p v-if="notice" class="picker-notice" role="status">{{ notice }}</p>
        <RouterLink class="picker-settings" to="/settings" @click="open = false">{{ connection?.configured ? '管理 API 连接 ↗' : '前往设置连接大模型 ↗' }}</RouterLink>
      </div>
    </template>
    <span v-if="notice && !open" class="sr-only" role="status">{{ notice }}</span>
  </div>
</template>

<style scoped>
.model-picker { position: relative; min-width: 0; }
.model-trigger { display: flex; align-items: center; gap: 7px; border: 0; border-radius: 8px; padding: 7px 9px; max-width: min(240px, 100%); min-width: 0; color: var(--text-secondary); background: transparent; cursor: pointer; font-size: 12px; }
.model-trigger span:first-of-type { overflow: hidden; min-width: 0; text-overflow: ellipsis; white-space: nowrap; }
.model-trigger:hover { background: var(--bg-selected); color: var(--text); }
.model-trigger:disabled { opacity: .5; cursor: wait; }
.picker-dismiss { position: fixed; inset: 0; border: 0; background: transparent; z-index: 30; cursor: default; }
.model-popover { position: absolute; bottom: calc(100% + 14px); left: 0; width: min(350px, calc(100vw - 70px)); max-height: min(460px, 70vh); overflow-y: auto; padding: 16px; z-index: 31; border: 1px solid var(--border); border-radius: 14px; background: var(--panel); box-shadow: var(--shadow-md); color: var(--text); }
.picker-heading { display: flex; align-items: center; justify-content: space-between; gap: 10px; font-size: 14px; }
.text-button { border: 0; padding: 4px 0; background: transparent; color: var(--accent); cursor: pointer; font-size: 11px; }
.text-button:disabled { opacity: .5; cursor: wait; }
.picker-hint, .picker-notice { color: var(--text-secondary); font-size: 11px; line-height: 1.65; margin: 10px 0; }
.picker-notice { color: var(--warn-text); }
.model-search { width: 100%; border: 1px solid var(--border-control); border-radius: 8px; background: var(--panel-secondary); color: var(--text); padding: 9px; font: inherit; font-size: 12px; }
.model-options { max-height: 220px; overflow: auto; margin: 8px -5px; }
.model-option { display: flex; justify-content: space-between; width: 100%; padding: 9px; border: 0; border-radius: 6px; background: transparent; color: var(--text); text-align: left; overflow-wrap: anywhere; cursor: pointer; gap: 8px; }
.model-option:hover { background: var(--bg-selected); }
.selected-mark { color: var(--success); }
.model-api-id { display: block; margin-top: 3px; color: var(--text-secondary); font-size: 10px; }
.picker-settings { display: block; border-top: 1px solid var(--border); padding-top: 12px; font-size: 12px; }
.sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); }
</style>
