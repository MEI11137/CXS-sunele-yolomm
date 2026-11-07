<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import DialogShell from '@/components/DialogShell.vue'
import DirectoryBrowser from '@/components/DirectoryBrowser.vue'
import { useWorkspaceStore } from '@/stores/workspace'

/**
 * 新建项目：**名称 + 项目文件夹**是主要字段，说明可选折叠。
 *
 * 与 v1.6 WorkspaceSwitcher 的实际设计一致：建项目时就能指定文件夹。
 * 对第一次用的人，这一步就是“我的文件在哪”，藏进详情里只会让人多绕两圈。
 *
 * 两条边界：
 * * 目录必须来自管理员授予的根目录（复用 `/directories/browse` 与 `/projects/:id/root`），
 *   不新增全盘浏览、不扩大权限；
 * * 允许先不绑目录，但界面要说明“不能读写本地项目文件”，不能假装已经有权限。
 */
const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (event: 'close'): void; (event: 'created', projectId: string): void }>()

const workspace = useWorkspaceStore()
const router = useRouter()

const name = ref('')
const folder = ref('')
const description = ref('')
const showDescription = ref(false)
const browseOpen = ref(false)
const error = ref('')
const busy = ref(false)
/**
 * 项目已经建出来、但目录没绑上时记在这里：接下来只重试绑定，
 * 绝不会因为再点一次按钮就重复创建同名项目。
 */
const createdId = ref('')
const createdName = ref('')

const submitLabel = computed(() => (createdId.value ? '重试绑定并进入' : '创建并进入'))

watch(
  () => props.open,
  (open) => {
    if (!open) return
    name.value = ''
    folder.value = ''
    description.value = ''
    showDescription.value = false
    browseOpen.value = false
    error.value = ''
    createdId.value = ''
    createdName.value = ''
  },
)

async function submit(): Promise<void> {
  if (busy.value) return
  error.value = ''

  if (!createdId.value) {
    const label = name.value.trim()
    if (!label) {
      error.value = '请填写项目名称'
      return
    }
    busy.value = true
    try {
      const project = await workspace.createProject(label, description.value.trim())
      if (!project) {
        error.value = workspace.error || '创建失败'
        // 弹窗里已经说清楚了，不要再让顶部横幅重复报一次同样的错。
        workspace.error = ''
        return
      }
      createdId.value = project.id
      createdName.value = project.name
    } finally {
      busy.value = false
    }
  }

  const path = folder.value.trim()
  if (!path) {
    // 允许空项目：不绑目录就直接进入，下一步由主对话的空状态说明怎么绑。
    await finish()
    return
  }

  busy.value = true
  try {
    const updated = await workspace.bindRoot(createdId.value, path, false)
    if (!updated) {
      // 项目已经存在，这里只让用户改路径重试；不重名、不丢目录、不静默吞错。
      const reason = workspace.error || '未知原因'
      error.value =
        `项目“${createdName.value}”已创建，但绑定文件夹失败：${reason}。` +
        '可以改路径后重试，不会重复创建项目。'
      workspace.error = '' // 同上：错误留在弹窗里，别在顶部再刷一条
      return
    }
  } finally {
    busy.value = false
  }
  await finish()
}

async function finish(): Promise<void> {
  const id = createdId.value
  if (!id) return
  emit('close')
  emit('created', id)
  await router.push({ name: 'project', params: { projectId: id } })
}

function pickFolder(path: string): void {
  folder.value = path
  browseOpen.value = false
}
</script>

<template>
  <DialogShell title="新建项目" :open="open" @close="emit('close')">
    <label class="field">
      <span>项目名称</span>
      <input
        v-model="name"
        placeholder="例如 光伏电站 A 区"
        :disabled="createdId !== ''"
        @keydown.enter="submit"
      />
    </label>

    <label class="field">
      <span>项目文件夹（服务器上已授权的目录）</span>
      <div class="folder-row">
        <input
          v-model="folder"
          placeholder="例如 D:\巡检数据\2026-11"
          spellcheck="false"
          @keydown.enter="submit"
        />
        <button class="ghost" type="button" @click="browseOpen = !browseOpen">
          {{ browseOpen ? '收起' : '浏览' }}
        </button>
      </div>
    </label>
    <p class="hint">
      可选。不绑定文件夹时<strong>不能读写本地项目文件</strong>，创建后可在“项目详情”里绑定；
      能选的只有管理员授予你的目录，不会浏览整块磁盘。
    </p>

    <button class="link small" type="button" @click="showDescription = !showDescription">
      {{ showDescription ? '收起说明' : '+ 添加说明（可选）' }}
    </button>
    <label v-if="showDescription" class="field">
      <span>说明</span>
      <textarea v-model="description" rows="2" placeholder="这次巡检看什么"></textarea>
    </label>

    <DirectoryBrowser
      v-if="browseOpen"
      :start-path="folder || undefined"
      @select="pickFolder"
      @close="browseOpen = false"
    />

    <p v-if="error" class="form-error" role="alert">{{ error }}</p>

    <template #footer>
      <button class="ghost" type="button" @click="emit('close')">取消</button>
      <button class="primary" type="button" :disabled="busy" @click="submit">{{ submitLabel }}</button>
    </template>
  </DialogShell>
</template>
