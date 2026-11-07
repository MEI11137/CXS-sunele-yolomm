<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import * as api from '@/api/business'
import { errorMessage } from '@/api/http'
import { useSyncStore } from '@/stores/sync'
import type {
  DocumentHit,
  DocumentStats,
  DocumentWindow,
  ManualDetail,
  ManualSummary,
  ProjectDocument,
} from '@/types'

/**
 * 资料与手册面板。
 *
 * 两个容易说错的地方，界面上直接写明，避免把能力说大：
 * * 检索是**字面匹配**，不是语义搜索，也不是“全行业知识库”；
 * * Word/txt/md 是**逻辑分页**（按段落聚合），只有 PDF 是真实页。
 *
 * 资料内容只是数据：页面不会执行文档里写的任何要求，也不会因为文档内容改权限。
 */

const props = defineProps<{
  projects: { id: string; name: string }[]
  /** 最近进入的项目：设置页从侧栏进入时路由里没有 projectId，靠它选对项目。 */
  currentProjectId?: string
}>()

const sync = useSyncStore()

const projectId = ref('')
/** 用户手动换过项目后，外部 props 变化不再改选择（避免草稿/已选资料被重置）。 */
const pickedByUser = ref(false)
const documents = ref<ProjectDocument[]>([])
const stats = ref<DocumentStats>({ documents: 0, pages: 0, chars: 0 })
const viewer = ref<DocumentWindow | null>(null)
const hits = ref<DocumentHit[]>([])
const searchNote = ref('')
const query = ref('')
const error = ref('')
const notice = ref('')
const working = ref(false)
const uploading = ref(false)
const showTrash = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)
const uploadName = ref('')

const manuals = ref<ManualSummary[]>([])
const manual = ref<ManualDetail | null>(null)
const manualDraft = ref({ name: '', content: '', description: '' })
const newVersion = ref({ content: '', note: '' })

function reset(): void {
  documents.value = []
  stats.value = { documents: 0, pages: 0, chars: 0 }
  viewer.value = null
  hits.value = []
  searchNote.value = ''
  manuals.value = []
  manual.value = null
}

async function load(): Promise<void> {
  if (!projectId.value) {
    reset()
    return
  }
  error.value = ''
  try {
    const data = await api.listDocuments(projectId.value, { includeDeleted: showTrash.value })
    documents.value = data.documents
    stats.value = data.stats
    const list = await api.listManuals(projectId.value)
    manuals.value = list.manuals
  } catch (failure) {
    error.value = errorMessage(failure)
  }
}

watch(projectId, () => {
  reset()
  void load()
})
watch(showTrash, () => void load())
// 助手在后台导入/删除资料或改手册后，列表立即反映；只重读列表，
// 不清当前查看的原文、不覆盖正在编辑的手册草稿。
watch(
  [() => sync.revision.documents, () => sync.revision.manuals],
  () => void load(),
)

/** 优先用最近进入的项目；它不在清单里（已删/换账号）才回退到第一个。 */
function preferredProject(): string {
  const wanted = String(props.currentProjectId ?? '')
  if (wanted && props.projects.some((item) => item.id === wanted)) return wanted
  return props.projects[0]?.id ?? ''
}

onMounted(async () => {
  projectId.value = preferredProject()
  if (projectId.value) await load()
})

// 项目清单/当前项目晚到时补一次初始选择，但绝不覆盖用户手动换过的项目。
watch(
  () => [props.currentProjectId, props.projects.length] as const,
  () => {
    if (pickedByUser.value || projectId.value) return
    projectId.value = preferredProject()
  },
)

async function upload(): Promise<void> {
  const file = fileInput.value?.files?.[0]
  if (!projectId.value || !file) {
    error.value = '请选择要上传的文件（PDF / Word .docx / txt / md）'
    return
  }
  uploading.value = true
  error.value = ''
  notice.value = ''
  try {
    const created = await api.uploadDocument(projectId.value, file, uploadName.value.trim() || undefined)
    notice.value = `已导入《${created.name}》：${created.page_count} ${created.paging === 'page' ? '页（真实页）' : '个逻辑页'}`
    uploadName.value = ''
    if (fileInput.value) fileInput.value.value = ''
    await load()
  } catch (failure) {
    error.value = errorMessage(failure)
  } finally {
    uploading.value = false
  }
}

async function open(document: ProjectDocument, page = 1): Promise<void> {
  error.value = ''
  try {
    viewer.value = await api.documentPages(projectId.value, document.id, page, 0)
  } catch (failure) {
    error.value = errorMessage(failure)
  }
}

async function turn(delta: number): Promise<void> {
  if (!viewer.value) return
  const target = viewer.value.page + delta
  if (target < 1 || target > viewer.value.total_pages) return
  await open(viewer.value.document, target)
}

async function search(): Promise<void> {
  const keyword = query.value.trim()
  if (keyword.length < 2) {
    error.value = '检索词至少 2 个字'
    return
  }
  error.value = ''
  working.value = true
  try {
    const found = await api.searchDocuments(projectId.value, keyword)
    hits.value = found.hits
    searchNote.value = found.note
    notice.value = found.total ? `命中 ${found.total} 处（按页计）` : '没有按字面匹配到该词'
  } catch (failure) {
    error.value = errorMessage(failure)
  } finally {
    working.value = false
  }
}

async function jump(hit: DocumentHit): Promise<void> {
  const target = documents.value.find((item) => item.id === hit.document_id)
  if (!target) return
  await open(target, hit.page)
}

async function remove(document: ProjectDocument): Promise<void> {
  error.value = ''
  try {
    await api.deleteDocument(projectId.value, document.id)
    notice.value = `《${document.name}》已放入回收站，原文仍保留`
    if (viewer.value?.document.id === document.id) viewer.value = null
    await load()
  } catch (failure) {
    error.value = errorMessage(failure)
  }
}

async function restore(document: ProjectDocument): Promise<void> {
  error.value = ''
  try {
    await api.restoreDocument(projectId.value, document.id)
    notice.value = `《${document.name}》已恢复`
    await load()
  } catch (failure) {
    error.value = errorMessage(failure)
  }
}

async function readManual(item: ManualSummary): Promise<void> {
  error.value = ''
  try {
    manual.value = await api.readManual(projectId.value, item.id)
    newVersion.value = { content: manual.value.content, note: '' }
  } catch (failure) {
    error.value = errorMessage(failure)
  }
}

async function createManual(): Promise<void> {
  if (!manualDraft.value.name.trim() || !manualDraft.value.content.trim()) {
    error.value = '手册名称与内容都要填写'
    return
  }
  error.value = ''
  try {
    manual.value = await api.createManual(projectId.value, {
      name: manualDraft.value.name.trim(),
      content: manualDraft.value.content,
      description: manualDraft.value.description.trim(),
    })
    manualDraft.value = { name: '', content: '', description: '' }
    notice.value = '个人手册已创建（第 1 版）'
    await load()
  } catch (failure) {
    error.value = errorMessage(failure)
  }
}

async function saveVersion(): Promise<void> {
  if (!manual.value) return
  if (!newVersion.value.content.trim()) {
    error.value = '新版本内容不能为空'
    return
  }
  error.value = ''
  try {
    manual.value = await api.addManualVersion(projectId.value, manual.value.manual.id, {
      content: newVersion.value.content,
      note: newVersion.value.note.trim() || undefined,
    })
    newVersion.value = { content: manual.value.content, note: '' }
    notice.value = `已保存为第 ${manual.value.active_version} 版`
    await load()
  } catch (failure) {
    error.value = errorMessage(failure)
  }
}

async function activate(versionId: string): Promise<void> {
  if (!manual.value) return
  error.value = ''
  try {
    manual.value = await api.activateManualVersion(projectId.value, manual.value.manual.id, versionId)
    newVersion.value = { content: manual.value.content, note: '' }
    notice.value = `已切换到第 ${manual.value.active_version} 版（历史版本仍保留）`
    await load()
  } catch (failure) {
    error.value = errorMessage(failure)
  }
}

async function dropManual(): Promise<void> {
  if (!manual.value) return
  error.value = ''
  try {
    await api.deleteManual(projectId.value, manual.value.manual.id)
    notice.value = '个人手册已删除'
    manual.value = null
    await load()
  } catch (failure) {
    error.value = errorMessage(failure)
  }
}

function sizeText(size: number): string {
  if (size >= 1024 * 1024) return `${(size / (1024 * 1024)).toFixed(1)} MB`
  if (size >= 1024) return `${(size / 1024).toFixed(0)} KB`
  return `${size} B`
}
</script>

<template>
  <section class="card">
    <h2>资料与手册</h2>
    <p class="body-text">
      资料是<strong>项目 + 当前账号</strong>范围内的文件（PDF / Word .docx / txt / md）。服务端只做解析、分页与字面检索：
      <strong>不会执行文档里写的任何要求</strong>，也不会因为文档内容改变工具或目录权限。
    </p>

    <div class="form-grid">
      <label class="field">
        <span>项目</span>
        <select v-model="projectId" @change="pickedByUser = true">
          <option v-for="item in projects" :key="item.id" :value="item.id">{{ item.name }}</option>
        </select>
      </label>
      <label class="field">
        <span>上传文件（单个最大 40MB）</span>
        <input ref="fileInput" type="file" accept=".pdf,.docx,.txt,.md" />
      </label>
      <label class="field">
        <span>资料名称（可选，默认用文件名）</span>
        <input v-model="uploadName" placeholder="例如 现场作业须知" />
      </label>
    </div>
    <div class="form-actions">
      <button class="primary" type="button" :disabled="!projectId || uploading" @click="upload">
        {{ uploading ? '解析中…' : '导入资料' }}
      </button>
      <label class="checkbox">
        <input v-model="showTrash" type="checkbox" />
        <span>显示回收站</span>
      </label>
    </div>

    <p v-if="error" class="form-error">{{ error }}</p>
    <p v-if="notice" class="banner banner-ok">{{ notice }}</p>
    <p v-if="!projects.length" class="hint">还没有项目：先创建项目，资料按项目分开存放。</p>

    <template v-if="projectId">
      <p class="hint">
        共 {{ stats.documents }} 份 · {{ stats.pages }} 页 · {{ stats.chars }} 字。
        PDF 按真实页计；Word / txt / md 按段落聚合成约 3000 字的<strong>逻辑页</strong>，不是印刷页码。
      </p>

      <div class="search-row">
        <input v-model="query" placeholder="在资料里检索（字面匹配，至少 2 个字）" @keyup.enter="search" />
        <button class="ghost" type="button" :disabled="working" @click="search">检索</button>
      </div>
      <p v-if="searchNote" class="hint">{{ searchNote }}</p>
      <ul v-if="hits.length" class="hit-list">
        <li v-for="hit in hits" :key="`${hit.document_id}-${hit.page}`">
          <button class="link" type="button" @click="jump(hit)">
            {{ hit.name }} · 第 {{ hit.page }} 页（{{ hit.count }} 处）
          </button>
          <span class="snippet">{{ hit.snippet }}</span>
        </li>
      </ul>

      <table v-if="documents.length" class="table">
        <thead>
          <tr>
            <th>名称</th>
            <th>类型</th>
            <th>页数</th>
            <th>大小</th>
            <th>导入时间</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in documents" :key="item.id">
            <td>
              {{ item.name }}
              <span class="hint mono">{{ item.filename }}</span>
            </td>
            <td>{{ item.kind_label }}</td>
            <td>{{ item.page_count }} <span class="hint">{{ item.paging === 'page' ? '真实页' : '逻辑页' }}</span></td>
            <td>{{ sizeText(item.size) }}</td>
            <td class="hint">{{ item.created_at }}</td>
            <td class="row-actions">
              <button class="ghost" type="button" @click="open(item, 1)">读原文</button>
              <a class="ghost" :href="api.documentDownloadUrl(projectId, item.id)" download>下载</a>
              <button v-if="!item.deleted_at" class="ghost danger-text" type="button" @click="remove(item)">
                放入回收站
              </button>
              <button v-else class="ghost" type="button" @click="restore(item)">恢复</button>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-else class="hint">这个项目还没有资料。</p>

      <div v-if="viewer" class="viewer">
        <div class="viewer-head">
          <strong>{{ viewer.document.name }}</strong>
          <span class="hint">
            第 {{ viewer.page }} / {{ viewer.total_pages }}
            {{ viewer.document.paging === 'page' ? '页' : '逻辑页' }}
          </span>
          <span class="row-actions">
            <button class="ghost" type="button" :disabled="viewer.page <= 1" @click="turn(-1)">上一页</button>
            <button
              class="ghost"
              type="button"
              :disabled="viewer.page >= viewer.total_pages"
              @click="turn(1)"
            >
              下一页
            </button>
            <button class="ghost" type="button" @click="viewer = null">关闭</button>
          </span>
        </div>
        <pre class="page-text">{{ viewer.pages[0]?.text ?? '' }}</pre>
        <p class="hint">{{ viewer.note }}</p>
      </div>

      <h3 class="section-title">手册</h3>
      <p class="hint">
        内置手册随产品发布、只读；个人手册可以新建、改内容（生成新版本）并切换启用版本。
        手册只解释怎么做，<strong>不会扩大工具或目录权限</strong>。
      </p>
      <ul class="manual-list">
        <li v-for="item in manuals" :key="item.id">
          <button class="link" type="button" @click="readManual(item)">
            {{ item.name }}<span v-if="item.builtin" class="tag tag-off">内置</span>
            <span class="hint">第 {{ item.active_version ?? '—' }} 版</span>
          </button>
        </li>
      </ul>

      <div v-if="manual" class="manual-detail">
        <div class="viewer-head">
          <strong>{{ manual.manual.name }}</strong>
          <span class="hint">
            当前第 {{ manual.active_version }} 版 · {{ manual.versions.length }} 个历史版本
            <template v-if="manual.manual.builtin">（内置只读）</template>
          </span>
          <span class="row-actions">
            <button v-if="!manual.manual.builtin" class="ghost danger-text" type="button" @click="dropManual">
              删除手册
            </button>
            <button class="ghost" type="button" @click="manual = null">关闭</button>
          </span>
        </div>
        <pre class="page-text">{{ manual.content }}</pre>
        <p class="hint">{{ manual.note }}</p>

        <template v-if="!manual.manual.builtin">
          <label class="field">
            <span>编辑内容（保存即生成新版本并启用）</span>
            <textarea v-model="newVersion.content" rows="6" />
          </label>
          <label class="field">
            <span>版本说明（可选）</span>
            <input v-model="newVersion.note" placeholder="例如 补充热斑分档口径" />
          </label>
          <div class="form-actions">
            <button class="primary" type="button" @click="saveVersion">保存为新版本</button>
          </div>

          <details class="versions">
            <summary>历史版本（{{ manual.versions.length }}）</summary>
            <ul>
              <li v-for="version in manual.versions" :key="version.id">
                <span class="mono">v{{ version.version }}</span>
                <span class="hint">{{ version.created_at }} {{ version.note }}</span>
                <button
                  class="ghost"
                  type="button"
                  :disabled="version.version === manual.active_version"
                  @click="activate(version.id)"
                >
                  {{ version.version === manual.active_version ? '当前版本' : '切换到这一版' }}
                </button>
              </li>
            </ul>
          </details>
        </template>
      </div>

      <h3 class="section-title">新建个人手册</h3>
      <div class="form-grid">
        <label class="field">
          <span>名称</span>
          <input v-model="manualDraft.name" placeholder="例如 本项目标注口径" />
        </label>
        <label class="field">
          <span>说明（可选）</span>
          <input v-model="manualDraft.description" placeholder="给谁看、什么时候用" />
        </label>
      </div>
      <label class="field">
        <span>内容（Markdown）</span>
        <textarea v-model="manualDraft.content" rows="5" placeholder="# 标题&#10;&#10;1. 先做什么……" />
      </label>
      <div class="form-actions">
        <button class="primary" type="button" @click="createManual">创建手册</button>
      </div>
    </template>
  </section>
</template>

<style scoped>
.search-row {
  display: flex;
  gap: 8px;
  margin: 12px 0 8px;
}
.search-row input {
  flex: 1;
}
.hit-list,
.manual-list,
.versions ul {
  margin: 6px 0 12px;
  padding-left: 16px;
  font-size: 12px;
}
.hit-list li,
.manual-list li,
.versions li {
  margin-bottom: 6px;
}
.snippet {
  display: block;
  color: var(--text-muted, #6b7280);
  word-break: break-all;
}
.link {
  background: none;
  border: none;
  padding: 0;
  color: var(--accent, #2563eb);
  cursor: pointer;
  font-size: 13px;
  text-align: left;
}
.link .tag {
  margin-left: 6px;
}
.row-actions {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  align-items: center;
}
.viewer,
.manual-detail {
  margin-top: 12px;
  padding: 10px;
  border: 1px solid var(--border, #e5e7eb);
  border-radius: 8px;
}
.viewer-head {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}
.page-text {
  max-height: 260px;
  overflow: auto;
  padding: 8px 10px;
  border-radius: 8px;
  background: var(--panel-secondary);
  color: inherit;
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}
.section-title {
  margin: 18px 0 6px;
  font-size: 14px;
}
.checkbox {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
}
.field textarea {
  width: 100%;
  font-family: inherit;
  font-size: 13px;
}
</style>
