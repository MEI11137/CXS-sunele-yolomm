<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { request } from '@/services/http'
import type { DatasetSummary } from '@/types'
const props = defineProps<{ dataset: DatasetSummary }>()
const emit = defineEmits<{ close: []; completed: [name: string] }>()
const name = ref(props.dataset.name + '-重新划分-' + Date.now())
const train = ref(80), val = ref(10), test = ref(10), busy = ref(false), error = ref('')
type Plan = { operation_id: string; preview: { counts: Record<string, number>; sample_count: number; blocked: boolean; conflicts: Array<{ message: string }>; warnings: unknown[]; impact: string } }
const plan = ref<Plan | null>(null)
const valid = computed(() => !!name.value.trim() && [train.value,val.value,test.value].every(Number.isFinite) && train.value > 0 && val.value > 0 && test.value >= 0 && Math.abs(train.value + val.value + test.value - 100) < .001)
watch([name, train, val, test], () => { plan.value = null; error.value = '' })
const path = '/api/dataset/' + encodeURIComponent(props.dataset.id) + '/split'
async function preview() {
  if (busy.value || !valid.value) return
  busy.value = true; error.value = ''
  try { plan.value = await request<Plan>(path + '/preview', { method: 'POST', body: { name: name.value.trim(), split_policy: { train: train.value / 100, val: val.value / 100, test: test.value / 100 } } }) }
  catch(e) { error.value = (e as Error).message }
  finally { busy.value = false }
}
async function execute() {
  const prepared = plan.value
  if (!prepared || prepared.preview.blocked || busy.value || !window.confirm(`确认将「${props.dataset.name}」重新划分为「${name.value}」？\n训练 ${prepared.preview.counts.train || 0}、验证 ${prepared.preview.counts.val || 0}、测试 ${prepared.preview.counts.test || 0} 张。\n原数据集保留，不启动训练。`)) return
  busy.value = true; error.value = ''
  try {
    const result = await request<{ status: string; outputs?: { name: string }; error?: { message: string } }>(path, { method: 'POST', body: { operation_id: prepared.operation_id } })
    if (result.status !== 'succeeded') throw new Error(result.error?.message || '划分未完成，请核对实际结果，勿重复提交')
    emit('completed', result.outputs?.name || name.value)
  } catch(e) { error.value = (e as Error).message; plan.value = null }
  finally { busy.value = false }
}
</script>
<template>
  <Teleport to="body"><div class="overlay"><section class="dialog split-dialog" role="dialog" aria-modal="true" aria-label="划分训练验证测试集">
    <header class="dialog-header"><h3>划分训练 / 验证 / 测试集</h3><button class="btn" :disabled="busy" @click="emit('close')">关闭</button></header>
    <div class="dialog-body">
      <p>来源：{{ dataset.name }}。生成一个新版本，原数据集和已有模型保留；图片、配套模态和标签一起划分。</p>
      <label>新数据集名称<input v-model="name" class="input" :disabled="busy" /></label>
      <div class="ratio-inputs"><label>训练集 %<input v-model.number="train" class="input" type="number" min="1" max="100" :disabled="busy" /></label><label>验证集 %<input v-model.number="val" class="input" type="number" min="1" max="100" :disabled="busy" /></label><label>测试集 %<input v-model.number="test" class="input" type="number" min="0" max="100" :disabled="busy" /></label></div>
      <p>合计 {{ train + val + test }}%，须为100%；训练集和验证集比例须大于0。按现有工具的固定顺序分配，整数张数以预览为准；相同图片跨集合等冲突会阻止生成。</p>
      <button class="btn" :disabled="busy || !valid" @click="preview">{{ busy ? '处理中…' : '预览实际划分数量' }}</button>
      <p v-if="error" role="alert">{{ error }}</p>
      <div v-if="plan" class="split-preview"><p>总样本 {{ plan.preview.sample_count }}；训练 {{ plan.preview.counts.train || 0 }}；验证 {{ plan.preview.counts.val || 0 }}；测试 {{ plan.preview.counts.test || 0 }}。</p><ul><li v-for="(conflict,i) in plan.preview.conflicts" :key="i">{{ conflict.message }}</li></ul><p>{{ plan.preview.impact }}</p><button class="btn btn-primary" :disabled="busy || plan.preview.blocked" @click="execute">确认生成新数据集</button></div>
    </div>
  </section></div></Teleport>
</template>
<style scoped>.split-dialog{width:min(700px,calc(100vw - 32px));max-height:85vh;overflow:auto}.dialog-body{padding:18px}.dialog-body p{font-size:13px;line-height:1.7}.dialog-body label{display:block;font-size:13px;margin:10px 0}.ratio-inputs{display:flex;gap:12px}.ratio-inputs label{flex:1;min-width:0}.split-preview{border-top:1px solid var(--border);margin-top:16px;padding-top:12px}</style>
