<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { getAdapter } from '@/services'
import type { DatasetSummary } from '@/types'
const props = defineProps<{dataset: DatasetSummary}>()
const emit = defineEmits<{close: []}>()
type Sample = {sample_key:string;split:string;image:string;label:string;identity:{original_name:string;width?:number;height?:number;size:number;sha256:string;pixel_sha256?:string}}
type Match = {record_id:string;classification:string;match_basis:string;candidates:Array<{sample_key:string;label:string}>}
type Audit = {total_samples:number;scan_complete:boolean;duplicate_groups:number;duplicates_truncated:boolean;feedback_truncated:boolean;filtered_total:number;samples:Sample[];feedback_matches:Match[];duplicates:Array<{sample_keys:string[];splits:string[];cross_split:boolean;label_conflict:boolean}>}
const result=ref<Audit|null>(null),page=ref(1),search=ref(''),busy=ref(false),error=ref('')
const bases:Record<string,string>={file_sha256:'原文件完全一致',decoded_pixels:'图像像素一致',legacy_jpeg95:'旧上传JPEG转换一致',legacy_jpeg95_pixels:'旧上传JPEG像素一致',filename_only:'只有同名线索',no_content_match:'未找到对应原图'}
async function load(target=1){
  if(busy.value)return
  busy.value=true;error.value=''
  try{result.value=await getAdapter().inspectDataset(props.dataset.id,target,search.value) as unknown as Audit;page.value=target}
  catch(e){error.value=(e as Error).message}
  finally{busy.value=false}
}
onMounted(()=>load())
</script>
<template>
  <Teleport to="body"><div class="audit-overlay"><section class="audit-dialog" role="dialog" aria-modal="true" aria-label="文件来源与重复检查">
    <header><div><h3>文件来源与重复检查</h3><p>{{ dataset.name }}</p></div><button class="btn" @click="emit('close')">关闭</button></header>
    <p>只读检查，不删除重复图片、不改标注。文件名、时间、大小不能单独证明是同一张图片。</p>
    <div class="search"><input v-model="search" class="input" placeholder="搜索样本路径或文件名" @keyup.enter="load()" /><button class="btn" :disabled="busy" @click="load()">{{ busy?'正在检查…':'检查 / 刷新' }}</button></div>
    <p v-if="error" role="alert">{{ error }}</p>
    <template v-if="result">
      <p>共 {{ result.total_samples }} 张 · {{ result.duplicate_groups }} 组重复候选<span v-if="!result.scan_complete"> · 扫描未完整，不能视为全部结果</span></p>
      <h4>复核反馈对应的原图</h4><p v-if="!result.feedback_matches.length">暂无有效确认反馈。</p>
      <ul><li v-for="m in result.feedback_matches" :key="m.record_id">记录 {{ m.record_id }}：<strong>{{ m.classification==='existing'?'替换原标注':m.classification==='new'?'新增图片':'待核对，不自动写入' }}</strong> · {{ bases[m.match_basis]||m.match_basis }}<div v-for="c in m.candidates" :key="c.sample_key">{{ c.sample_key }} → {{ c.label }}</div></li></ul>
      <p v-if="result.feedback_truncated">仅展示前100条反馈，请让助手指定record_id继续核对。</p>
      <h4>数据集重复情况</h4><p v-if="!result.duplicate_groups">未发现本次文件、像素及旧JPEG转换匹配的重复项；不代表排除了所有相似或裁剪图片。</p>
      <ul><li v-for="(group,i) in result.duplicates" :key="i">{{ group.sample_keys.join('、') }}<strong v-if="group.cross_split"> · 跨集合重复</strong><strong v-if="group.label_conflict"> · 标注不一致</strong></li></ul>
      <p v-if="result.duplicates_truncated">仅展示前100组重复候选，不能据此批量删除全部。</p>
      <h4>文件信息</h4><div class="table-wrap"><table><thead><tr><th>样本 / 标签</th><th>集合</th><th>属性与指纹</th></tr></thead><tbody><tr v-for="s in result.samples" :key="s.sample_key"><td>{{ s.image }}<small>{{ s.label }}</small></td><td>{{ s.split }}</td><td>{{ s.identity.width }} × {{ s.identity.height }} · {{ s.identity.size }} B<details><summary>文件指纹</summary><small>SHA256：{{ s.identity.sha256 }}</small><small>像素：{{ s.identity.pixel_sha256||'无法解码' }}</small></details></td></tr></tbody></table></div>
      <footer><button class="btn" :disabled="busy||page<=1" @click="load(page-1)">上一页</button><span>第 {{ page }} 页 · 筛选共 {{ result.filtered_total }} 张</span><button class="btn" :disabled="busy||page*50>=result.filtered_total" @click="load(page+1)">下一页</button></footer>
    </template>
  </section></div></Teleport>
</template>
<style scoped>
.audit-overlay{position:fixed;inset:0;z-index:910;background:#0007;display:flex;align-items:center;justify-content:center;padding:24px}.audit-dialog{width:min(960px,100%);max-height:88vh;overflow:auto;background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:22px;color:var(--text)}header,footer,.search{display:flex;align-items:center;gap:12px}header{justify-content:space-between}h3{margin:0}p,li,td,th{font-size:13px;line-height:1.7}li{margin:8px 0;overflow-wrap:anywhere}h4{margin-bottom:8px}table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid var(--border);padding:10px;text-align:left}small{display:block;overflow-wrap:anywhere;color:var(--text-secondary)}.table-wrap{overflow:auto}footer{justify-content:flex-end;margin-top:16px}
</style>
