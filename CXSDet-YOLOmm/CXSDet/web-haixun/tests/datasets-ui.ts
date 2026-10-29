// 实际页面、内存业务，禁止网络；只验证交互，不修改用户数据。
import { createApp, h } from 'vue'
import { createPinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'
import DatasetsView from '../src/views/DatasetsView.vue'
import { useWorkspaceStore } from '../src/stores/workspace'
import { getAdapter } from '../src/services'
import type { DatasetSummary, DetectionRecordSummary } from '../src/types'
import '../src/assets/base.css'
import '../src/assets/theme.css'

const pinia=createPinia(), workspace=useWorkspaceStore(pinia)
workspace.currentId='fixture'
workspace.workspaces=[{id:'fixture',name:'测试项目',description:'',requirements:'',requirements_raw:{},created_at:'',updated_at:'',origin:'user'}]
const dataset=(id:string,name:string,count:number,kind:'formal'|'draft'='formal')=>({id,name,description:'',dataset_mode:'multimodal',primary_modality:'rgb',secondary_modality:'ir',status:1,sample_count:count,upload_time:'2026-10-29T09:00:00',class_names:['热斑'],record_kind:kind,workflow_status:kind==='draft'?'draft':undefined} as DatasetSummary)
let datasets=[dataset('source','Hotspot_MM · 原始数据集',163),dataset('feedback-data','人工反馈数据集',9),dataset('draft','已收集的反馈 · 待整理',3,'draft')]
const record=(id:string,status:string,pool:string|null=null)=>({id,title:id,human_review_status:status,record_status:'completed',feedback_dataset_id:pool,detections:[],class_names:['热斑']} as unknown as DetectionRecordSummary)
const records=[record('confirmed','confirmed'),record('pooled','confirmed','source'),record('pending','pending')]
const samples=Array.from({length:3},(_,i)=>({id:'sample-'+i,sample_key:'image-'+i,split:'unassigned',pairing_state:'paired'}))
const api=getAdapter()
api.listDatasets=async()=>({items:datasets.map(x=>({...x})),total:datasets.length,page:1,pageSize:200})
api.listRecords=async()=>({items:records.map(x=>({...x})),total:records.length,page:1,pageSize:50})
api.addFeedbackBatch=async ids=>{records.filter(r=>ids.includes(r.id)).forEach(r=>r.feedback_dataset_id='source');return {succeeded:ids.length,failed:0,items:ids.map(id=>({record_id:id,status:'success'}))}}
api.createFeedbackDraft=async id=>{if(id!=='source')throw new Error('反馈数据集中还没有已确认样本'); const draft=dataset('new-draft','原始数据集 · 新反馈',2,'draft'); datasets.push(draft);return{id:draft.id}}
let revisionName=''
api.previewFeedbackRevision=async(id,name)=>{if(id!=='source')throw new Error('没有与当前人工标注版本一致的反馈样本'); revisionName=name; return {operation_id:'revision-preview',preview:{sample_count:163,counts:{train:130,val:33,test:0},blocked:false,conflicts:[],source:{revision:{feedback_count:1,replaced_samples:1,added_samples:0},feedback_samples:[{record_id:'fixture-record',action:'replace_annotation',match_basis:'legacy_jpeg95',candidates:[{sample_key:'train/000084',label:'labels/train/000084.txt'}]}]}}}}
api.inspectDataset=async()=>({total_samples:163,scan_complete:true,duplicate_groups:0,duplicates:[],feedback_truncated:false,duplicates_truncated:false,filtered_total:1,samples:[{sample_key:'train/000084',split:'train',image:'images/train/000084.jpg',label:'labels/train/000084.txt',identity:{original_name:'000084.jpg',width:640,height:512,size:80000,sha256:'mock-file-fingerprint',pixel_sha256:'mock-pixel-fingerprint'}}],feedback_matches:[{record_id:'fixture-record',classification:'existing',match_basis:'legacy_jpeg95',candidates:[{sample_key:'train/000084',label:'labels/train/000084.txt'}]}]})
api.executeFeedbackRevision=async(id,operation)=>{if(id!=='source'||operation!=='revision-preview')throw new Error('预览不匹配');datasets.push(dataset('revision','原始数据集 · 修订版',164));return{status:'succeeded',outputs:{name:revisionName,output_dir:'E:/模拟项目/datasets/candidates/revision/dataset'}}}
api.listDraftSamples=async()=>({samples:samples.map(s=>({...s})),total:3})
api.assignDraftSamples=async(_id,ids,split)=>{samples.filter(s=>ids.includes(s.id)).forEach(s=>s.split=split);return{updated:ids.length}}
api.finalizeDraft=async id=>{if(samples.some(s=>s.split==='unassigned')||!samples.some(s=>s.split==='train')||!samples.some(s=>s.split==='val'))throw new Error('请完成划分并保留独立训练和验证样本');datasets=datasets.filter(d=>d.id!==id);datasets.push(dataset('new-formal','反馈新版本 · 已生成',3));return{dataset_id:'new-formal'}}
window.fetch=async()=>{throw new Error('隔离验收禁止真实业务请求')}
const router=createRouter({history:createMemoryHistory(),routes:[{path:'/datasets',component:DatasetsView}]})
await router.push('/datasets'+location.search)
await router.isReady()
createApp({render:()=>h('div',[h('p',{style:'padding:8px;text-align:center'},'数据集双主线 · 模拟验收，所有操作只影响内存'),h(DatasetsView)])}).use(pinia).use(router).mount('#fixture')
