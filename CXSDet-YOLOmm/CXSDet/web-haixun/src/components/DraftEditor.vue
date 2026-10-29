<script setup lang="ts">
import { ref, watch, computed } from 'vue'
import { getAdapter } from '@/services'
import { useResourceRefresh } from '@/utils/resourceLifecycle'
const props = defineProps<{ draftId: string }>()
const emit = defineEmits<{ finalized: []; close: [] }>()
const samples = ref<Array<Record<string, unknown>>>([])
const selected = ref<string[]>([])
const split = ref('train')
const page = ref(1)
const total = ref(0)
const busy = ref(false)
const error = ref('')
const counts = computed(() => Object.fromEntries(['train','val','test','unassigned','excluded'].map(key => [key, samples.value.filter(sample => sample.split === key).length])))
const allSelected = computed(() => !!samples.value.length && samples.value.every(sample => selected.value.includes(String(sample.id))))
function selectPage(event: Event) { selected.value = (event.target as HTMLInputElement).checked ? samples.value.map(sample => String(sample.id)) : [] }
let generation = 0
async function load() {
  const token = ++generation
  try {
    const data = await getAdapter().listDraftSamples(props.draftId, page.value)
    if (token !== generation) return
    samples.value = (data.samples ?? []) as Array<Record<string, unknown>>
    total.value = Number(data.total ?? samples.value.length)
    selected.value = []
    error.value = ''
  } catch (e) { if (token === generation) error.value = (e as Error).message }
}
watch(() => props.draftId, () => { page.value = 1; void load() }, { immediate: true })
useResourceRefresh(load)
async function mutate(finalize = false) {
  if (busy.value) return
  if (finalize && !window.confirm(`确认将待整理版本 ${props.draftId} 生成正式数据集？后端会核对全部样本划分和配对，成功后才可用于后续模型工作。`)) return
  busy.value = true
  try {
    if (finalize) { await getAdapter().finalizeDraft(props.draftId); emit('finalized') }
    else await getAdapter().assignDraftSamples(props.draftId, selected.value, split.value)
    await load()
  } catch (e) { error.value = (e as Error).message }
  finally { busy.value = false }
}
</script>
<template>
  <section class="panel editor">
    <div class="editor-heading"><h3>设置样本用途</h3><button class="btn btn-sm" :disabled="busy" @click="emit('close')">返回待整理列表</button></div>
    <p>版本 {{ draftId }}。本页：训练 {{ counts.train }}、验证 {{ counts.val }}、测试 {{ counts.test }}、未划分 {{ counts.unassigned }}、排除 {{ counts.excluded }}。勾选样本后选择目标集合并保存。</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <table class="data-table"><thead><tr><th><input type="checkbox" aria-label="全选本页草稿样本" :checked="allSelected" :disabled="busy" @change="selectPage" /></th><th>样本</th><th>当前划分</th><th>配对</th></tr></thead><tbody>
      <tr v-for="s in samples" :key="String(s.id)"><td><input v-model="selected" type="checkbox" :value="String(s.id)" :aria-label="'选择 ' + s.sample_key" /></td><td>{{ s.sample_key }}</td><td>{{ s.split || '未划分' }}</td><td>{{ s.pairing_state }}</td></tr>
    </tbody></table>
    <select v-model="split" class="select" aria-label="目标划分"><option value="train">训练集</option><option value="val">验证集</option><option value="test">测试集</option><option value="excluded">排除，不用于训练</option></select>
    <button class="btn" :disabled="busy || !selected.length" @click="mutate()">保存所选样本划分</button>
    <button class="btn btn-primary" :disabled="busy" @click="mutate(true)">检查并生成数据集</button>
    <button class="btn" :disabled="page <= 1 || busy" @click="page--; load()">上一页</button>
    <span>第 {{ page }} 页 / {{ total }} 个样本</span>
    <button class="btn" :disabled="page * 50 >= total || busy" @click="page++; load()">下一页</button>
  </section>
</template>
<style scoped>.editor { padding: 16px; margin-top: 16px; } .editor-heading { display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; } button { margin: 4px; }</style>
