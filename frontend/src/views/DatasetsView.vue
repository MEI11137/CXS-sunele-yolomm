<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import * as api from '@/api/business'
import { errorMessage } from '@/api/http'
import { formatBytes } from '@/lib/business'
import DirectoryBrowser from '@/components/DirectoryBrowser.vue'
import { useBusinessStore } from '@/stores/business'
import { useSyncStore } from '@/stores/sync'
import { useWorkspaceStore } from '@/stores/workspace'
import type { DatasetSample, DatasetSummary, ReflowDraft } from '@/types'

/**
 * 数据集页：首屏先看清「有哪些数据集」，导入按需展开。
 *
 * 两件事分开：
 *  1. 列表只回答“我有哪些数据集、多大、能不能用”，低频动作收进「更多」；
 *  2. 导入是表单，点按钮才出现，取消就收起，不常驻占掉首屏。
 *
 * 导入本身走后端同一个服务（网页与助手共用），这里不做“前端预校验”去假装数据集合法：
 * 类别缺失、标签越界、ZIP 越界解压都由服务端明确报错，错误原文直接展示。
 */

const route = useRoute()
const router = useRouter()
const business = useBusinessStore()
const workspace = useWorkspaceStore()
const sync = useSyncStore()

const projectId = computed(() => String(route.params.projectId ?? ''))
const project = computed(() => (projectId.value ? workspace.projectById(projectId.value) : null))
const datasets = computed(() => business.datasets[projectId.value] ?? [])

// --------------------------------------------------------------------- 列表

const showDeleted = ref(false)
const drafts = ref<ReflowDraft[]>([])
const showDraftTrash = ref(false)

const activeId = ref('')
const active = computed<DatasetSummary | null>(
  () => datasets.value.find((item) => item.id === activeId.value) ?? null,
)
const samples = ref<DatasetSample[]>([])
const sampleSplit = ref('')

const draftReady = computed(
  () => drafts.value.filter((item) => item.status === 'ready' || item.status === 'editing').length,
)

async function reloadDatasets(): Promise<void> {
  await business.loadDatasets(projectId.value, showDeleted.value ? 'only' : 'exclude')
  drafts.value = (await api.listReflowDrafts(projectId.value, showDraftTrash.value)).drafts
}

onMounted(async () => {
  await reloadDatasets()
  await loadArchives()
  // Esc 关菜单：焦点可能已经移到菜单项，也可能停在别处，统一在窗口层收口。
  window.addEventListener('keydown', onWindowKeydown)
})

onBeforeUnmount(() => window.removeEventListener('keydown', onWindowKeydown))

// 助手在后台导入/删除/恢复数据集或保存/删除回流草稿后，当前页立即反映。
// 只重读列表，不清当前查看的样本、筛选与导入表单。
watch(
  [() => sync.revision.datasets, () => sync.revision.reflow],
  () => void reloadDatasets(),
)

function onWindowKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape' && menuMode.value) {
    event.stopPropagation()
    hideMenus()
  }
}

async function toggleDeleted(): Promise<void> {
  showDeleted.value = !showDeleted.value
  await reloadDatasets()
}

/** 名称、样本、模态、划分、状态：只展示真实字段，未知就给“—”，不编造。 */
function sourceLabel(dataset: DatasetSummary): string {
  if (dataset.kind === 'revision') return '回流修订'
  if (dataset.kind === 'new') return '回流新建'
  return dataset.source === 'local' ? '项目目录' : '上传压缩包'
}

function modalityLabel(dataset: DatasetSummary): string {
  const primary = dataset.primary_modality ?? dataset.modalities?.[0] ?? 'rgb'
  const base = primary === 'ir' ? 'IR' : 'RGB'
  const extra = dataset.secondary_modality
  return extra ? `${base} + ${extra.toUpperCase()}` : base
}

function splitCount(dataset: DatasetSummary, key: string): number | null {
  const value = dataset.split_counts?.[key]
  return typeof value === 'number' ? value : null
}

function splitText(dataset: DatasetSummary): string {
  return ['train', 'val', 'test']
    .map((key) => {
      const value = splitCount(dataset, key)
      return value === null ? '—' : String(value)
    })
    .join(' / ')
}

function statusLabel(dataset: DatasetSummary): string {
  if (dataset.deleted_at) return '已移入回收站'
  return dataset.status === 'ready' ? '可用' : dataset.status
}

function classHint(dataset: DatasetSummary): string {
  if (!dataset.classes.length) return '无类别表'
  return dataset.classes.length <= 2
    ? dataset.classes.join('、')
    : `${dataset.classes.slice(0, 2).join('、')} 等 ${dataset.classes.length} 类`
}

// --------------------------------------------------------------------- 行内动作

/**
 * 「更多」菜单只有一份，渲染在页面层、用 fixed 定位。
 *
 * 放进表格里时，菜单会被后面的行、右侧助手面板压住或裁掉（表格行与带 containment
 * 的容器都会影响绘制顺序）。挪到页面层 + fixed 定位后，位置由触发按钮算出来：
 * 左右不出屏、下面放不下就往上翻。
 */
const menuMode = ref<'' | 'row' | 'header'>('')
const menuFor = ref<DatasetSummary | null>(null)
const menuPos = ref({ left: 0, top: 0 })
const menuBox = ref<HTMLElement | null>(null)

function openMenu(mode: 'row' | 'header', dataset: DatasetSummary | null, event: MouseEvent): void {
  if (menuMode.value === mode && menuFor.value?.id === dataset?.id) {
    hideMenus()
    return
  }
  const trigger = event.currentTarget as HTMLElement
  const rect = trigger.getBoundingClientRect()
  const width = 184
  const estimated = mode === 'row' ? 132 : 92
  const left = Math.max(8, Math.min(rect.right - width, window.innerWidth - width - 8))
  const below = rect.bottom + 4
  const top =
    below + estimated > window.innerHeight - 8 ? Math.max(8, rect.top - estimated - 4) : below
  menuFor.value = dataset
  menuMode.value = mode
  menuPos.value = { left, top }
  void nextTick(() => {
    menuBox.value?.querySelector<HTMLElement>('button, a')?.focus()
  })
}

function hideMenus(): void {
  menuMode.value = ''
  menuFor.value = null
}

/**
 * 菜单项点击：先把目标数据集取出来再关菜单。
 * 直接写 `hideMenus(); removeDataset(menuFor)` 会把 menuFor 先置空，
 * 第二个参数就成了 null —— 动作静默失败。
 */
function withMenuTarget(action: (dataset: DatasetSummary) => void): void {
  const target = menuFor.value
  hideMenus()
  if (target) action(target)
}

/** 「用于训练」：带真实 dataset ID 打开模型页的训练表单——只预选，不自动开始。 */
function trainWith(dataset: DatasetSummary): void {
  hideMenus()
  void router.push({
    name: 'models',
    params: { projectId: dataset.project_id },
    query: { train_dataset: dataset.id },
  })
}

async function resplit(datasetId: string): Promise<void> {
  try {
    const data = await api.resplitDataset(projectId.value, datasetId, {
      strategy: 'even',
      seed: 7,
      request_key: `resplit:${datasetId}:${Date.now()}`,
    })
    business.notice = `已生成新的划分版本「${data.dataset.name}」，原数据集保留`
    await reloadDatasets()
  } catch (failure) {
    business.error = errorMessage(failure)
  }
}

async function removeDataset(dataset: DatasetSummary): Promise<void> {
  if (!window.confirm(`把「${dataset.name}」移入回收站？之后可以在这里恢复。`)) return
  try {
    await api.deleteDataset(projectId.value, dataset.id)
    business.notice = `已把「${dataset.name}」移入回收站`
    await reloadDatasets()
  } catch (failure) {
    business.error = errorMessage(failure)
  }
}

async function restoreDataset(dataset: DatasetSummary): Promise<void> {
  try {
    await api.restoreDataset(projectId.value, dataset.id)
    business.notice = `已恢复「${dataset.name}」`
    await reloadDatasets()
  } catch (failure) {
    business.error = errorMessage(failure)
  }
}

// --------------------------------------------------------------------- 待整理样本

const DRAFT_STATUS: Record<ReflowDraft['status'], string> = {
  editing: '待设置',
  ready: '待定稿',
  failed: '生成失败',
  finalized: '已生成',
  deleted: '已删除',
}

function draftStatusLabel(draft: ReflowDraft): string {
  return DRAFT_STATUS[draft.status] ?? draft.status
}

function draftModeLabel(draft: ReflowDraft): string {
  return draft.mode === 'revision' ? '更新原数据集修订版' : '另建数据集'
}

function goReflow(): void {
  void router.push({ name: 'reflow', params: { projectId: projectId.value } })
}

async function finalizeDraft(draftId: string): Promise<void> {
  try {
    const data = await api.finalizeReflowDraft(projectId.value, draftId)
    business.notice = data.note || `已生成「${data.dataset.name}」，原数据集保留`
    await reloadDatasets()
  } catch (failure) {
    business.error = errorMessage(failure)
  }
}

async function removeDraft(draftId: string): Promise<void> {
  try {
    await api.deleteReflowDraft(projectId.value, draftId)
    await reloadDatasets()
  } catch (failure) {
    business.error = errorMessage(failure)
  }
}

async function restoreDraft(draftId: string): Promise<void> {
  try {
    await api.restoreReflowDraft(projectId.value, draftId)
    await reloadDatasets()
  } catch (failure) {
    business.error = errorMessage(failure)
  }
}

// --------------------------------------------------------------------- 导入

type Source = 'dir' | 'zip'

const importOpen = ref(false)
const importing = ref(false)
const importError = ref('')
const nameError = ref('')
const sourceError = ref('')
const nameInput = ref<HTMLInputElement | null>(null)
const importPanel = ref<HTMLElement | null>(null)
const advancedOpen = ref(false)

const form = ref({
  name: '',
  source: 'dir' as Source,
  relPath: '',
  assetId: '',
  // 主模态：原图本身是 RGB 还是 IR。单 IR 数据集不需要也不接受第二张配图。
  primary: 'rgb' as 'rgb' | 'ir',
  secondary: '' as '' | 'ir' | 'edge' | 'depth',
  classes: '',
})

const archives = ref<{ id: string; filename: string; size: number }[]>([])
const uploading = ref(false)
const browseOpen = ref(false)

const selectedArchive = computed(
  () => archives.value.find((item) => item.id === form.value.assetId) ?? null,
)

function openImport(): void {
  importError.value = ''
  nameError.value = ''
  sourceError.value = ''
  importOpen.value = true
  void nextTick(() => nameInput.value?.focus())
}

function closeImport(): void {
  if (importing.value) return
  importOpen.value = false
  importError.value = ''
  nameError.value = ''
  sourceError.value = ''
  form.value = { name: '', source: 'dir', relPath: '', assetId: '', primary: 'rgb', secondary: '', classes: '' }
  advancedOpen.value = false
}

/** 切换来源时清掉另一边的选择，避免带着看不见的旧值提交。 */
function setSource(source: Source): void {
  form.value.source = source
  sourceError.value = ''
  if (source === 'dir') form.value.assetId = ''
  else form.value.relPath = ''
}

async function loadArchives(): Promise<void> {
  try {
    const data = await api.listAssets(projectId.value, { kind: 'archive', limit: 50 })
    archives.value = data.assets.map((asset) => ({
      id: asset.id,
      filename: asset.filename,
      size: asset.size,
    }))
  } catch {
    // 压缩包列表拉不到不影响目录导入；真正上传时再报错。
    archives.value = []
  }
}

/** 浏览器给的是绝对路径，导入要的是项目内相对路径：只在项目目录里换算，别处一律拒绝。 */
function pickFolder(path: string): void {
  browseOpen.value = false
  const root = String(project.value?.root_path ?? '').replace(/[\\/]+$/, '')
  const chosen = path.replace(/\\/g, '/')
  const rootPath = root.replace(/\\/g, '/')
  if (!rootPath) {
    sourceError.value = '这个项目还没有绑定文件夹，绑定后才能从这里导入'
    return
  }
  if (chosen === rootPath) {
    sourceError.value = '请选择项目文件夹里面的子目录'
    return
  }
  if (!chosen.startsWith(rootPath + '/')) {
    sourceError.value = '只能选择当前项目目录里的文件夹，不会浏览整块磁盘'
    return
  }
  form.value.relPath = chosen.slice(rootPath.length + 1)
  sourceError.value = ''
}

async function onZip(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  uploading.value = true
  importError.value = ''
  try {
    const asset = await api.uploadAsset(projectId.value, file)
    if (asset.kind !== 'archive') {
      importError.value = `这个文件按内容被识别为 ${asset.kind}，不是数据集压缩包`
    } else {
      setSource('zip')
      form.value.assetId = asset.id
      await loadArchives()
    }
  } catch (failure) {
    importError.value = errorMessage(failure)
  } finally {
    uploading.value = false
    input.value = ''
  }
}

async function submitImport(): Promise<void> {
  nameError.value = ''
  sourceError.value = ''
  importError.value = ''
  const name = form.value.name.trim()
  if (!name) {
    nameError.value = '先给这个数据集起个名字'
    nameInput.value?.focus()
    return
  }
  if (form.value.source === 'dir' && !form.value.relPath.trim()) {
    sourceError.value = '选一个工作区文件夹，或改用上传 ZIP'
    return
  }
  if (form.value.source === 'zip' && !form.value.assetId) {
    sourceError.value = '先上传一个 ZIP，或从已上传的压缩包里选'
    return
  }

  // 单 IR：原图就是红外图，只声明 ir，不造第二张图；RGB+配图保持原来的两模态语义。
  const modalities =
    form.value.primary === 'ir'
      ? ['ir']
      : form.value.secondary
        ? ['rgb', form.value.secondary]
        : ['rgb']
  const classes = form.value.classes
    .split(/[,，\s]+/)
    .map((item) => item.trim())
    .filter(Boolean)
  importing.value = true
  const ok = await business.importDataset(projectId.value, {
    name,
    rel_path: form.value.source === 'dir' ? form.value.relPath.trim() : undefined,
    asset_id: form.value.source === 'zip' ? form.value.assetId : undefined,
    modalities,
    secondary_modality: form.value.primary === 'ir' ? null : form.value.secondary || null,
    classes: classes.length ? classes : null,
  })
  importing.value = false
  if (ok) {
    importOpen.value = false
    form.value = { name: '', source: 'dir', relPath: '', assetId: '', primary: 'rgb', secondary: '', classes: '' }
    advancedOpen.value = false
    await reloadDatasets()
    return
  }
  // 失败：面板留着，填过的名字/来源/已上传的 ZIP 都还在，改完可直接重试。
  importError.value = business.error || '导入没有成功，请按上面的提示修正后重试'
  business.error = ''
  await loadArchives()
}

// --------------------------------------------------------------------- 样本

async function openDetail(dataset: DatasetSummary): Promise<void> {
  activeId.value = dataset.id
  sampleSplit.value = ''
  await loadSamples()
}

async function loadSamples(): Promise<void> {
  if (!active.value) return
  try {
    const data = await api.datasetSamples(
      projectId.value,
      active.value.id,
      sampleSplit.value || undefined,
    )
    samples.value = data.samples
  } catch (failure) {
    business.error = errorMessage(failure)
    samples.value = []
  }
}

watch(sampleSplit, () => void loadSamples())
</script>

<template>
  <section class="page">
    <header class="page-header">
      <div>
        <h1>数据集</h1>
        <p class="subtitle">
          {{ showDeleted ? `回收站里 ${datasets.length} 个` : `${datasets.length} 个` }}
          <template v-if="!showDeleted && draftReady"> · {{ draftReady }} 个待整理版本</template>
        </p>
      </div>
      <div class="header-actions">
        <button v-if="!showDeleted" class="primary small" type="button" @click="openImport">
          导入数据集
        </button>
        <button
          class="ghost small"
          type="button"
          title="把已复核样本回原数据集，或另建一个数据集"
          @click="goReflow"
        >
          数据回流
        </button>
        <button
          class="ghost small"
          type="button"
          aria-haspopup="menu"
          :aria-expanded="menuMode === 'header'"
          @click="openMenu('header', null, $event)"
        >
          更多
        </button>
      </div>
    </header>

    <!-- 只留一条提示：成功与失败各一，不再重复刷第二遍。 -->
    <p v-if="business.error" class="banner banner-error" role="alert">
      {{ business.error }}
      <button class="link" type="button" @click="business.error = ''">知道了</button>
    </p>
    <p v-else-if="business.notice" class="banner banner-ok" role="status">{{ business.notice }}</p>

    <!-- 导入：点按钮才出现，取消即收起，不占首屏。 -->
    <section
      v-if="importOpen"
      ref="importPanel"
      class="import-panel"
      aria-label="导入数据集"
      @keydown.esc="closeImport"
    >
      <div class="import-head">
        <h2>导入数据集</h2>
        <p class="hint">导入时会逐个样本校验图片、标签与配套模态，不合法会明确指出是哪一条。</p>
      </div>

      <div class="import-grid">
        <label class="field">
          <span>数据集名称</span>
          <input
            ref="nameInput"
            v-model="form.name"
            placeholder="例如 2026-11 巡检集"
            :aria-invalid="Boolean(nameError)"
            aria-describedby="import-name-error"
          />
          <span v-if="nameError" id="import-name-error" class="form-error">{{ nameError }}</span>
        </label>

        <div class="field">
          <span id="import-source-label">来源</span>
          <div class="segmented" role="group" aria-labelledby="import-source-label">
            <button
              type="button"
              :aria-pressed="form.source === 'dir'"
              @click="setSource('dir')"
            >
              工作区文件夹
            </button>
            <button
              type="button"
              :aria-pressed="form.source === 'zip'"
              @click="setSource('zip')"
            >
              上传 ZIP
            </button>
          </div>
        </div>

        <div v-if="form.source === 'dir'" class="field">
          <span>文件夹</span>
          <div class="zip-row">
            <input
              :value="form.relPath"
              readonly
              placeholder="还没有选择文件夹"
              aria-label="工作区文件夹"
            />
            <button class="ghost small" type="button" @click="browseOpen = !browseOpen">
              {{ browseOpen ? '收起' : '浏览文件夹' }}
            </button>
          </div>
          <span class="hint">只能选当前项目目录里的文件夹，不会浏览整块磁盘。</span>
          <span v-if="sourceError" class="form-error">{{ sourceError }}</span>
        </div>

        <div v-else class="field">
          <span>ZIP 文件</span>
          <div class="zip-row">
            <label class="button ghost small">
              {{ uploading ? '上传中…' : '选择 ZIP 上传' }}
              <input type="file" accept=".zip" hidden :disabled="uploading" @change="onZip" />
            </label>
            <span v-if="selectedArchive" class="hint">
              已选 {{ selectedArchive.filename }}（{{ formatBytes(selectedArchive.size) }}）
            </span>
          </div>
          <select v-if="archives.length" v-model="form.assetId" aria-label="已上传的压缩包">
            <option value="">或从已上传的压缩包里选</option>
            <option v-for="archive in archives" :key="archive.id" :value="archive.id">
              {{ archive.filename }}（{{ formatBytes(archive.size) }}）
            </option>
          </select>
          <span v-if="sourceError" class="form-error">{{ sourceError }}</span>
        </div>

        <label class="field">
          <span>主模态（原图本身是什么）</span>
          <select v-model="form.primary" aria-label="主模态">
            <option value="rgb">可见光 RGB</option>
            <option value="ir">红外 IR（单模态，原图就是 IR）</option>
          </select>
        </label>

        <label v-if="form.primary === 'rgb'" class="field">
          <span>配图模态（可选）</span>
          <select v-model="form.secondary" aria-label="配图模态">
            <option value="">不配图（仅 RGB）</option>
            <option value="ir">RGB + 红外（需要配图）</option>
            <option value="edge">RGB + 边缘（由 RGB 派生）</option>
            <option value="depth">RGB + 深度（需要配图）</option>
          </select>
        </label>
        <p v-else class="hint">单 IR 数据集只记录红外原图，不需要第二张配图；若要做 RGB+IR 双模态，请把主模态设为 RGB 并选择配图。</p>
      </div>

      <DirectoryBrowser
        v-if="form.source === 'dir' && browseOpen"
        :start-path="project?.root_path || undefined"
        @select="pickFolder"
        @close="browseOpen = false"
      />

      <details class="import-advanced" :open="advancedOpen" @toggle="advancedOpen = !advancedOpen">
        <summary>类别与划分（可选）</summary>
        <label class="field">
          <span>类别</span>
          <input
            v-model="form.classes"
            placeholder="资料里没有 data.yaml 时必填，例如 单一热斑,大面积热斑"
          />
          <span class="hint">系统不会猜类别；留空表示沿用文件夹里 data.yaml 的类别表。</span>
        </label>
      </details>

      <p v-if="importError" class="form-error" role="alert">
        {{ importError }}
        <button class="link" type="button" @click="submitImport">用当前内容再试一次</button>
      </p>

      <div class="import-actions">
        <button class="ghost small" type="button" :disabled="importing" @click="closeImport">
          取消
        </button>
        <button
          class="primary small"
          type="button"
          :disabled="importing"
          @click="submitImport"
        >
          {{ importing ? '导入中…' : '导入' }}
        </button>
      </div>
    </section>

    <!-- 列表：首屏就是“有哪些数据集”，一行看完名称/样本/模态/划分/状态。 -->
    <div class="ds-table-wrap">
      <table v-if="datasets.length" class="table ds-table">
        <caption class="sr-only">当前项目的数据集</caption>
        <thead>
          <tr>
            <th scope="col">数据集</th>
            <th scope="col" class="ds-col-num">样本</th>
            <th scope="col" class="ds-col-modality">模态</th>
            <th scope="col" class="ds-col-split" title="训练 / 验证 / 测试">训练 / 验证 / 测试</th>
            <th scope="col">状态</th>
            <th scope="col" class="ds-col-actions">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="dataset in datasets" :key="dataset.id">
            <td class="ds-name">
              <strong>{{ dataset.name }}</strong>
              <small>
                {{ sourceLabel(dataset) }} · {{ classHint(dataset) }}
              </small>
              <small v-if="dataset.notes" class="ds-note">{{ dataset.notes }}</small>
            </td>
            <td class="ds-col-num num">{{ dataset.sample_count }}</td>
            <td class="ds-col-modality">{{ modalityLabel(dataset) }}</td>
            <td class="ds-col-split num">{{ splitText(dataset) }}</td>
            <td>
              <span
                class="tag"
                :class="dataset.deleted_at ? 'tag-off' : dataset.status === 'ready' ? 'tag-on' : 'tag-admin'"
              >
                {{ statusLabel(dataset) }}
              </span>
            </td>
            <td class="ds-col-actions">
              <div class="ds-actions">
                <button class="ghost small" type="button" @click="openDetail(dataset)">查看</button>
                <button
                  class="ghost small"
                  type="button"
                  aria-haspopup="menu"
                  :aria-expanded="menuMode === 'row' && menuFor?.id === dataset.id"
                  @click="openMenu('row', dataset, $event)"
                >
                  更多
                </button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>

      <div v-else class="ds-empty">
        <p class="empty-hint">
          {{
            showDeleted
              ? '回收站里没有数据集。'
              : '还没有数据集。导入一个 YOLO 目录或 ZIP，或从「数据回流」定稿一份草稿。'
          }}
        </p>
      </div>
    </div>

    <!-- 待整理样本：稳定入口 + 折叠的草稿历史（不删任何已生成的数据集）。 -->
    <section class="refine-block" aria-label="待整理样本">
      <div class="refine-head">
        <div>
          <h2>待整理样本</h2>
          <p class="hint">
            已人工复核的样本可以更新原数据集的修订版，或另建一个数据集。原版本会保留，也不会自动开始训练。
          </p>
        </div>
        <button class="ghost small" type="button" @click="goReflow">处理复核结果</button>
      </div>

      <details v-if="drafts.length" class="draft-fold">
        <summary>
          草稿与生成历史（{{ drafts.length }}）
          <span v-if="draftReady" class="hint">· {{ draftReady }} 个待定稿</span>
        </summary>
        <div class="draft-tools">
          <button class="link small" type="button" @click="showDraftTrash = !showDraftTrash; reloadDatasets()">
            {{ showDraftTrash ? '看在用草稿' : '看草稿回收站' }}
          </button>
        </div>
        <table class="table">
          <caption class="sr-only">待整理草稿</caption>
          <thead>
            <tr>
              <th scope="col">名称</th>
              <th scope="col">去向</th>
              <th scope="col">状态</th>
              <th scope="col" class="ds-col-num">冲突</th>
              <th scope="col">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="draft in drafts" :key="draft.id">
              <td class="ds-name"><strong>{{ draft.name }}</strong></td>
              <td>{{ draftModeLabel(draft) }}</td>
              <td>
                {{ draftStatusLabel(draft) }}
                <span v-if="draft.error" class="form-error"> · {{ draft.error.message }}</span>
              </td>
              <td class="ds-col-num num">{{ draft.plan?.counts?.conflicts ?? 0 }}</td>
              <td>
                <div class="ds-actions">
                  <button
                    v-if="draft.status === 'ready'"
                    class="primary small"
                    type="button"
                    @click="finalizeDraft(draft.id)"
                  >
                    定稿
                  </button>
                  <button
                    v-if="draft.status === 'failed' || draft.status === 'editing'"
                    class="ghost small"
                    type="button"
                    @click="goReflow"
                  >
                    去修改
                  </button>
                  <button
                    v-if="draft.status !== 'deleted'"
                    class="ghost small"
                    type="button"
                    @click="removeDraft(draft.id)"
                  >
                    删除
                  </button>
                  <button v-else class="ghost small" type="button" @click="restoreDraft(draft.id)">
                    恢复
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </details>
      <p v-else class="hint refine-empty">
        还没有待整理版本。到「数据回流」选已确认的样本、预览并生成。
      </p>
    </section>

    <!-- 唯一的「更多」菜单：页面层 + fixed，不受表格行与右侧面板影响。 -->
    <template v-if="menuMode">
      <div class="row-menu-backdrop" @click="hideMenus"></div>
      <div
        ref="menuBox"
        class="row-menu"
        :style="{ left: menuPos.left + 'px', top: menuPos.top + 'px' }"
        role="menu"
        :aria-label="menuMode === 'header' ? '页面操作' : '数据集操作'"
        @keydown.esc="hideMenus"
      >
        <template v-if="menuMode === 'header'">
          <button type="button" role="menuitem" @click="hideMenus(); reloadDatasets()">刷新列表</button>
          <button type="button" role="menuitem" @click="hideMenus(); toggleDeleted()">
            {{ showDeleted ? '看正式数据集' : '打开回收站' }}
          </button>
        </template>
        <template v-else-if="menuFor">
          <button
            v-if="!menuFor.deleted_at"
            type="button"
            role="menuitem"
            @click="withMenuTarget((item) => resplit(item.id))"
          >
            重新划分
          </button>
          <button
            v-if="!menuFor.deleted_at"
            type="button"
            role="menuitem"
            @click="withMenuTarget(trainWith)"
          >
            用于训练
          </button>
          <a
            v-if="menuFor.export_url"
            class="row-menu-link"
            role="menuitem"
            :href="api.datasetExportUrl(menuFor.project_id, menuFor.id)"
            download
            @click="hideMenus()"
          >
            导出 ZIP
          </a>
          <button
            v-if="!menuFor.deleted_at"
            class="danger-text"
            type="button"
            role="menuitem"
            @click="withMenuTarget(removeDataset)"
          >
            移入回收站
          </button>
          <button
            v-else
            type="button"
            role="menuitem"
            @click="withMenuTarget(restoreDataset)"
          >
            恢复数据集
          </button>
        </template>
      </div>
    </template>

    <div v-if="active" class="modal-backdrop" @click.self="activeId = ''">
      <div class="modal" role="dialog" aria-label="数据集样本">
        <header class="modal-header">
          <h2>{{ active.name }} · 样本</h2>
          <button class="link" type="button" @click="activeId = ''">关闭</button>
        </header>
        <div class="modal-body">
          <label class="inline-field">
            <span class="hint">划分</span>
            <select v-model="sampleSplit">
              <option value="">全部</option>
              <option v-for="(count, split) in active.split_counts" :key="split" :value="split">
                {{ split }}（{{ count }}）
              </option>
            </select>
          </label>
          <table v-if="samples.length" class="table">
            <thead>
              <tr><th scope="col">划分</th><th scope="col">文件名</th><th scope="col">尺寸</th><th scope="col">框</th><th scope="col">配套图</th></tr>
            </thead>
            <tbody>
              <tr v-for="sample in samples" :key="sample.id">
                <td>{{ sample.split }}</td>
                <td class="mono ds-name">{{ sample.stem }}</td>
                <td class="mono num">{{ sample.width }}×{{ sample.height }}</td>
                <td class="num">{{ sample.boxes.length }}</td>
                <td class="mono ds-name">{{ sample.x_rel ? sample.x_rel.split(/[\\/]/).pop() : '—' }}</td>
              </tr>
            </tbody>
          </table>
          <p v-else class="empty-hint">这个划分里没有样本。</p>
        </div>
      </div>
    </div>
  </section>
</template>
