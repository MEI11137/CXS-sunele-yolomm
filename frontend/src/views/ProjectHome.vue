<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import DialogShell from '@/components/DialogShell.vue'
import DirectoryBrowser from '@/components/DirectoryBrowser.vue'
import { useWorkspaceStore } from '@/stores/workspace'

const route = useRoute()
const router = useRouter()
const workspace = useWorkspaceStore()

const projectId = computed(() => String(route.params.projectId ?? ''))
const sessionId = computed(() => String(route.params.sessionId ?? ''))
const project = computed(() => (projectId.value ? workspace.projectById(projectId.value) : null))
const sessions = computed(() =>
  projectId.value ? (workspace.sessionsByProject[projectId.value] ?? []) : [],
)
const currentSession = computed(() => sessions.value.find((item) => item.id === sessionId.value) ?? null)

const createOpen = ref(false)
const newProjectName = ref('')
const newProjectDesc = ref('')
const createError = ref('')

const pathInput = ref('')
const browseOpen = ref(false)
const replaceOpen = ref(false)
const pendingPath = ref('')
const bindError = ref('')

const renameOpen = ref(false)
const renameValue = ref('')

const deleteOpen = ref(false)
const sessionDeleteId = ref('')
const sessionDeleteOpen = ref(false)
const editingSessionId = ref('')
const editingTitle = ref('')

const newSessionTitle = ref('')

watch(
  project,
  (value) => {
    if (value && !pathInput.value) pathInput.value = value.root_path ?? ''
  },
  { immediate: true },
)

watch(
  projectId,
  (value) => {
    pathInput.value = ''
    if (value) void workspace.loadSessions(value)
  },
  { immediate: true },
)

async function createProject(): Promise<void> {
  createError.value = ''
  const name = newProjectName.value.trim()
  if (!name) {
    createError.value = '请填写项目名称'
    return
  }
  const created = await workspace.createProject(name, newProjectDesc.value.trim())
  if (!created) {
    createError.value = workspace.error
    return
  }
  createOpen.value = false
  newProjectName.value = ''
  newProjectDesc.value = ''
  await router.push({ name: 'project', params: { projectId: created.id } })
}

async function bind(path: string): Promise<void> {
  bindError.value = ''
  const target = path.trim()
  if (!target) {
    bindError.value = '请填写或选择服务器上的目录'
    return
  }
  if (project.value?.root_path && project.value.root_path !== target) {
    // 替换已有绑定必须显式确认，避免项目被悄悄指向另一个目录。
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
  if (removed) await router.push({ name: 'projects' })
}

async function createSession(): Promise<void> {
  const title = newSessionTitle.value.trim() || '新会话'
  const created = await workspace.createSession(projectId.value, title)
  if (!created) return
  newSessionTitle.value = ''
  await router.push({ name: 'session', params: { projectId: projectId.value, sessionId: created.id } })
}

function openSession(id: string): void {
  void router.push({ name: 'session', params: { projectId: projectId.value, sessionId: id } })
}

function askDeleteSession(id: string): void {
  sessionDeleteId.value = id
  sessionDeleteOpen.value = true
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

async function confirmDeleteSession(): Promise<void> {
  const target = sessionDeleteId.value
  const removed = await workspace.deleteSession(projectId.value, target)
  sessionDeleteOpen.value = false
  if (removed && target === sessionId.value) {
    await router.push({ name: 'project', params: { projectId: projectId.value } })
  }
}

/**
 * 项目详情是**低频**入口：日常操作直接进会话。
 *
 * 打开项目时，如果已经有会话就自动进入最近的会话（点击即进入上下文）；
 * 只有显式 `?details=1`（从“项目详情”按钮进来）或还没有会话时才停留在这里。
 */
const detailsMode = computed(() => route.query.details === '1')

function enterConversation(): void {
  const first = sessions.value[0]
  if (!first) return
  void router.push({
    name: 'session',
    params: { projectId: projectId.value, sessionId: first.id },
  })
}

watch(
  [project, sessions, detailsMode],
  () => {
    if (!projectId.value || detailsMode.value) return
    const first = sessions.value[0]
    if (!first) return
    // 自动进入用 replace：否则“项目页 → 会话页”会在历史里堆一层，
    // 用户点返回又立刻被送回会话页，等于返回不了。
    void router.replace({
      name: 'session',
      params: { projectId: projectId.value, sessionId: first.id },
    })
  },
  { immediate: true, flush: 'post' },
)
</script>

<template>
  <section class="page">
    <!-- 项目列表 -->
    <template v-if="!projectId">
      <header class="page-header">
        <div>
          <h1>项目</h1>
          <p class="subtitle">每个项目独立绑定服务器目录，会话与文件都按项目隔离。</p>
        </div>
        <button class="primary" type="button" @click="createOpen = true">新建项目</button>
      </header>

      <div v-if="workspace.projects.length" class="cards">
        <article v-for="item in workspace.projects" :key="item.id" class="card project-card">
          <h2>{{ item.name }}</h2>
          <p class="body-text">{{ item.description || '（未填写说明）' }}</p>
          <dl class="kv compact">
            <div>
              <dt>目录</dt>
              <dd class="mono">{{ item.root_path || '未绑定' }}</dd>
            </div>
            <div>
              <dt>创建</dt>
              <dd>{{ item.created_at }}</dd>
            </div>
          </dl>
          <div class="card-actions">
            <RouterLink class="button primary" :to="{ name: 'project', params: { projectId: item.id } }">
              打开
            </RouterLink>
          </div>
        </article>
      </div>
      <p v-else-if="!workspace.loading" class="empty-hint">
        还没有项目。新建后先绑定一个服务器目录，后续检测与复核都会在该目录内进行。
      </p>
    </template>

    <!-- 项目不存在 / 无权访问 -->
    <template v-else-if="!project">
      <header class="page-header">
        <div>
          <h1>项目不可用</h1>
          <p class="subtitle">该项目不存在，或不属于当前账号。</p>
        </div>
      </header>
      <RouterLink class="button ghost" :to="{ name: 'projects' }">返回项目列表</RouterLink>
    </template>

    <!-- 项目详情 -->
    <template v-else>
      <header class="page-header">
        <div>
          <h1>{{ project.name }}</h1>
          <p class="subtitle">
            {{ project.description || '未填写说明' }}
            <span v-if="currentSession" class="crumb">/ 会话：{{ currentSession.title }}</span>
          </p>
        </div>
        <div class="header-actions">
          <button v-if="sessions.length" class="primary" type="button" @click="enterConversation">
            进入对话
          </button>
          <button class="ghost" type="button" @click="openRename">改名</button>
          <button class="ghost danger-text" type="button" @click="deleteOpen = true">删除</button>
        </div>
      </header>

      <section class="card">
        <h2>服务器目录绑定</h2>
        <p class="body-text">
          这里选择的是<strong>运行后端的这台机器</strong>上的目录，不是浏览器所在电脑的目录。
          只能绑定管理员<strong>授予你</strong>的根目录内部；授权被收回后本项目会立即无法访问。
          换目录必须点“确认替换”。
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
            {{ browseOpen ? '收起浏览器' : '浏览服务器目录' }}
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

      <section class="card">
        <h2>会话</h2>
        <p class="body-text">
          会话保存真实消息记录。打开会话后，「海巡助手」会在本项目绑定目录内
          列目录、读文件、写入/移动/删除文件，也能查项目与会话；
          写入类动作是否要你确认由权限模式决定。
        </p>
        <div class="inline-form">
          <input v-model="newSessionTitle" placeholder="新会话名称（可留空）" @keydown.enter="createSession" />
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
              <button class="session-title" type="button" @click="openSession(item.id)">
                {{ item.title }}
                <span v-if="item.id === sessionId" class="tag tag-on">当前</span>
              </button>
              <span class="session-meta">{{ item.updated_at }}</span>
              <button class="ghost small" type="button" @click="startRenameSession(item.id, item.title)">
                改名
              </button>
              <button
                class="ghost small danger-text"
                type="button"
                @click="askDeleteSession(item.id)"
              >
                删除
              </button>
            </template>
          </li>
        </ul>
        <p v-else class="empty-hint">还没有会话。新建一个后可在右侧助手区留下消息记录。</p>
      </section>

    </template>
  </section>

  <DialogShell title="新建项目" :open="createOpen" @close="createOpen = false">
    <label class="field">
      <span>项目名称</span>
      <input v-model="newProjectName" placeholder="例如 光伏电站 A 区" />
    </label>
    <label class="field">
      <span>说明（可选）</span>
      <textarea v-model="newProjectDesc" rows="2"></textarea>
    </label>
    <p v-if="createError" class="form-error">{{ createError }}</p>
    <template #footer>
      <button class="ghost" type="button" @click="createOpen = false">取消</button>
      <button class="primary" type="button" :disabled="workspace.loading" @click="createProject">创建</button>
    </template>
  </DialogShell>

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
