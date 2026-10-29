// 真实ReviewView与AnnotationCanvas，内存记录/SVG图片；禁止所有网络写入。
import { createApp, h } from 'vue'
import { createPinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'
import ReviewView from '../src/views/ReviewView.vue'
import { useWorkspaceStore } from '../src/stores/workspace'
import { getAdapter } from '../src/services'
import type { DetectionRecordSummary } from '../src/types'
import '../src/assets/base.css'
import '../src/assets/theme.css'

const picture = (thermal: boolean) => 'data:image/svg+xml,' + encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="1000"><defs><pattern id="p" width="200" height="200" patternUnits="userSpaceOnUse"><rect width="198" height="198" fill="${thermal ? '#ffc644' : '#346c9b'}" stroke="white"/><path d="M50 0v200M100 0v200M150 0v200M0 50h200M0 100h200M0 150h200" stroke="${thermal ? '#f67838' : '#97bbd6'}"/></pattern></defs><rect width="1600" height="1000" fill="${thermal ? '#461049' : '#233d2b'}"/><rect x="100" y="200" width="1400" height="600" fill="url(#p)"/><text x="100" y="100" fill="white" font-size="40">${thermal ? 'IR paired image — simulated' : 'RGB image — simulated'}</text></svg>`)
const pinia = createPinia()
const workspace = useWorkspaceStore(pinia)
workspace.currentId = 'fixture'
workspace.workspaces = [{id:'fixture',name:'缩放隔离验收',description:'',requirements:'',requirements_raw:{},created_at:'',updated_at:'',origin:'user'}]
const records = [1,2].map(index => ({id:'fixture-'+index,title:'模拟图片 '+index,description:'',model_id:'fixture-model',model_name:'模拟模型',original_image_url:picture(false),secondary_image_url:picture(true),result_image_url:picture(false),detection_count:0,confidence_threshold:.25,create_time:'',record_status:'completed',ai_review_status:'not_requested',human_review_status:'pending',review_version:0,class_names:['热斑'],detections:[]} as DetectionRecordSummary))
const api = getAdapter()
api.listRecords = async () => ({ items:records,total:records.length,page:1,pageSize:50 })
api.getRecord = async id => records.find(record=>record.id===id) || null
api.submitHumanReview = async () => { throw new Error('隔离验收禁止真实提交') }
window.fetch = async () => { throw new Error('隔离验收禁止网络业务请求') }
const router = createRouter({history:createMemoryHistory(),routes:[{path:'/review',component:ReviewView}]})
await router.push('/review')
await router.isReady()
createApp({render:()=>h('div',[h('p',{style:'padding:8px'},'模拟数据验收：滚轮缩放、拖动、适应窗口、下一项；不提交标注。'),h(ReviewView)])}).use(pinia).use(router).mount('#fixture')
