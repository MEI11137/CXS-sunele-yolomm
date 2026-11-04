<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { errorMessage, http } from '@/api/http'
import DialogShell from '@/components/DialogShell.vue'
import { useAuthStore } from '@/stores/auth'
import type { AllowedRoot, AuditEntry, User } from '@/types'

const auth = useAuthStore()
const users = ref<User[]>([])
const roots = ref<AllowedRoot[]>([])
const audit = ref<AuditEntry[]>([])
const error = ref('')
const notice = ref('')
const loading = ref(false)

const createOpen = ref(false)
const newUsername = ref('')
const newDisplayName = ref('')
const newPassword = ref('')

const rootPath = ref('')
const rootLabel = ref('')
/** root.id → 待授予的用户名 */
const grantPick = reactive<Record<number, string>>({})

const resetOpen = ref(false)
const resetTarget = ref<User | null>(null)
const resetPassword = ref('')

const isAdmin = computed(() => auth.isAdmin)

async function load(): Promise<void> {
  if (!isAdmin.value) return
  loading.value = true
  error.value = ''
  try {
    const [userData, rootData, auditData] = await Promise.all([
      http.get<{ users: User[] }>('/admin/users'),
      http.get<{ roots: AllowedRoot[] }>('/admin/roots'),
      http.get<{ entries: AuditEntry[] }>('/admin/audit?limit=30'),
    ])
    users.value = userData.users
    roots.value = rootData.roots
    audit.value = auditData.entries
  } catch (failure) {
    error.value = errorMessage(failure)
  } finally {
    loading.value = false
  }
}

async function createUser(): Promise<void> {
  error.value = ''
  try {
    await http.post('/admin/users', {
      username: newUsername.value.trim(),
      display_name: newDisplayName.value.trim(),
      password: newPassword.value,
    })
    createOpen.value = false
    newUsername.value = ''
    newDisplayName.value = ''
    newPassword.value = ''
    notice.value = '已创建普通账号'
    await load()
  } catch (failure) {
    error.value = errorMessage(failure)
  }
}

async function toggleStatus(user: User): Promise<void> {
  error.value = ''
  try {
    await http.put(`/admin/users/${user.id}/status`, {
      status: user.status === 'active' ? 'disabled' : 'active',
    })
    await load()
  } catch (failure) {
    error.value = errorMessage(failure)
  }
}

async function addRoot(): Promise<void> {
  error.value = ''
  try {
    await http.post('/admin/roots', { path: rootPath.value.trim(), label: rootLabel.value.trim() })
    rootPath.value = ''
    rootLabel.value = ''
    notice.value = '已添加授权根目录；默认不共享，请为需要的账号授予'
    await load()
  } catch (failure) {
    error.value = errorMessage(failure)
  }
}

async function grantRoot(root: AllowedRoot): Promise<void> {
  const username = grantPick[root.id]
  if (!username) return
  error.value = ''
  try {
    await http.post(`/admin/roots/${root.id}/grants`, { username })
    grantPick[root.id] = ''
    notice.value = `已把该目录授予 ${username}`
    await load()
  } catch (failure) {
    error.value = errorMessage(failure)
  }
}

async function revokeRoot(root: AllowedRoot, userId: number): Promise<void> {
  error.value = ''
  try {
    await http.del(`/admin/roots/${root.id}/grants/${userId}`)
    notice.value = '已收回该账号的目录授权，其项目访问立即被拒绝'
    await load()
  } catch (failure) {
    error.value = errorMessage(failure)
  }
}

async function removeRoot(root: AllowedRoot): Promise<void> {
  error.value = ''
  try {
    await http.del(`/admin/roots/${root.id}`)
    notice.value = '已移除授权'
    await load()
  } catch (failure) {
    error.value = errorMessage(failure)
  }
}

function openReset(user: User): void {
  resetTarget.value = user
  resetPassword.value = ''
  resetOpen.value = true
}

async function submitReset(): Promise<void> {
  if (!resetTarget.value) return
  error.value = ''
  try {
    await http.post(`/admin/users/${resetTarget.value.id}/reset-password`, {
      new_password: resetPassword.value,
    })
    resetOpen.value = false
    notice.value = '密码已重置，该账号的登录态已全部失效'
  } catch (failure) {
    error.value = errorMessage(failure)
  }
}

onMounted(() => {
  void load()
})
</script>

<template>
  <section class="page">
    <header class="page-header">
      <div>
        <h1>本机管理</h1>
        <p class="subtitle">仅管理员可见。授权根目录决定普通用户能绑定哪些服务器目录。</p>
      </div>
      <button class="ghost" type="button" :disabled="loading" @click="load">刷新</button>
    </header>

    <p v-if="!isAdmin" class="banner banner-warn">当前账号没有管理员权限。</p>
    <p v-if="error" class="banner banner-error">{{ error }}</p>
    <p v-if="notice" class="banner banner-ok">{{ notice }}</p>

    <template v-if="isAdmin">
      <section class="card">
        <div class="card-head">
          <h2>账号</h2>
          <button class="primary small" type="button" @click="createOpen = true">新建普通账号</button>
        </div>
        <p class="body-text">管理员账号只能在本机用 CLI 创建；这里只能创建普通用户。</p>
        <table class="table">
          <thead>
            <tr>
              <th>用户名</th>
              <th>显示名</th>
              <th>角色</th>
              <th>状态</th>
              <th>创建时间</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in users" :key="item.id">
              <td>{{ item.username }}</td>
              <td>{{ item.display_name }}</td>
              <td>{{ item.role === 'admin' ? '管理员' : '普通用户' }}</td>
              <td>
                <span class="tag" :class="item.status === 'active' ? 'tag-on' : 'tag-off'">
                  {{ item.status === 'active' ? '启用' : '已停用' }}
                </span>
              </td>
              <td class="mono">{{ item.created_at }}</td>
              <td class="row-actions">
                <button
                  class="ghost small"
                  type="button"
                  :disabled="item.id === auth.user?.id"
                  @click="toggleStatus(item)"
                >
                  {{ item.status === 'active' ? '停用' : '启用' }}
                </button>
                <button
                  class="ghost small"
                  type="button"
                  :disabled="item.id === auth.user?.id"
                  @click="openReset(item)"
                >
                  重置密码
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </section>

      <section class="card">
        <h2>授权根目录</h2>
        <p class="body-text">
          普通用户只能看到并绑定<strong>被显式授予</strong>的目录。新增根目录默认不共享；
          收回授予后，对方已绑定的项目会立即无法访问。磁盘根目录不允许添加；已绑定项目的目录不能直接移除。
        </p>
        <ul v-if="roots.length" class="trash-list">
          <li v-for="item in roots" :key="item.id" class="trash-row">
            <div>
              <code class="mono">{{ item.path }}</code>
              <span v-if="item.label" class="hint">{{ item.label }}</span>
              <p class="hint">
                获授账号：
                <template v-if="item.grants && item.grants.length">
                  <span v-for="grant in item.grants" :key="grant.user_id" class="grant-chip">
                    {{ grant.display_name }}（{{ grant.username }}）
                    <button
                      class="ghost small danger-text"
                      type="button"
                      @click="revokeRoot(item, grant.user_id)"
                    >
                      收回
                    </button>
                  </span>
                </template>
                <template v-else>（未授予任何账号，普通用户看不到该目录）</template>
              </p>
              <div class="inline-form">
                <select v-model="grantPick[item.id]">
                  <option value="">选择要授予的账号…</option>
                  <option v-for="person in users" :key="person.id" :value="person.username">
                    {{ person.display_name }}（{{ person.username }}）
                  </option>
                </select>
                <button
                  class="ghost small"
                  type="button"
                  :disabled="!grantPick[item.id]"
                  @click="grantRoot(item)"
                >
                  授予
                </button>
              </div>
            </div>
            <button class="ghost small danger-text" type="button" @click="removeRoot(item)">移除</button>
          </li>
        </ul>
        <p v-else class="empty-hint">还没有授权目录，普通用户将无法绑定项目目录。</p>
        <div class="inline-form">
          <input v-model="rootPath" placeholder="服务器绝对路径，例如 D:\巡检数据" spellcheck="false" />
          <input v-model="rootLabel" placeholder="备注（可选）" />
          <button class="primary" type="button" @click="addRoot">添加</button>
        </div>
      </section>

      <section class="card">
        <h2>最近操作</h2>
        <ul class="audit">
          <li v-for="entry in audit" :key="entry.id">
            <span class="mono">{{ entry.at }}</span>
            <span>{{ entry.action }}</span>
            <span class="mono">{{ entry.target }}</span>
            <span class="hint">{{ entry.detail }}</span>
          </li>
        </ul>
        <p v-if="!audit.length" class="empty-hint">暂无记录。</p>
      </section>
    </template>
  </section>

  <DialogShell title="新建普通账号" :open="createOpen" @close="createOpen = false">
    <label class="field">
      <span>用户名</span>
      <input v-model="newUsername" placeholder="3-32 位，小写字母/数字/点/下划线/连字符" />
    </label>
    <label class="field">
      <span>显示名（可选）</span>
      <input v-model="newDisplayName" />
    </label>
    <label class="field">
      <span>初始密码</span>
      <input v-model="newPassword" type="password" autocomplete="new-password" />
      <small class="hint">至少 10 位；创建后请让本人登录并自行修改。</small>
    </label>
    <template #footer>
      <button class="ghost" type="button" @click="createOpen = false">取消</button>
      <button class="primary" type="button" @click="createUser">创建</button>
    </template>
  </DialogShell>

  <DialogShell title="重置密码" :open="resetOpen" @close="resetOpen = false">
    <p class="body-text">为 {{ resetTarget?.username }} 设置新密码，该账号所有登录态会立即失效。</p>
    <label class="field">
      <span>新密码</span>
      <input v-model="resetPassword" type="password" autocomplete="new-password" />
    </label>
    <template #footer>
      <button class="ghost" type="button" @click="resetOpen = false">取消</button>
      <button class="primary" type="button" @click="submitReset">重置</button>
    </template>
  </DialogShell>
</template>
