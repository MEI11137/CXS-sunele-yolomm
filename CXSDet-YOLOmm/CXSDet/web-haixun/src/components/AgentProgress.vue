<script setup lang="ts">
import { computed, ref, watch, nextTick, onBeforeUnmount } from 'vue'
import { useSessionStore } from '@/stores/session'
import { useWorkspaceStore } from '@/stores/workspace'
import { getAdapter } from '@/services'
import type { TaskSnapshot } from '@/services/types'
import type { AgentEvent, Confirmation } from '@/types'
import { useRouter } from 'vue-router'
import { scopedPage, projectPath } from '@/utils/projectRoutes'
import { useThemeStore } from '@/stores/theme'
import { useGuideStore } from '@/stores/guide'
import { workspaceEffect, closedContext } from '@/utils/workspaceBridge'
import { toolActivities } from '@/utils/toolActivity'
import { dispatchReviewEvent, reviewPage } from '@/utils/reviewBridge'

defineProps<{ approvalTarget?: HTMLElement | null }>()
const emit = defineEmits<{ running: [value: boolean] }>()

const session = useSessionStore()
const workspace = useWorkspaceStore()
const router = useRouter()
const snapshot = ref<TaskSnapshot | null>(null)
const events = ref<AgentEvent[]>([])
const error = ref('')
const actionError = ref('')
const busy = ref(false)
const selectedActionId = ref('')
const pendingActions = computed(() => snapshot.value?.actions.filter(action => action.status === 'pending') || [])
const selectedAction = computed(() => pendingActions.value.find(action => action.action_id === selectedActionId.value) || pendingActions.value[0])
const selectedIndex = computed(() => pendingActions.value.findIndex(action => action.action_id === selectedAction.value?.action_id))
const awaitingPlan = computed(() => snapshot.value?.task?.status === 'awaiting_plan')
watch(pendingActions, actions => {
  if (!actions.some(action => action.action_id === selectedActionId.value)) selectedActionId.value = actions[0]?.action_id || ''
})
function switchAction(offset: number) { selectedActionId.value = pendingActions.value[selectedIndex.value + offset]?.action_id || selectedActionId.value }
let timer: ReturnType<typeof setTimeout> | undefined
let generation = 0
let requestVersion = 0
let activatedAt = Date.now() - 1000
let navigatedSequence = 0
let reviewSequence = 0
let resourcesSequence = 0
let workspaceSequence = 0
const downloads = ref<Array<{ id: string; url: string; filename: string }>>([])
function clearDownloads() { downloads.value.forEach(file => URL.revokeObjectURL(file.url)); downloads.value = [] }

async function leaveDeletedContext(kind: string, id: string) {
  if (kind === 'project') { session.forgetProject(id); workspace.forget(id) }
  else session.forgetSession(id)
  await router.push(kind === 'project' ? '/projects' : projectPath(workspace.currentId))
}

async function applyWorkspaceEvent(event: AgentEvent, sid: string) {
  const effect = workspaceEffect(event, workspace.currentId, sid, activatedAt)
  if (!effect) return false
  const p = effect.payload, api = getAdapter(), pid = workspace.currentId
  if (effect.type === 'workspace.account_changed') {
    if (p.context_closed) { await leaveDeletedContext(String(p.kind), String(p.id)); return true }
    workspace.loaded = false; await workspace.load()
    if (sid !== session.currentId || pid !== workspace.currentId) return true
    if (p.operation === 'restore') await session.revive(String(p.target_project_id), p.kind === 'session' ? String(p.id) : undefined)
    else if (p.operation === 'delete') {
      if (p.kind === 'session') session.forgetSession(String(p.id))
      else session.forgetProject(String(p.id))
    }
    await session.loadProjectHistory(p.kind === 'project' && p.operation === 'delete' ? pid : String(p.target_project_id || pid), true)
  } else if (effect.type === 'workspace.context') {
    const target = String(p.project_id || ''), chat = String(p.session_id || '')
    workspace.loaded = false; await workspace.load()
    if (sid !== session.currentId || pid !== workspace.currentId || !workspace.workspaces.some(w => w.id === target)) return true
    await router.push(projectPath(target, chat ? 'chat/' + encodeURIComponent(chat) : 'chat'))
    return true
  } else if (effect.type === 'workspace.download') {
    const asset = await api.getFileMetadata(String(p.asset_id))
    const url = await api.fileObjectUrl(asset.bucket, asset.object_key)
    if (sid !== session.currentId || pid !== workspace.currentId) { URL.revokeObjectURL(url); return true }
    downloads.value.push({ id: String(p.asset_id), url, filename: String(p.filename || '识别记录.csv') })
  } else if (effect.type === 'workspace.document') {
    await router.push(projectPath(pid, 'manual') + '?document=' + encodeURIComponent(String(p.document_id)))
  } else if (effect.type === 'workspace.ui') {
    if (p.command === 'theme' && ['light','dark'].includes(String(p.value))) useThemeStore().apply(p.value as 'light' | 'dark')
    else if (p.command === 'guide') useGuideStore().replay()
    else if (p.command === 'open_settings') { await router.push('/settings'); return true }
    else if (p.command === 'copy_message') await navigator.clipboard.writeText(String(p.text || ''))
    else window.dispatchEvent(new CustomEvent('haixun:ui', { detail: p }))
  }
  return false
}
const labels: Record<string, string> = { planning: '规划中', awaiting_plan: '等待确认计划', awaiting_action: '等待确认操作', awaiting_human: '等待人工处理', awaiting_business: '等待业务完成', running: '执行中', completed: '完成', stopped: '已停止', partial_failed: '部分失败', failed: '失败', interrupted: '中断待核实', unknown: '状态未知', awaiting_input: '等待补充信息' }
const activity = computed(() => toolActivities(events.value, snapshot.value?.task?.id, snapshot.value?.actions))
const automaticApprovals = computed(() => events.value.filter(event => event.type === 'permission.auto_approved' && event.task_id === snapshot.value?.task?.id).length)
const batchProgress = computed(() => events.value.filter(event => event.type === 'task.progress' && event.task_id === snapshot.value?.task?.id && event.payload.inspection_run_id).slice(-8))
const waitingApproval = computed(() => !!snapshot.value?.actions.some(action => action.status === 'pending'))
const running = computed(() => !waitingApproval.value && ['running', 'planning', 'awaiting_business'].includes(snapshot.value?.task?.status || ''))
const statusLabel = computed(() => waitingApproval.value ? '等待批准' : snapshot.value?.task ? labels[snapshot.value.task.status] || snapshot.value.task.status : '执行过程')
const eventErrors = computed(() => events.value.filter(event => event.type.includes('error') && (!snapshot.value?.task || event.task_id === snapshot.value.task.id)))
const toolStatuses: Record<string,string> = { pending: '等待中', running: '执行中', completed: '已完成', succeeded: '已完成', failed: '失败', error: '失败', cancelled: '已取消' }
watch(running, value => emit('running', value), { immediate: true })

async function refresh(token = generation) {
  if (!session.currentId || !workspace.currentId) return
  const request = ++requestVersion
  try {
    const sid = session.currentId
    const tasks = await getAdapter().listTasks(workspace.currentId)
    const task = tasks.find(t => t.session_id === sid)
    const ev = await getAdapter().listEvents(sid, events.value[events.value.length - 1]?.sequence || 0)
    if (token !== generation || request !== requestVersion) return
    events.value = [...new Map([...events.value, ...ev].map(e => [e.sequence, e])).values()]
    for (const event of ev.filter(item => item.sequence > workspaceSequence && workspaceEffect(item, workspace.currentId, sid, activatedAt))) {
      workspaceSequence = event.sequence
      if (await applyWorkspaceEvent(event, sid)) return
      if (token !== generation || sid !== session.currentId) return
    }
    const navigation = [...ev].reverse().find(event => event.type === 'workspace.navigate' && event.sequence > navigatedSequence && (event.created_at || 0) >= activatedAt)
    if (navigation && navigation.payload.project_id === workspace.currentId) {
      const target = scopedPage(workspace.currentId, String(navigation.payload.path || ''))
      if (target) { navigatedSequence = navigation.sequence; await router.push(target) }
    }
      const changed = ev.filter(event => event.sequence > resourcesSequence && (event.created_at || 0) >= activatedAt &&
        (event.type === 'workspace.resources_changed' || event.type === 'tool.result' &&
        /^(business_(dataset_(import|transform|register|split)|model_(import|publish|unpublish)|draft_(assign|finalize|create)|feedback_|training_start|detection_run|review_ai|document_parse)|workspace_route_reviewed_samples)/.test(String(event.payload.tool || '')) && ['succeeded', 'completed', 'partial', 'partial_failed'].includes(String(event.payload.status))))
      if (changed.length && token === generation && request === requestVersion && sid === session.currentId) {
        resourcesSequence = Math.max(...changed.map(event => event.sequence))
        window.dispatchEvent(new CustomEvent('haixun:resources-changed', { detail: { project_id: workspace.currentId } }))
      }
    for (const event of ev.filter(item => ['review.view', 'review.draft', 'review.changed'].includes(item.type) && item.sequence > reviewSequence && (item.created_at || 0) >= activatedAt)) {
      reviewSequence = event.sequence
      if (event.payload.project_id !== workspace.currentId) continue
      if (['review.view', 'review.draft'].includes(event.type) && reviewPage.value?.record_id !== event.payload.record_id) {
        const target = scopedPage(workspace.currentId, '/review/' + encodeURIComponent(String(event.payload.record_id)))
        if (target) { await router.push(target); await nextTick() }
      }
      if (token !== generation || request !== requestVersion || sid !== session.currentId) return
      dispatchReviewEvent(event.type, event.payload)
    }
    if (task) {
      const next = ['awaiting_human', 'awaiting_business'].includes(task.status) && !session.streaming && !busy.value
        ? await getAdapter().reconcileTask(task.id) : await getAdapter().getTaskSnapshot(task.id)
      if (token !== generation || request !== requestVersion) return
      snapshot.value = next
      if (!session.streaming) await session.loadMessages(sid)
    } else snapshot.value = null
    error.value = ''
  } catch (e) { if (token === generation) error.value = (e as Error).message }
}
async function poll(token: number) {
  await refresh(token)
  if (token === generation) timer = setTimeout(() => poll(token), 4000)
}
watch(() => [session.currentId, workspace.currentId], () => {
  generation++
  clearTimeout(timer)
  snapshot.value = null
  events.value = []
  selectedActionId.value = ''
  error.value = ''
  actionError.value = ''
  busy.value = false
  activatedAt = Date.now() - 1000
  navigatedSequence = 0
  reviewSequence = 0
  resourcesSequence = 0
  workspaceSequence = 0
  clearDownloads()
  if (session.currentId && workspace.currentId) void poll(generation)
}, { immediate: true })
onBeforeUnmount(() => { generation++; clearTimeout(timer); clearDownloads() })

async function act(kind: 'plan' | 'confirm' | 'reject' | 'stop', id = '', revision = 0) {
  const task = snapshot.value?.task
  if (!task || busy.value) return
  const token = generation
  busy.value = true
  actionError.value = ''
  try {
    const api = getAdapter()
    const action = snapshot.value?.actions.find(item => item.action_id === id)
    const page = reviewPage.value
    if (kind === 'confirm' && action?.tool_name === 'workspace_review_record' && page && page.record_id === action.preview.record_id && action.preview.editor_revision != null && page.draft_revision !== action.preview.editor_revision)
      throw new Error('当前标注草稿已变化，请重新告诉助手要修改的内容，旧卡片不能覆盖新草稿。')
    if (kind === 'plan') await api.confirmPlan(task.id, task.planVersion)
    if (kind === 'confirm') {
      const confirmed = await api.confirmAction(id, revision)
      const closed = closedContext(confirmed)
      if (closed) { await leaveDeletedContext(closed.kind, closed.id); return }
    }
    if (kind === 'reject') await api.rejectAction(id, '用户拒绝')
    if (kind === 'stop') await api.stopTask(task.id, '用户停止后续操作')
    if (token === generation) await refresh(token)
  } catch (e) { if (token === generation) actionError.value = (e as Error).message }
  finally { if (token === generation) busy.value = false }
}
function eventTitle(name: string) {
  const raw = name.replace(/^haixun_/, '').replace(/^business_/, '')
  const workspaceNames: Record<string,string> = { workspace_projects:'查询本人项目',workspace_manage_project:'管理项目',workspace_sessions:'查询会话历史',workspace_manage_session:'管理会话',workspace_switch_context:'切换项目或会话',workspace_export_records_csv:'导出本页 CSV',workspace_documents:'读取或打开项目资料',workspace_manuals:'读取任务手册',workspace_manage_manual:'管理个人手册',workspace_ui:'调整当前界面' }
  if (workspaceNames[raw]) return workspaceNames[raw]
  const resourceNames: Record<string, string> = { workspace_add_feedback: '批量加入反馈池', workspace_query_resources: '查询项目业务资源', workspace_manage_resource: '准备资源删除 / 恢复', workspace_draft_samples: '读取草稿样本', draft_assign_splits: '按指定方式划分样本', dataset_split: '划分训练 / 验证 / 测试集', workspace_delete_records: '删除所选识别记录' }
  if (resourceNames[raw]) return resourceNames[raw]
  const batchNames: Record<string, string> = { workspace_start_inspection_loop: '自动巡检闭环', workspace_inspection_state: '查询本轮进度', workspace_route_reviewed_samples: '回流人工确认样本', workspace_discover_inputs: '查找数据集与权重' }
  if (batchNames[raw]) return batchNames[raw]
  const reviewNames: Record<string, string> = { workspace_review_state: '读取复核记录', workspace_review_view: '调整复核视图', workspace_review_record: '按你的指示处理复核记录' }
  if (reviewNames[raw]) return reviewNames[raw]
  const names: Record<string,string> = { workspace_open_page: '打开项目页面', workspace_attach_file: '上传项目文件', workspace_list_resources: '查看项目资源', read: '读取文件', write: '写入文件', edit: '修改文件', bash: '执行命令', glob: '查找文件', grep: '搜索内容', dataset_import: '校验并导入数据集', detection_run: '图片巡检', review_ai: 'AI 核查', wait_for_human: '等待人工处理' }
  return names[raw] || raw.replace(/_/g,' ')
}
function displayBrand(value: string) { return value.startsWith('将指定记录批量加入') ? '批量加入反馈池' : value.startsWith('按用户指定范围单条或批量删除') ? '删除所选识别记录' : value.startsWith('删除或恢复当前项目的数据集') ? '项目资源删除 / 恢复' : value.startsWith('按用户指示编辑复核草稿') ? '复核记录操作' : value.replace(/OpenCode/g, '海巡助手') }
function cardPreview(preview: Record<string, unknown>) {
  return { ...preview, ...(typeof preview.tool_title === 'string' ? { tool_title: displayBrand(preview.tool_title) } : {}) }
}
function targetName(id: string) { return workspace.workspaces.find(item => item.id === id)?.name || id }
function actionSummary(action: Confirmation) {
  if (['workspace_manage_project','workspace_manage_session','workspace_manage_manual'].includes(action.tool_name)) {
    const target = action.preview.target as Record<string, unknown> | undefined
    const operation = ({ create:'创建',update:'修改',rename:'重命名',bind:'绑定本机目录',delete:'移入回收站',restore:'恢复',select:'选择手册' } as Record<string,string>)[String(action.parameters.operation)] || ''
    return `${operation}：${target?.name || target?.title || action.parameters.name || action.parameters.title || target?.id || ''}。${action.preview.impact || ''}`
  }
  if (action.tool_name === 'workspace_add_feedback') return `将所选 ${action.preview.count} 条人工确认记录加入原数据集反馈池。${action.preview.impact || ''}`
  if (action.tool_name === 'workspace_delete_records') return `仅删除所选 ${action.preview.count} 条记录。${action.preview.impact || ''}`
  if (action.tool_name === 'workspace_manage_resource') return `${approvalLabel(action)} · ${action.preview.name} · 资源版本 ${action.preview.revision}。${action.preview.impact || ''}`
  if (action.tool_name === 'workspace_review_record') return `${approvalLabel(action)} · 记录 ${action.preview.title || action.preview.record_id} · 当前版本 ${action.preview.current_version}。${action.preview.impact || ''}`
  return displayBrand(String(action.tool_name === 'opencode_permission' ? action.preview.impact || action.parameters_summary : action.parameters_summary))
}
function approvalLabel(action: Confirmation) {
  if (action.tool_name === 'workspace_add_feedback') return `确认加入这 ${action.preview.count} 条反馈`
  if (action.tool_name === 'workspace_delete_records') return `确认删除这 ${action.preview.count} 条记录`
  if (action.tool_name === 'workspace_manage_resource') return action.preview.operation === 'restore' ? '确认恢复此资源' : '确认移入回收站'
  if (action.tool_name === 'workspace_review_record') return ({ draft: '应用到页面草稿', confirmed: '确认并提交标注', no_defect: '我确认无缺陷', rejected: '确认驳回', delete: '确认删除此记录', restore: '恢复此记录' } as Record<string, string>)[String(action.preview.operation)] || '确认执行'
  if (action.tool_name !== 'opencode_permission') return '确认执行'
  return action.preview.operation_kind === 'delete' ? '批准本次批量删除' : '批准本次操作'
}
function waitingLabel(kind: string) { return ({ human: '请在页面完成复核', business: '等待后台处理结果', input: '请补充必要信息' } as Record<string, string>)[kind] || '等待下一步处理' }
</script>

<template>
  <section v-if="snapshot?.task || events.length || error || actionError" class="agent-progress" aria-label="助手执行过程">
    <p v-if="error || actionError" class="progress-error" role="alert">{{ error || actionError }}</p>
    <p v-for="file in downloads" :key="file.id">CSV 已在项目工作区生成：<a :href="file.url" :download="file.filename">下载 {{ file.filename }}</a></p>
    <details v-if="snapshot?.task || activity.length" open class="execution-details">
      <summary class="execution-summary"><span class="execution-mark" :class="{ spinning: running }" aria-hidden="true">{{ running ? '◌' : waitingApproval ? '·' : '✓' }}</span><strong>{{ statusLabel }}</strong><span v-if="activity.length" class="tool-count">{{ activity.length }} 项工具操作</span><span v-if="automaticApprovals" class="tool-count">已按权限批准 {{ automaticApprovals }} 项</span><span class="summary-chevron" aria-hidden="true">⌄</span></summary>
      <div class="execution-content">
        <p v-for="event in batchProgress" :key="event.event_id">{{ event.payload.message }}</p>
        <ol v-if="snapshot?.task?.steps.length" class="plan-steps"><li v-for="step in snapshot.task.steps" :key="step.id">{{ step.name }}<span>{{ labels[step.status] || toolStatuses[step.status] || step.status }}</span></li></ol>
        <div v-for="tool in activity" :key="tool.id" class="tool-step"><span class="tool-state" :class="{ failed: ['failed','error'].includes(tool.status) }" aria-hidden="true">{{ ['completed','succeeded'].includes(tool.status) ? '✓' : ['failed','error'].includes(tool.status) ? '!' : '·' }}</span><div class="tool-info"><div class="tool-heading"><strong>{{ eventTitle(tool.name) }}</strong><span>{{ toolStatuses[tool.status] || tool.status }}</span></div><details class="raw-details"><summary>输入与执行结果</summary><pre>{{ JSON.stringify(tool.events.map(event => event.payload), null, 2) }}</pre></details></div></div>
        <div v-for="event in eventErrors" :key="event.event_id" class="progress-error">{{ event.payload.message || '工具执行出现错误，请展开详情核实。' }}</div>
        <template v-if="snapshot?.task">
          <button v-if="!['completed','stopped','failed','partial_failed'].includes(snapshot.task.status)" class="btn btn-sm" :disabled="busy" @click="act('stop')">停止后续步骤</button>
        </template>
      </div>
    </details>
    <!-- 复用同一轮询和确认入口；待批准项移至输入框上方，执行历史留在消息列表。 -->
    <Teleport v-if="snapshot?.task && (awaitingPlan || selectedAction)" :to="approvalTarget || 'body'" :disabled="!approvalTarget">
      <section class="approval-dock" aria-label="待批准操作">
        <div class="approval-heading"><strong>{{ awaitingPlan ? '请确认本轮计划' : '需要你的批准' }}</strong><div v-if="!awaitingPlan && pendingActions.length > 1" class="approval-pagination"><button class="btn btn-sm" aria-label="上一项待批准操作" :disabled="busy || selectedIndex <= 0" @click="switchAction(-1)">‹</button><span>{{ selectedIndex + 1 }} / {{ pendingActions.length }}</span><button class="btn btn-sm" aria-label="下一项待批准操作" :disabled="busy || selectedIndex >= pendingActions.length - 1" @click="switchAction(1)">›</button></div></div>
        <div v-if="awaitingPlan" class="confirmation-card">
          <div class="confirmation-body"><strong>{{ snapshot.task.goal }}</strong><p>实际目标项目：{{ targetName(snapshot.task.project_id) }}</p><ol class="plan-steps"><li v-for="step in snapshot.task.steps" :key="step.id">{{ step.name }}</li></ol><p v-if="actionError || error" class="progress-error" role="alert">{{ actionError || error }}</p></div>
          <div class="confirmation-buttons"><button class="btn btn-sm btn-primary" :disabled="busy" @click="act('plan')">{{ busy ? '处理中…' : `确认计划 v${snapshot.task.planVersion}` }}</button><button class="btn btn-sm" :disabled="busy" @click="act('stop')">停止本轮任务</button></div>
        </div>
        <div v-else-if="selectedAction" :key="selectedAction.action_id" class="confirmation-card">
          <div class="confirmation-body"><strong>{{ displayBrand(selectedAction.tool_title) }}</strong><p>实际目标项目：{{ selectedAction.actual_target_projects.map(targetName).join('、') || targetName(snapshot.task.project_id) }}</p><p>{{ actionSummary(selectedAction) }}</p><details><summary>查看操作预览</summary><pre>{{ JSON.stringify(cardPreview(selectedAction.preview), null, 2) }}</pre></details><p v-if="actionError || error" class="progress-error" role="alert">{{ actionError || error }}</p></div>
          <div class="confirmation-buttons"><button class="btn btn-sm btn-primary" :disabled="busy" @click="act('confirm', selectedAction.action_id, selectedAction.revision)">{{ busy ? '处理中…' : approvalLabel(selectedAction) }}</button><button class="btn btn-sm" :disabled="busy" @click="act('reject', selectedAction.action_id)">拒绝</button></div>
        </div>
      </section>
    </Teleport>
    <template v-if="snapshot?.task">
      <div v-if="snapshot.task.waiting_for" class="human-handoff"><p>{{ waitingLabel(snapshot.task.waiting_for.kind) }}</p><RouterLink v-if="snapshot.task.waiting_for.pageHint && scopedPage(workspace.currentId,snapshot.task.waiting_for.pageHint)" :to="scopedPage(workspace.currentId,snapshot.task.waiting_for.pageHint) || ''">打开对应人工处理页面 ↗</RouterLink></div>
    </template>
  </section>
</template>

<style scoped>
.agent-progress { margin: 0 0 4px; min-width: 0; font-size: 12px; color: var(--text-secondary); }
.execution-details { border: 1px solid var(--border); border-radius: 12px; overflow: hidden; }
.execution-summary { display: flex; align-items: center; gap: 8px; padding: 13px 14px; cursor: pointer; list-style: none; }
.execution-summary::-webkit-details-marker { display: none; }
.execution-summary strong { font-weight: 500; color: var(--text); }
.execution-mark { color: var(--success); font-size: 15px; }
.tool-count { font-size: 11px; }
.summary-chevron { margin-left: auto; }
.execution-details[open] .summary-chevron { transform: rotate(180deg); }
.execution-content { padding: 0 14px 10px; }
.tool-step { display: flex; align-items: flex-start; gap: 9px; padding: 10px 0; border-top: 1px solid var(--border); }
.tool-state { color: var(--success); width: 14px; padding-top: 1px; text-align: center; flex-shrink: 0; }
.tool-state.failed, .progress-error { color: var(--danger); }
.tool-info { flex: 1; min-width: 0; }
.tool-heading { display: flex; align-items: center; justify-content: space-between; gap: 10px; line-height: 1.7; }
.tool-heading strong { font-weight: 500; color: var(--text); }
.tool-heading span { font-size: 10px; white-space: nowrap; }
.raw-details { margin-top: 5px; font-size: 11px; }
.raw-details summary { cursor: pointer; opacity: .8; }
pre { font-family: var(--font-mono); font-size: 11px; white-space: pre-wrap; overflow-wrap: anywhere; max-height: 260px; overflow: auto; border: 1px solid var(--border); border-radius: 8px; padding: 10px; background: var(--panel-secondary); }
.plan-steps { padding-left: 20px; line-height: 1.9; }
.plan-steps li span { margin-left: 10px; font-size: 10px; }
.execution-content > button { margin: 8px 8px 0 0; }
.approval-dock { display: flex; flex-direction: column; max-height: min(42dvh, 380px); min-height: 0; border: 1px solid var(--border-control); border-radius: 14px; background: var(--panel-secondary); color: var(--text); font-size: 12px; box-shadow: var(--shadow-md); overflow: hidden; }
.approval-heading { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 10px 14px; border-bottom: 1px solid var(--border); flex-shrink: 0; }
.approval-heading > strong { color: var(--warn-text); font-weight: 500; }
.approval-pagination { display: flex; align-items: center; gap: 8px; white-space: nowrap; }
.confirmation-card { display: flex; flex-direction: column; min-height: 0; line-height: 1.7; }
.confirmation-body { min-height: 0; overflow-y: auto; overscroll-behavior: contain; padding: 10px 14px; overflow-wrap: anywhere; }
.confirmation-body p { margin: 6px 0; }
.confirmation-body details summary { cursor: pointer; color: var(--text-secondary); }
.confirmation-buttons { display: flex; flex-wrap: wrap; gap: 8px; padding: 10px 14px; flex-shrink: 0; border-top: 1px solid var(--border); }
.human-handoff { border-left: 2px solid var(--border-control); margin: 12px 0; padding: 2px 12px; }
.spinning { display: inline-block; animation: spin 1.5s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .spinning { animation: none; } }
</style>
