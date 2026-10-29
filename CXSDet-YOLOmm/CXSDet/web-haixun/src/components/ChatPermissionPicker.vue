<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useSessionStore } from '@/stores/session'
import { useWorkspaceStore } from '@/stores/workspace'
import { getAdapter } from '@/services'
import { projectPath } from '@/utils/projectRoutes'

type Mode = 'request' | 'assist' | 'full'
const props = defineProps<{ disabled?: boolean }>()
const auth = useAuthStore()
const session = useSessionStore()
const workspace = useWorkspaceStore()
const router = useRouter()
const route = useRoute()
const modes: Array<{ id: Mode; label: string; desc: string }> = [
  { id: 'request', label: '请求批准', desc: '项目内读取自动执行；文件修改、删除和未知命令请求批准。' },
  { id: 'assist', label: '帮我批准', desc: '检测、AI核查、导入和平台数据流转自动执行；已有文件删改、人工标注确认与未知命令仍需批准。' },
  { id: 'full', label: '完全访问', desc: '平台常规业务及项目内新文件、可识别的新建命令自动执行；已有文件删改和未知操作仍需批准。' },
]
const selected = computed(() => session.current()?.permission_mode || 'request')
const label = computed(() => modes.find(item => item.id === selected.value)?.label || '请求批准')
const open = ref(false)
const pending = ref<Mode | null>(null)
const saving = ref(false)
const notice = ref('')
const picker = ref<HTMLDivElement | null>(null)
const popupStyle = ref<Record<string, string>>({})
watch(() => [workspace.currentId, session.currentId], () => { open.value = false; pending.value = null; notice.value = '' })
async function toggle() {
  open.value = !open.value
  pending.value = null
  await nextTick()
  if (open.value && picker.value) {
    const left = picker.value.getBoundingClientRect().left
    const width = Math.min(370, window.innerWidth - 32)
    popupStyle.value = { width: width + 'px', left: Math.max(16 - left, Math.min(0, window.innerWidth - 16 - left - width)) + 'px' }
  }
}
async function choose(mode: Mode) {
  if (saving.value || props.disabled || mode === selected.value) return
  if (mode !== 'request') { pending.value = mode; return }
  await save(mode)
}
async function save(mode: Mode) {
  if (saving.value || props.disabled || auth.user?.role !== 1) return
  saving.value = true
  const projectId = workspace.currentId
  try {
    if (!session.current()) {
      const chat = await session.createSession(projectId, '新会话')
      if (workspace.currentId !== projectId) return
      if (route.name === 'project-chat') await router.replace(projectPath(projectId, 'chat/' + chat.id))
    }
    const chatId = session.currentId
    const updated = await getAdapter().setSessionPermissionMode(chatId, mode)
    const chat = session.sessions.find(item => item.id === chatId)
    if (chat) chat.permission_mode = updated.permission_mode
    if (session.currentId === chatId) { open.value = false; pending.value = null; notice.value = '' }
  } catch (e) { notice.value = (e as Error).message }
  finally { saving.value = false }
}
</script>

<template>
  <div ref="picker" class="permission-picker" @keydown.esc="open = false">
    <button class="permission-trigger" :disabled="disabled || saving" :aria-expanded="open" aria-label="选择权限模式" @click="toggle"><svg viewBox="0 0 20 20" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.4"><path d="m10 2 6 3v5c0 4-6 8-6 8s-6-4-6-8V5z"/><path d="m7 10 2 2 4-4"/></svg><span>{{ saving ? '保存中…' : label }}</span><span aria-hidden="true">⌄</span></button>
    <template v-if="open">
      <button class="dismiss" aria-label="关闭权限选择" @click="open = false"></button>
      <div class="permission-popover" :style="popupStyle" role="dialog" aria-label="海巡助手权限">
        <strong class="picker-title">海巡助手权限</strong>
        <p class="scope-note">帮我批准 / 完全访问不会对每张检测或AI核查重复弹卡。文件访问限项目授权范围；已有文件删改、人工标注决定及未知命令保留批准。</p>
        <template v-if="auth.user?.role === 1">
          <button v-for="mode in modes" :key="mode.id" class="mode-option" :class="{ selected: selected === mode.id }" :disabled="saving || disabled" @click="choose(mode.id)"><span class="option-heading">{{ mode.label }}<span v-if="selected === mode.id">✓</span></span><span class="option-desc">{{ mode.desc }}</span></button>
          <div v-if="pending" class="enable-confirm"><strong>启用{{ modes.find(item => item.id === pending)?.label }}？</strong><p>{{ modes.find(item => item.id === pending)?.desc }}待确认且符合此规则的文件操作，也会按新模式继续。</p><div class="confirm-buttons"><button class="btn btn-sm btn-primary" :disabled="saving || disabled" @click="save(pending)">启用此模式</button><button class="btn btn-sm" @click="pending = null">取消</button></div></div>
        </template>
        <p v-else class="scope-note">普通账号保留网站业务权限；本机文件/命令授权需使用本机管理员账号。</p>
        <p class="scope-note bottom-note">批量删改先说明整批范围，一次批准当前操作；修改目标后重新确认。自动业务仍检查权限、真实状态和防重，不代表可自行训练、发布或扩大核查范围。</p>
        <p v-if="notice" class="error" role="alert">{{ notice }}</p>
      </div>
    </template>
  </div>
</template>

<style scoped>
.permission-picker { position: relative; min-width: 0; }
.permission-trigger { display: flex; align-items: center; gap: 6px; border: 0; padding: 7px 8px; color: var(--text-secondary); background: transparent; border-radius: 8px; font-size: 11px; cursor: pointer; white-space: nowrap; }
.permission-trigger:hover { color: var(--text); background: var(--bg-selected); }
.permission-trigger:disabled { opacity: .5; cursor: wait; }
.dismiss { position: fixed; inset: 0; z-index: 32; background: transparent; border: 0; cursor: default; }
.permission-popover { position: absolute; bottom: calc(100% + 14px); z-index: 33; max-height: 70vh; overflow: auto; padding: 16px; border: 1px solid var(--border); border-radius: 14px; background: var(--panel); color: var(--text); box-shadow: var(--shadow-md); }
.picker-title { font-size: 14px; }
.scope-note { font-size: 11px; line-height: 1.7; color: var(--text-secondary); margin: 10px 0; }
.mode-option { display: flex; flex-direction: column; width: 100%; padding: 10px; border: 0; border-radius: 8px; background: transparent; color: var(--text); text-align: left; cursor: pointer; gap: 5px; }
.mode-option:hover, .mode-option.selected { background: var(--panel-secondary); }
.option-heading { display: flex; justify-content: space-between; width: 100%; font-size: 12px; font-weight: 550; }
.option-heading span { color: var(--success); }
.option-desc { font-size: 11px; color: var(--text-secondary); line-height: 1.6; }
.enable-confirm { margin-top: 10px; border: 1px solid var(--border-control); border-radius: 8px; padding: 12px; font-size: 12px; }
.enable-confirm p { color: var(--text-secondary); line-height: 1.6; font-size: 11px; }
.confirm-buttons { display: flex; gap: 8px; }
.bottom-note { border-top: 1px solid var(--border); padding-top: 10px; margin-bottom: 0; }
.error { font-size: 12px; color: var(--danger); }
</style>
