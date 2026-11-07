<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import DialogShell from '@/components/DialogShell.vue'
import DirectoryBrowser from '@/components/DirectoryBrowser.vue'
import { useWorkspaceStore } from '@/stores/workspace'

/**
 * 项目详情抽屉：目录绑定、项目要求、会话管理都收在这里。
 *
 * 为什么不是整页：这些是**低频**动作，整页表单会把主对话挤走。
 * v1.6 也是把目录设置放进弹层；这里保留全部手动入口，只是不再占主位。
 */
const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (event: 'close'): void }>()

const route = useRoute()
const router = useRouter()
const workspace = useWorkspaceStore()

const projectId = computed(() => String(route.params.projectId ?? ''))
const project = computed(() => (projectId.value ? workspace.projectById(projectId.value) : null))
const sessions = computed(() =>
  projectId.value ? (workspace.sessionsByProject[projectId.value] ?? []) : [],
)

const pathInput = ref('')
const browseOpen = ref(false)
const replaceOpen = ref(false)
const pendingPath = ref('')
const bindError = ref('')

const requirementsDraft = ref('')
const requirementsNotice = ref('')
const requirementsError = ref('')

const renameOpen = ref(false)
const renameValue = ref('')
const deleteOpen = ref(false)

const sessionDeleteId = ref('')
const sessionDeleteOpen = ref(false)
const editingSessionId = ref('')
const editingTitle = ref('')
const newSessionTitle = ref('')

watch(
  () => [props.open, project.value?.id],
  () => {
    if (!props.open) return
    pathInput.value = project.value?.root_path ?? ''
    requirementsDraft.value = project.value?.requirements ?? ''
    requirementsNotice.value = ''
    requirementsError.value = ''
    bindError.value = ''
    browseOpen.value = false
  },
  { immediate: true },
)

watch(projectId, (id) => {
  if (id) void workspace.loadSessions(id)
})

async function bind(path: string): Promise<void> {
  bindError.value = ''
  const target = path.trim()
  if (!target) {
    bindError.value = '请填写或选择服务器上的目录'
    return
  }
  if (project.value?.root_path && project.value.root_path !== target) {
    pendingPath.value = target
    replaceOpen.value = true
    return
  }
  const updated = await workspace.bindRoot(projectId.value, target, false)
  if (!updated) {
    bindError.value = workspace.error
    return
  }
  pathInput.value = updated.root_path ?? ''
  browseOpen.value = false
}

async function confirmReplace(): Promise<void> {
  const updated = await workspace.bindRoot(projectId.value, pendingPath.value, true)
  replaceOpen.value = false
  if (!updated) {
    bindError.value = workspace.error
    return
  }
  pathInput.value = updated.root_path ?? ''
}

async function unbind(): Promise<void> {
  const updated = await workspace.unbindRoot(projectId.value)
  if (!updated) {
    bindError.value = workspace.error
    return
  }
  pathInput.value = ''
}

async function saveRequirements(): Promise<void> {
  if (!project.value) return
  requirementsNotice.value = ''
  requirementsError.value = ''
  const saved = await workspace.saveRequirements(project.value.id, requirementsDraft.value)
  if (saved) {
    requirementsDraft.value = saved.requirements
    requirementsNotice.value = '项目要求已保存，下一轮对话生效'
  } else {
    requirementsError.value = workspace.error || '保存失败'
  }
}

function openRename(): void {
  renameValue.value = project.value?.name ?? ''
  renameOpen.value = true
}

async function submitRename(): Promise<void> {
  const name = renameValue.value.trim()
  if (!name) return
  const updated = await workspace.renameProject(projectId.value, name)
  if (updated) renameOpen.value = false
}

async function confirmDeleteProject(): Promise<void> {
  const removed = await workspace.deleteProject(projectId.value)
  deleteOpen.value = false
  if (removed) {
    emit('close')
    await router.push({ name: 'projects' })
  }
}

async function createSession(): Promise<void> {
  const title = newSessionTitle.value.trim() || '新会话'
  const created = await workspace.createSession(projectId.value, title)
  if (!created) return
  newSessionTitle.value = ''
  emit('close')
  await router.push({ name: 'session', params: { projectId: projectId.value, sessionId: created.id } })
}

function startRenameSession(id: string, title: string): void {
  editingSessionId.value = id
  editingTitle.value = title
}

async function submitRenameSession(): Promise<void> {
  const title = editingTitle.value.trim()
  if (!title) return
  const updated = await workspace.renameSession(projectId.value, editingSessionId.value, title)
  if (updated) editingSessionId.value = ''
}

function askDeleteSession(id: string): void {
  sessionDeleteId.value = id
  sessionDeleteOpen.value = true
}

async function confirmDeleteSession(): Promise<void> {
  const target = sessionDeleteId.value
  const removed = await workspace.deleteSession(projectId.value, target)
  sessionDeleteOpen.value = false
  if (removed && String(route.params.sessionId ?? '') === target) {
    await router.push({ name: 'project', params: { projectId: projectId.value } })
  }
}
</script>

<template>
  <Teleport to="body">
    <div v-if="open && project" class="drawer-backdrop" @click.self="emit('close')">
      <aside class="drawer" role="dialog" aria-modal="true" aria-label="项目详情">
        <header class="drawer-header">
          <h2 :title="project.name">{{ project.name }}</h2>
          <div class="header-actions">
            <button class="ghost small" type="button" @click="openRename">改名</button>
            <button class="link" type="button" @click="emit('close')">关闭</button>
          </div>
        </header>

        <div class="drawer-body">
          <section class="drawer-section">
            <h3>服务器目录</h3>
            <p class="hint">
              选择的是<strong>运行后端这台机器</strong>上的目录，不是浏览器所在电脑的目录；
              只能绑定管理员授予你的根目录内部。
            </p>
            <div v-if="project.root_path" class="binding">
              <span class="tag tag-on">已绑定</span>
              <code class="mono">{{ project.root_path }}</code>
            </div>
            <p v-else class="tag tag-off inline">尚未绑定</p>

            <label class="field">
              <span>目录路径（服务器本地绝对路径）</span>
              <input v-model="pathInput" placeholder="例如 D:\巡检数据\2026-11" spellcheck="false" />
            </label>
            <p v-if="bindError" class="form-error" role="alert">{{ bindError }}</p>
            <div class="form-actions">
              <button class="primary" type="button" :disabled="workspace.loading" @click="bind(pathInput)">
                {{ project.root_path ? '更换绑定目录' : '绑定此目录' }}
              </button>
              <button class="ghost" type="button" @click="browseOpen = !browseOpen">
                {{ browseOpen ? '收起浏览器' : '浏览目录' }}
              </button>
              <button v-if="project.root_path" class="ghost danger-text" type="button" @click="unbind">
                解除绑定
              </button>
            </div>
            <DirectoryBrowser
              v-if="browseOpen"
              :start-path="pathInput || undefined"
              @select="
                (path) => {
                  pathInput = path
                  bind(path)
                }
              "
              @close="browseOpen = false"
            />
          </section>

          <section class="drawer-section">
            <h3>项目要求</h3>
            <p class="hint">
              写清楚这个项目先看什么、术语怎么叫、结论怎么写。它只影响回答侧重点，
              <strong>不能</strong>改变工具清单、目录范围、授权或确认规则。
            </p>
            <label class="field">
              <span>要求正文（最多 2000 字，留空表示没有特别要求）</span>
              <textarea
                v-model="requirementsDraft"
                rows="5"
                placeholder="例如：优先核查前缘侵蚀与表面裂纹；结论按“缺陷类型 / 位置 / 建议”三段写。"
              />
            </label>
            <p v-if="requirementsError" class="form-error" role="alert">{{ requirementsError }}</p>
            <p v-if="requirementsNotice" class="banner banner-ok">{{ requirementsNotice }}</p>
            <div class="form-actions">
              <button class="primary" type="button" :disabled="workspace.loading" @click="saveRequirements">
                保存要求
              </button>
              <span class="hint">保存后下一轮对话生效。</span>
            </div>
          </section>

          <section class="drawer-section">
            <h3>会话</h3>
            <div class="inline-form">
              <input
                v-model="newSessionTitle"
                placeholder="新会话名称（可留空）"
                @keydown.enter="createSession"
              />
              <button class="primary" type="button" :disabled="workspace.loading" @click="createSession">
                新建会话
              </button>
            </div>

            <ul v-if="sessions.length" class="session-table">
              <li v-for="item in sessions" :key="item.id" class="session-row">
                <template v-if="editingSessionId === item.id">
                  <input v-model="editingTitle" class="session-rename" @keydown.enter="submitRenameSession" />
                  <button class="primary small" type="button" @click="submitRenameSession">保存</button>
                  <button class="ghost small" type="button" @click="editingSessionId = ''">取消</button>
                </template>
                <template v-else>
                  <span class="session-title">{{ item.title }}</span>
                  <span class="session-meta">{{ item.updated_at }}</span>
                  <button class="ghost small" type="button" @click="startRenameSession(item.id, item.title)">
                    改名
                  </button>
                  <button class="ghost small danger-text" type="button" @click="askDeleteSession(item.id)">
                    删除
                  </button>
                </template>
              </li>
            </ul>
            <p v-else class="hint">还没有会话。回到主对话直接说需求，发送时会自动建一条。</p>
          </section>

          <section class="drawer-section">
            <h3>项目操作</h3>
            <div class="form-actions">
              <button class="ghost danger-text" type="button" @click="deleteOpen = true">移入回收站</button>
            </div>
            <p class="hint">移入回收站后会话暂时不可访问，可在回收站恢复。</p>
          </section>
        </div>
      </aside>
    </div>
  </Teleport>

  <DialogShell title="替换项目目录" :open="replaceOpen" @close="replaceOpen = false">
    <p class="body-text">该项目已绑定其它目录。确认把项目改指向下面这个目录？</p>
    <pre class="code-block">{{ pendingPath }}</pre>
    <p class="hint">原绑定不会被保留；如需回退要重新绑定并再次确认。</p>
    <template #footer>
      <button class="ghost" type="button" @click="replaceOpen = false">取消</button>
      <button class="primary" type="button" @click="confirmReplace">确认替换</button>
    </template>
  </DialogShell>

  <DialogShell title="重命名项目" :open="renameOpen" @close="renameOpen = false">
    <label class="field">
      <span>新名称</span>
      <input v-model="renameValue" @keydown.enter="submitRename" />
    </label>
    <template #footer>
      <button class="ghost" type="button" @click="renameOpen = false">取消</button>
      <button class="primary" type="button" @click="submitRename">保存</button>
    </template>
  </DialogShell>

  <DialogShell title="删除项目" :open="deleteOpen" @close="deleteOpen = false">
    <p class="body-text">
      项目“{{ project?.name }}”会移入回收站，其会话暂时不可访问；可在回收站恢复。
    </p>
    <template #footer>
      <button class="ghost" type="button" @click="deleteOpen = false">取消</button>
      <button class="primary danger" type="button" @click="confirmDeleteProject">移入回收站</button>
    </template>
  </DialogShell>

  <DialogShell title="删除会话" :open="sessionDeleteOpen" @close="sessionDeleteOpen = false">
    <p class="body-text">该会话会移入回收站，消息记录保留，可在回收站恢复。</p>
    <template #footer>
      <button class="ghost" type="button" @click="sessionDeleteOpen = false">取消</button>
      <button class="primary danger" type="button" @click="confirmDeleteSession">移入回收站</button>
    </template>
  </DialogShell>
</template>
