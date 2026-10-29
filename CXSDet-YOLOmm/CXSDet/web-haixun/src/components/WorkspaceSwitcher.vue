<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useWorkspaceStore } from '@/stores/workspace'
import { useSessionStore } from '@/stores/session'
import { useAuthStore } from '@/stores/auth'
import { getAdapter } from '@/services'
import type { Attachment, ResourceKind } from '@/types'
import { projectPath } from '@/utils/projectRoutes'
import LocalFolderPicker from './LocalFolderPicker.vue'

const props = withDefaults(defineProps<{ workspaceName?: string; mode?: 'create' | 'bind' }>(), { mode: 'create' })
const workspace = useWorkspaceStore()
const session = useSessionStore()
const auth = useAuthStore()
const router = useRouter()
const canUseLocalFolders = computed(() => auth.isLoggedIn && auth.user?.role === 1)
const show = ref(false)
const creating = ref(false)
const folderPicker = ref(false)
const targetId = ref('')
const createdId = ref('')
const error = ref('')
const progress = ref('')
const form = ref({ name: '', description: '', requirements: '', root_directory: '' })
const files = ref<File[]>([])
const uploaded = ref<Attachment[]>([])

function open() {
  form.value = { name: '', description: '', requirements: '', root_directory: '' }
  files.value = []; uploaded.value = []; createdId.value = ''; error.value = ''; progress.value = ''
  targetId.value = workspace.currentId
  folderPicker.value = false
  show.value = true
}
function pickFiles(event: Event) { files.value = Array.from((event.target as HTMLInputElement).files || []) }
function resourceKind(name: string): ResourceKind {
  const suffix = name.split('.').pop()?.toLowerCase()
  return suffix === 'zip' ? 'dataset' : suffix === 'pt' ? 'model' : ['jpg','jpeg','png','tif','tiff'].includes(suffix || '') ? 'image' : 'manual'
}
async function save() {
  if (creating.value) return
  error.value = ''; creating.value = true
  try {
    if (props.mode === 'bind') {
      await workspace.update(targetId.value, { root_directory: form.value.root_directory })
    } else {
      if (!createdId.value) {
        const project = await workspace.create({ ...form.value, root_directory: canUseLocalFolders.value ? form.value.root_directory : undefined })
        createdId.value = project.id
        await router.push(projectPath(createdId.value))
      }
      for (let index = uploaded.value.length; index < files.value.length; index++) {
        const file = files.value[index]
        progress.value = `正在上传 ${index + 1}/${files.value.length}：${file.name}`
        const asset = await getAdapter().uploadProjectFile(createdId.value, file)
        const resource = await getAdapter().registerResource(createdId.value, { kind: resourceKind(file.name), displayName: file.name,
          metadata: { file_id: asset.file_id, content_type: asset.content_type } })
        uploaded.value.push({ id: asset.file_id, name: file.name, type: file.name.split('.').pop() || 'file', size: file.size,
          state: 'uploaded', source: 'project', fileId: asset.file_id, resourceId: resource.id })
      }
      if (uploaded.value.length) session.queueAttachments(createdId.value, uploaded.value)
      await router.push(projectPath(createdId.value))
    }
    show.value = false
  } catch (e) {
    error.value = (e as Error).message + (createdId.value ? '。项目已创建，已成功上传的文件会保留，可重试剩余上传或关闭窗口。' : '')
  } finally { creating.value = false; progress.value = '' }
}
</script>

<template>
  <button class="btn setup-trigger" data-guide="workspace" :disabled="!auth.isLoggedIn" @click="open">{{ mode === 'bind' ? '绑定项目文件夹' : '＋ 新建项目' }}</button>
  <Teleport to="body">
    <div v-if="show" class="overlay" @click.self="!creating && (show = false)">
      <section class="dialog project-dialog" role="dialog" aria-modal="true" :aria-label="mode === 'bind' ? '绑定项目文件夹' : '新建项目'">
        <header class="dialog-header"><h3 class="dialog-title">{{ mode === 'bind' ? '绑定项目文件夹' : '新建项目' }}</h3><button class="btn btn-ghost" aria-label="关闭" :disabled="creating" @click="show = false">✕</button></header>
        <div class="dialog-body">
          <div v-if="mode === 'create'" class="field"><label for="project-name" class="field-label">项目名称</label><input id="project-name" v-model="form.name" class="input" placeholder="例如：热斑检测项目" :disabled="creating || !!createdId" /></div>
          <div v-if="canUseLocalFolders" class="field">
            <label for="project-folder" class="field-label">项目文件夹</label>
            <div class="folder-row"><input id="project-folder" v-model="form.root_directory" class="input" placeholder="选择本机项目文件夹" :disabled="creating || !!createdId" /><button class="btn" :disabled="creating || !!createdId" @click="folderPicker = !folderPicker">选择文件夹</button></div>
            <LocalFolderPicker v-if="folderPicker" :initial-path="form.root_directory" @select="form.root_directory = $event; folderPicker = false" @cancel="folderPicker = false" />
            <p class="help">海巡助手将在此目录处理项目文件。读取自动执行，修改按会话权限模式处理；删除和未知命令仍需批准。删除项目不会删除本机文件夹。</p>
          </div>
          <template v-if="mode === 'create'">
            <div class="field"><label for="project-description" class="field-label">项目说明</label><textarea id="project-description" v-model="form.description" class="textarea" rows="2" :disabled="creating || !!createdId"></textarea></div>
            <div class="field"><label for="project-requirements" class="field-label">项目要求</label><textarea id="project-requirements" v-model="form.requirements" class="textarea" rows="2" placeholder="例如：重点检查热斑漏标与 RGB/红外配对" :disabled="creating || !!createdId"></textarea></div>
            <div class="field"><label for="project-files" class="field-label">初始资料（可选）</label><input id="project-files" type="file" multiple accept=".zip,.pt,.jpg,.jpeg,.png,.tif,.tiff,.pdf,.docx,.md" :disabled="creating || !!createdId" @change="pickFiles" /><p class="help">可同时上传数据集 ZIP、PT 权重、图片或资料。上传完成后作为对话附件交给海巡助手；数据集校验、导入及训练仍需按流程确认。</p></div>
            <p v-if="uploaded.length" class="help">已上传 {{ uploaded.length }} 个附件。</p>
          </template>
          <p v-if="progress" role="status" class="help">{{ progress }}</p><p v-if="error" role="alert" class="error">{{ error }}</p>
        </div>
        <footer class="dialog-footer"><button class="btn" :disabled="creating" @click="show = false">{{ createdId ? '保留项目并关闭' : '取消' }}</button><button class="btn btn-primary" :disabled="creating || (!createdId && mode === 'create' && !form.name.trim()) || (canUseLocalFolders && !form.root_directory.trim())" @click="save">{{ creating ? '处理中…' : mode === 'bind' ? '确认绑定' : createdId ? '继续上传并进入项目' : '创建并进入项目' }}</button></footer>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.setup-trigger { width: 100%; text-align: center; }
.project-dialog { width: min(640px, calc(100vw - 32px)); max-height: calc(100vh - 40px); overflow-y: auto; }
.folder-row { display: flex; gap: 8px; }
.folder-row .input { flex: 1; min-width: 0; }
.folder-row .btn { flex-shrink: 0; }
.help { font-size: 12px; line-height: 1.6; color: var(--text-secondary); margin: 8px 0; }
.error { font-size: 12px; color: #b44239; }
</style>
