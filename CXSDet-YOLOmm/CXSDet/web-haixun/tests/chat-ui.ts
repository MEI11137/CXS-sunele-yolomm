// 仅开发验收入口：挂载真实对话组件，所有服务替换为内存数据，不请求业务库或模型。
import { createApp, defineComponent, h, nextTick, ref } from 'vue'
import { createPinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'
import AssistantPanel from '../src/components/AssistantPanel.vue'
import { useWorkspaceStore } from '../src/stores/workspace'
import { useSessionStore } from '../src/stores/session'
import { useAuthStore } from '../src/stores/auth'
import { getAdapter } from '../src/services'
import type { AgentTask, ChatMessage, PersonalConnection, AgentEvent } from '../src/types'
import type { TaskSnapshot } from '../src/services/types'
import '../src/assets/base.css'
import '../src/assets/theme.css'

const pinia = createPinia()
const workspace = useWorkspaceStore(pinia)
const session = useSessionStore(pinia)
if (location.search.includes('permissions=1')) {
  // Isolated display role only: no login, token or persistent authorization.
  useAuthStore(pinia).user = {id:0,username:'fixture',realName:'模拟管理员',role:1}
}
workspace.workspaces = [{ id: 'fixture', name: 'UI 隔离项目', description: '', requirements: '', requirements_raw: {}, created_at: '', updated_at: '', origin: 'user' }]
workspace.currentId = 'fixture'
workspace.loaded = true
const stamp = Date.now()
const initial: ChatMessage[] = Array.from({ length: 14 }, (_, index) => ({ id: 'm' + index, session_id: 'fixture-chat', role: index % 2 ? 'assistant' : 'user', content: index % 2 ? `第 ${index} 条历史回复。\n\n这里展示原图检测、AI 疑点核查与人工确认的实际流程。请对照图片核对类别和标注，确认后再生成反馈数据集。\n\n查看历史时，新回复不应该把阅读位置拉到底部。` : `第 ${index} 条提问：请说明人工复核的操作。`, created_at: stamp + index }))
session.sessions = [{ id: 'fixture-chat', project_id: 'fixture', title: '滚动与模型选择验收（模拟）', created_at: stamp, messages: initial }]
session.currentId = 'fixture-chat'
let connection: PersonalConnection = { configured: true, provider: 'custom', api_url: 'https://fixture.invalid/v1', model: 'fixture-chat' }
const api = getAdapter()
api.getConnection = async () => ({ ...connection })
api.listConnectionModels = async () => ({ models: ['fixture-chat', 'fixture-reasoner'], current: connection.model!, discovery_error: '' })
api.saveConnection = async input => { connection = { ...connection, model: input.model }; return { ...connection } }
let fixtureTask: AgentTask | null = null
let fixtureSnapshot: TaskSnapshot = { task: null, actions: [], note: null }
api.listTasks = async () => fixtureTask ? [structuredClone(fixtureTask)] : []
api.getTaskSnapshot = async () => structuredClone(fixtureSnapshot)
api.reconcileTask = async () => structuredClone(fixtureSnapshot)
const calls = ref<string[]>([])
const failNextApproval = ref(false)
api.confirmAction = async id => {
  if (failNextApproval.value) { failNextApproval.value = false; throw new Error('模拟确认失败，请重试；没有执行任何操作。') }
  const action = fixtureSnapshot.actions.find(item => item.action_id === id)!
  action.status = 'approved'
  calls.value.push('已批准 ' + id)
  if (!fixtureSnapshot.actions.some(item => item.status === 'pending')) fixtureTask!.status = 'completed'
  return structuredClone(action)
}
api.rejectAction = async id => {
  const action = fixtureSnapshot.actions.find(item => item.action_id === id)!
  action.status = 'rejected'
  calls.value.push('已拒绝 ' + id)
  return structuredClone(action)
}
api.confirmPlan = async () => { fixtureTask!.status = 'completed'; calls.value.push('已确认计划'); return structuredClone(fixtureSnapshot) }
api.stopTask = async () => { fixtureTask!.status = 'stopped'; calls.value.push('已停止计划'); return structuredClone(fixtureSnapshot) }
api.setSessionPermissionMode = async (id, mode) => {
  const chat = session.sessions.find(item => item.id === id)!
  chat.permission_mode = mode
  return chat
}
const uiEvents: AgentEvent[] = []
api.listEvents = async (_sid, after = 0) => uiEvents.filter(e => e.sequence > after)
api.listWorkspaces = async () => structuredClone(workspace.workspaces)
api.listSessions = async pid => session.projectSessions(pid).map(s => ({ ...s, messages: [] }))
api.getFileMetadata = async () => ({ bucket: 'fixture', object_key: 'fixture.csv' })
api.fileObjectUrl = async () => URL.createObjectURL(new Blob(['模型,记录\n模拟模型,隔离导出\n'], { type: 'text/csv;charset=utf-8' }))
api.listMessages = async () => session.current()?.messages || []
api.sendMessage = async () => { throw new Error('隔离验收禁止真实发送，请使用“追加回复”按钮。') }
const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/settings', component: { render: () => h('p', '模拟设置入口') } }] })
const Root = defineComponent({ setup() {
  const mode = ref('main')
  const panel = ref<InstanceType<typeof AssistantPanel> | null>(null)
  const dark = ref(false)
  const append = () => session.current()!.messages.push({ id: 'incoming-' + Date.now(), session_id: 'fixture-chat', role: 'assistant', content: '追加的新回复：阅读旧消息的位置应当保持不变。', created_at: Date.now() })
  const oldCard = () => {
    fixtureTask = { id: 'fixture-task', session_id: 'fixture-chat', project_id: 'fixture', status: 'running', goal: '确认卡兼容显示', steps: [], waiting_for: null, business_refs: [], planVersion: 0, updatedAt: Date.now() }
    fixtureSnapshot = { task: fixtureTask, note: null, actions: Array.from({ length: 3 }, (_, index) => ({ action_id: 'fixture-action-' + (index + 1), task_id: fixtureTask!.id, revision: 1, actual_target_projects: ['fixture'], parameters: {}, parameters_summary: 'OpenCode 文件/命令操作；参数：request_id、detail', preview: { tool_title: 'OpenCode 文件/命令操作', targets: Array.from({ length: 50 }, (_, i) => `images/${index}-${i}.png`), impact: '本次批量操作一次批准' }, tool_name: 'opencode_permission', tool_title: `OpenCode 文件/命令操作 ${index + 1}`, status: 'pending', execution_state: 'not_executed' })) }
    session.current()!.messages.push({ id: 'old-card-user-' + Date.now(), session_id: 'fixture-chat', role: 'user', content: '展示历史确认卡的名称与批量批准按钮。', created_at: Date.now() })
  }
  const planCard = () => {
    fixtureTask = { id: 'fixture-plan', session_id: 'fixture-chat', project_id: 'fixture', status: 'awaiting_plan', goal: '检查数据集并准备巡检（模拟计划）', steps: [{ id: 'one', name: '检查数据集和配套权重', status: 'pending' }], waiting_for: null, business_refs: [], planVersion: 1, updatedAt: Date.now() }
    fixtureSnapshot = { task: fixtureTask, actions: [], note: null }
  }
  const switchSession = () => {
    if (!session.sessions.some(item => item.id === 'other-chat')) session.sessions.push({ id: 'other-chat', project_id: 'fixture', title: '无确认卡的另一会话', created_at: stamp, messages: [] })
    session.currentId = session.currentId === 'fixture-chat' ? 'other-chat' : 'fixture-chat'
  }
  const changeMode = async (value: string) => { mode.value = value; await nextTick(); panel.value?.openConversation() }
  const uiEvent = (type: string, payload: Record<string, unknown>) => {
    uiEvents.push({ event_id: 'ui-' + Date.now(), sequence: uiEvents.length + 1, task_id: 'fixture-task', type, created_at: Date.now(), payload: { source_project_id: 'fixture', source_session_id: 'fixture-chat', ...payload } })
  }
  return () => h('div', { style: 'height:100vh;display:flex;flex-direction:column;background:var(--bg)' }, [
    h('div', { style: 'padding:10px;display:flex;gap:10px;flex-wrap:wrap;align-items:center;font-size:12px' }, [h('strong', '隔离验收 · 模拟数据'), h('button', { onClick: append }, '追加回复'), h('button', { onClick: oldCard }, '显示旧确认卡'), h('button', { onClick: planCard }, '显示计划确认'), h('button', { onClick: switchSession }, '切换模拟会话'), h('button', { onClick: () => { failNextApproval.value = true } }, '模拟下次确认失败'), ...['main','sidebar','mobile'].map(value => h('button', { onClick: () => changeMode(value) }, value)), h('button', { onClick: () => { dark.value = !dark.value; document.documentElement.dataset.theme = dark.value ? 'dark' : 'light' } }, '切换主题'), h('span', { role: 'status' }, calls.value.join('；'))]),
    h('div', { style: 'padding:6px;display:flex;gap:10px;font-size:12px' }, [
      h('button', { onClick: () => uiEvent('workspace.ui',{ command:'theme',value:'dark' }) }, '模拟助手切深色'),
      h('button', { onClick: () => uiEvent('workspace.ui',{ command:'scroll',value:'top' }) }, '模拟助手回顶部'),
      h('button', { onClick: () => uiEvent('workspace.download',{ asset_id:'fixture',filename:'隔离导出.csv' }) }, '模拟助手导出CSV'),
      h('button', { onClick: () => uiEvent('workspace.ui',{ command:'open_settings' }) }, '模拟助手打开设置'),
      h('span', { 'data-testid':'current-route' }, router.currentRoute.value.path),
    ]),
    h('div', { style: `min-height:0;flex:1;display:flex;position:relative;transform:translateZ(0);margin-left:${mode.value === 'sidebar' ? 'auto' : '0'};width:${mode.value === 'main' ? '100%' : mode.value === 'mobile' ? '390px' : '336px'}` }, [h(AssistantPanel, { ref: panel, open: true, primary: mode.value === 'main', mobile: mode.value === 'mobile', style: 'width:100%' })]),
  ])
} })
createApp(Root).use(pinia).use(router).mount('#fixture')
