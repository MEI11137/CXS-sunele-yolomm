<script setup lang="ts">
import { ref, watch, onBeforeUnmount } from 'vue'
import { useSessionStore } from '@/stores/session'
import { useWorkspaceStore } from '@/stores/workspace'
import { getAdapter } from '@/services'
import type { TaskSnapshot } from '@/services/types'
import type { AgentEvent } from '@/types'

const session = useSessionStore()
const workspace = useWorkspaceStore()
const snapshot = ref<TaskSnapshot | null>(null)
const events = ref<AgentEvent[]>([])
const error = ref('')
const busy = ref(false)
let timer: ReturnType<typeof setTimeout> | undefined
let generation = 0
let requestVersion = 0
const labels: Record<string, string> = { planning: '规划中', awaiting_plan: '等待确认计划', awaiting_action: '等待确认操作', awaiting_human: '等待人工处理', awaiting_business: '等待业务完成', running: '执行中', completed: '完成', stopped: '已停止', partial_failed: '部分失败', failed: '失败', interrupted: '中断待核实', unknown: '状态未知', awaiting_input: '等待补充信息' }

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
  if (session.currentId && workspace.currentId) void poll(generation)
}, { immediate: true })
onBeforeUnmount(() => { generation++; clearTimeout(timer) })

async function act(kind: 'plan' | 'confirm' | 'reject' | 'stop', id = '', revision = 0) {
  const task = snapshot.value?.task
  if (!task || busy.value) return
  busy.value = true
  try {
    const api = getAdapter()
    if (kind === 'plan') await api.confirmPlan(task.id, task.planVersion)
    if (kind === 'confirm') await api.confirmAction(id, revision)
    if (kind === 'reject') await api.rejectAction(id, '用户拒绝')
    if (kind === 'stop') await api.stopTask(task.id, '用户停止后续操作')
    await refresh()
  } catch (e) { error.value = (e as Error).message }
  finally { busy.value = false }
}
</script>

<template>
  <section v-if="snapshot?.task || events.length || error" class="progress panel" aria-live="polite">
    <p v-if="error" role="alert">{{ error }}</p>
    <template v-if="snapshot?.task">
      <strong>{{ labels[snapshot.task.status] || snapshot.task.status }}</strong>
      <p>{{ snapshot.task.goal }}</p>
      <ol><li v-for="s in snapshot.task.steps" :key="s.id">{{ s.name }} · {{ s.status }}</li></ol>
      <button v-if="snapshot.task.status === 'awaiting_plan'" class="btn btn-sm" :disabled="busy" @click="act('plan')">确认计划 v{{ snapshot.task.planVersion }}</button>
      <button v-if="!['completed','stopped','failed','partial_failed'].includes(snapshot.task.status)" class="btn btn-sm" :disabled="busy" @click="act('stop')">停止后续步骤</button>
      <div v-for="a in snapshot.actions.filter(x => x.status === 'pending')" :key="a.action_id" class="action">
        <strong>{{ a.tool_title }}</strong>
        <p>实际目标项目：{{ a.actual_target_projects.join('、') || snapshot.task.project_id }}</p>
        <p>{{ a.parameters_summary }}</p>
        <pre>{{ JSON.stringify(a.preview, null, 2) }}</pre>
        <button class="btn btn-sm btn-primary" :disabled="busy" @click="act('confirm', a.action_id, a.revision)">确认执行</button>
        <button class="btn btn-sm" :disabled="busy" @click="act('reject', a.action_id)">拒绝</button>
      </div>
      <p v-if="snapshot.task.waiting_for">等待：{{ snapshot.task.waiting_for.kind }} · {{ JSON.stringify(snapshot.task.waiting_for.refs) }}</p>
      <RouterLink v-if="snapshot.task.waiting_for?.pageHint?.startsWith('/review/') || snapshot.task.waiting_for?.pageHint?.startsWith('/datasets?draft=')" :to="snapshot.task.waiting_for.pageHint">打开对应人工处理页面</RouterLink>
    </template>
    <details v-for="e in events.filter(x => x.type.includes('tool') || x.type.includes('error'))" :key="e.event_id" open>
      <summary>{{ e.sequence }} · {{ e.type }}</summary><pre>{{ JSON.stringify(e.payload, null, 2) }}</pre>
    </details>
  </section>
</template>

<style scoped>
.progress { padding: 12px; margin: 8px; font-size: 12px; }
.action { margin: 12px 0; padding: 8px; border: 1px solid var(--border); }
pre { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 260px; overflow: auto; }
button { margin: 4px; }
</style>
