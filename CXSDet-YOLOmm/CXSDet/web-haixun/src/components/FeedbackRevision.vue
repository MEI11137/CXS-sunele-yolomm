<script setup lang="ts">
import { ref, watch } from 'vue'
import { getAdapter } from '@/services'
import type { DatasetSummary } from '@/types'
const props = defineProps<{ dataset: DatasetSummary }>()
const emit = defineEmits<{ completed: [name: string, directory: string] }>()
type Match = {record_id:string;action:string;match_basis:string;candidates:Array<{sample_key:string;label:string}>}
type Plan = { operation_id: string; preview: { sample_count: number; counts: Record<string, number>; blocked: boolean; conflicts: Array<{message: string}>; source: {feedback_samples?:Match[];revision: {feedback_count: number; replaced_samples: number; added_samples: number;ambiguous_samples?:number}} } }
const bases:Record<string,string>={file_sha256:'原文件一致',decoded_pixels:'像素一致',legacy_jpeg95:'旧上传JPEG转换匹配',legacy_jpeg95_pixels:'旧上传JPEG像素匹配',filename_only:'仅同名，需核对',no_content_match:'未匹配原图'}
const name = ref(props.dataset.name + '-复核修订-' + Date.now())
const split = ref('train'), busy = ref(false), error = ref(''), plan = ref<Plan | null>(null)
watch([name, split], () => { plan.value = null; error.value = '' })
async function preview() {
  if (busy.value || !name.value.trim()) return
  busy.value = true; error.value = ''; plan.value = null
  try { plan.value = await getAdapter().previewFeedbackRevision(props.dataset.id, name.value.trim(), split.value) as unknown as Plan }
  catch(e) { error.value = (e as Error).message }
  finally { busy.value = false }
}
async function execute() {
  const prepared = plan.value
  if (!prepared || prepared.preview.blocked || busy.value) return
  const revision = prepared.preview.source.revision
  if (!window.confirm(`确认将「${props.dataset.name}」的 ${revision.feedback_count} 条有效反馈回流为「${name.value}」？\n修正原有 ${revision.replaced_samples} 张，新增 ${revision.added_samples} 张，共 ${prepared.preview.sample_count} 张。原版本保留，不启动训练。`)) return
  busy.value = true; error.value = ''
  try {
    const result = await getAdapter().executeFeedbackRevision(props.dataset.id, prepared.operation_id)
    const outputs = result.outputs as {name?: string; output_dir?: string} | undefined
    if (result.status !== 'succeeded') throw new Error((result.error as {message?: string})?.message || '操作未确认成功，请核对状态后再尝试，不要重复生成')
    emit('completed', outputs?.name || name.value, outputs?.output_dir || '')
  } catch(e) { error.value = (e as Error).message; plan.value = null }
  finally { busy.value = false }
}
</script>
<template>
  <section class="revision-form" aria-label="回原数据集生成修订版">
    <p>原数据集中的图片只覆盖对应标签，图片与划分保留；原文件、像素及旧上传转换匹配均算原图。真正的新图才加入指定集合；同名或来源不唯一时停止并提示核对。</p>
    <label>修订版名称<input v-model="name" class="input" :disabled="busy" /></label>
    <label>新增图片放入<select v-model="split" class="select" :disabled="busy"><option value="train">训练集</option><option value="val">验证集</option><option value="test">测试集</option></select></label>
    <p class="hint">本次处理该来源反馈池中全部有效的人工确认样本；不移动原图，不覆盖原版本，也不自动重新划分或训练。</p>
    <button class="btn" :disabled="busy || !name.trim()" @click="preview">{{ busy ? '正在处理…' : '预览回流结果' }}</button>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <div v-if="plan" class="revision-preview" aria-live="polite">
      <p>有效反馈 {{ plan.preview.source.revision.feedback_count }} 条 · 修正原有 {{ plan.preview.source.revision.replaced_samples }} 张 · 新增 {{ plan.preview.source.revision.added_samples }} 张</p>
      <p>修订版共 {{ plan.preview.sample_count }} 张：训练 {{ plan.preview.counts.train || 0 }}，验证 {{ plan.preview.counts.val || 0 }}，测试 {{ plan.preview.counts.test || 0 }}。</p>
      <p v-if="plan.preview.source.revision.ambiguous_samples">{{ plan.preview.source.revision.ambiguous_samples }} 条来源待核对，暂不能生成。</p>
      <details v-if="plan.preview.source.feedback_samples?.length" open><summary>文件匹配依据</summary><ul><li v-for="m in plan.preview.source.feedback_samples" :key="m.record_id"><strong>{{ m.action==='replace_annotation'?'覆盖原标签':m.action==='add_sample'?'新增图片':'待核对' }}</strong> · {{ bases[m.match_basis]||m.match_basis }}<small>记录：{{ m.record_id }}</small><small v-for="c in m.candidates" :key="c.sample_key">{{ c.sample_key }} → {{ c.label }}</small></li></ul></details>
      <ul v-if="plan.preview.conflicts.length"><li v-for="(item,i) in plan.preview.conflicts" :key="i">{{ item.message }}</li></ul>
      <button class="btn btn-primary" :disabled="busy || plan.preview.blocked" @click="execute">确认回原数据集，生成修订版</button>
    </div>
  </section>
</template>
<style scoped>
.revision-form {margin-top:16px;border-top:1px solid var(--border);padding-top:8px}
label {display:block;margin:12px 0;font-size:13px}input,select{display:block;margin-top:6px;width:100%}
p {font-size:13px;line-height:1.7}.hint{color:var(--text-secondary)}.error{color:var(--danger,#e05252)}
.revision-preview{margin-top:16px;padding:12px;border:1px solid var(--border);border-radius:8px}
small{display:block;overflow-wrap:anywhere;color:var(--text-secondary)}li{margin:8px 0;font-size:13px;line-height:1.7}
</style>
