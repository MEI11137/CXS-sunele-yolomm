<script setup lang="ts">
import { ref, watch } from 'vue'
import { getAdapter } from '@/services'
const props = defineProps<{ draftId: string }>()
const emit = defineEmits<{ finalized: [] }>()
const samples = ref<Array<Record<string, unknown>>>([])
const selected = ref<string[]>([])
const split = ref('train')
const page = ref(1)
const total = ref(0)
const busy = ref(false)
const error = ref('')
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
async function mutate(finalize = false) {
  if (busy.value) return
  if (finalize && !window.confirm(`确认将草稿 ${props.draftId} 定稿？后端会核对全部样本划分和配对，成功后才可训练。`)) return
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
    <h3>人工划分 · {{ draftId }}</h3>
    <p v-if="error" role="alert">{{ error }}</p>
    <table class="data-table"><thead><tr><th>选择</th><th>样本</th><th>当前划分</th><th>配对</th></tr></thead><tbody>
      <tr v-for="s in samples" :key="String(s.id)"><td><input v-model="selected" type="checkbox" :value="String(s.id)" :aria-label="'选择 ' + s.sample_key" /></td><td>{{ s.sample_key }}</td><td>{{ s.split || '未划分' }}</td><td>{{ s.pairing_state }}</td></tr>
    </tbody></table>
    <select v-model="split" class="select" aria-label="目标划分"><option value="train">训练集</option><option value="val">验证集</option><option value="test">测试集</option></select>
    <button class="btn" :disabled="busy || !selected.length" @click="mutate()">保存所选样本划分</button>
    <button class="btn" :disabled="busy" @click="mutate(true)">检查并定稿</button>
    <button class="btn" :disabled="page <= 1 || busy" @click="page--; load()">上一页</button>
    <span>第 {{ page }} 页 / {{ total }} 个样本</span>
    <button class="btn" :disabled="page * 50 >= total || busy" @click="page++; load()">下一页</button>
  </section>
</template>
<style scoped>.editor { padding: 16px; margin-top: 16px; } button { margin: 4px; }</style>
