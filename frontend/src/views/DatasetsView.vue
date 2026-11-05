<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import * as api from '@/api/business'
import { errorMessage } from '@/api/http'
import { formatBytes } from '@/lib/business'
import { useBusinessStore } from '@/stores/business'
import type { DatasetSample, DatasetSummary } from '@/types'

/**
 * 数据集页：导入（目录 / 上传的 ZIP）+ 查看每个划分的真实样本。
 *
 * 导入本身走的是后端同一个服务（网页与助手共用），所以这里不做任何“前端预校验”
 * 去假装数据集合法：类别缺失、标签越界、ZIP 越界解压都由服务端明确报错。
 */

const route = useRoute()
const business = useBusinessStore()

const projectId = computed(() => String(route.params.projectId ?? ''))
const datasets = computed(() => business.datasets[projectId.value] ?? [])

const form = ref({
  name: '',
  source: 'dir' as 'dir' | 'zip',
  relPath: '',
  assetId: '',
  secondary: 'ir' as '' | 'ir' | 'edge' | 'depth',
  classes: '',
})
const candidates = ref<{ rel_path: string; name: string; kind: string; size: number }[]>([])
const archives = ref<{ id: string; filename: string; size: number }[]>([])
const uploadError = ref('')
const uploading = ref(false)

const activeId = ref('')
const active = computed<DatasetSummary | null>(
  () => datasets.value.find((item) => item.id === activeId.value) ?? null,
)
const samples = ref<DatasetSample[]>([])
const sampleSplit = ref('')

onMounted(async () => {
  await business.loadDatasets(projectId.value)
  await loadArchives()
  await refreshCandidates()
})

async function loadArchives(): Promise<void> {
  try {
    const data = await api.listAssets(projectId.value, { kind: 'archive', limit: 50 })
    archives.value = data.assets.map((asset) => ({
      id: asset.id,
      filename: asset.filename,
      size: asset.size,
    }))
  } catch (failure) {
    uploadError.value = errorMessage(failure)
  }
}

async function refreshCandidates(): Promise<void> {
  try {
    const data = await api.listCandidates(projectId.value, '.')
    candidates.value = data.entries
  } catch {
    candidates.value = []
  }
}

async function onZip(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  uploading.value = true
  uploadError.value = ''
  try {
    const asset = await api.uploadAsset(projectId.value, file)
    if (asset.kind !== 'archive') {
      uploadError.value = `这个文件按内容被识别为 ${asset.kind}，不是数据集压缩包`
    } else {
      form.value.source = 'zip'
      form.value.assetId = asset.id
      await loadArchives()
    }
  } catch (failure) {
    uploadError.value = errorMessage(failure)
  } finally {
    uploading.value = false
    input.value = ''
  }
}

async function submit(): Promise<void> {
  if (!activeName.value) return
  const modalities = form.value.secondary ? ['rgb', form.value.secondary] : ['rgb']
  const classes = form.value.classes
    .split(/[,，\s]+/)
    .map((item) => item.trim())
    .filter(Boolean)
  const ok = await business.importDataset(projectId.value, {
    name: activeName.value,
    rel_path: form.value.source === 'dir' ? form.value.relPath || undefined : undefined,
    asset_id: form.value.source === 'zip' ? form.value.assetId || undefined : undefined,
    modalities,
    secondary_modality: form.value.secondary || null,
    classes: classes.length ? classes : null,
  })
  if (ok) {
    form.value.name = ''
    form.value.classes = ''
    await business.loadDatasets(projectId.value)
  }
}

const activeName = computed(() => form.value.name.trim())

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
          只显示当前项目的数据集：{{ datasets.length }} 个
          <span class="hint">· 导入时会逐样本校验图片、标签与配套模态</span>
        </p>
      </div>
      <div class="header-actions">
        <button class="ghost small" type="button" @click="business.loadDatasets(projectId)">
          刷新
        </button>
      </div>
    </header>

    <p v-if="business.error" class="banner banner-error" role="alert">
      {{ business.error }}
      <button class="link" type="button" @click="business.error = ''">知道了</button>
    </p>
    <p v-else-if="business.notice" class="banner banner-ok" role="status">{{ business.notice }}</p>

    <div class="cards">
      <article v-for="dataset in datasets" :key="dataset.id" class="card">
        <p class="card-head">{{ dataset.name }}</p>
        <div class="kv">
          <span>样本</span><span>{{ dataset.sample_count }}</span>
          <span>模态</span><span>{{ dataset.modalities.join(' + ') }}</span>
          <span>类别</span><span>{{ dataset.classes.join('、') || '（无类别表）' }}</span>
          <span>划分</span>
          <span class="mono">
            {{ Object.entries(dataset.split_counts).map(([key, value]) => `${key}:${value}`).join(' ') }}
          </span>
          <span>来源</span><span>{{ dataset.source === 'local' ? '项目目录' : '上传压缩包' }}</span>
        </div>
        <p v-if="dataset.notes" class="hint">{{ dataset.notes }}</p>
        <div class="card-actions">
          <button class="ghost small" type="button" @click="openDetail(dataset)">查看样本</button>
        </div>
      </article>
      <p v-if="!datasets.length" class="empty-hint">
        还没有数据集。可以把一个含 data.yaml 的 YOLO 目录导入，或上传打包好的 ZIP。
      </p>
    </div>

    <div class="card">
      <p class="card-head">导入数据集</p>
      <div class="form-grid">
        <label class="field">
          <span>名称</span>
          <input v-model="form.name" placeholder="例如 2026-11 巡检集" />
        </label>
        <label class="field">
          <span>来源</span>
          <select v-model="form.source">
            <option value="dir">项目目录</option>
            <option value="zip">已上传的 ZIP</option>
          </select>
        </label>
        <label v-if="form.source === 'dir'" class="field">
          <span>目录（项目内相对路径）</span>
          <input v-model="form.relPath" placeholder="例如 datasets/raw" />
        </label>
        <label v-else class="field">
          <span>压缩包</span>
          <select v-model="form.assetId">
            <option value="">选择已上传的 ZIP</option>
            <option v-for="archive in archives" :key="archive.id" :value="archive.id">
              {{ archive.filename }}（{{ formatBytes(archive.size) }}）
            </option>
          </select>
        </label>
        <label class="field">
          <span>第二模态</span>
          <select v-model="form.secondary">
            <option value="">仅 RGB</option>
            <option value="ir">红外（需要 real IR 配图）</option>
            <option value="edge">边缘（由 RGB 派生，会记录 provenance）</option>
            <option value="depth">深度（需要配图）</option>
          </select>
        </label>
        <label class="field">
          <span>类别（缺 data.yaml 时必填，系统不猜）</span>
          <input v-model="form.classes" placeholder="例如 单一热斑,大面积热斑" />
        </label>
      </div>

      <div class="inline-form">
        <label class="button ghost small">
          {{ uploading ? '上传中…' : '上传 ZIP 作为来源' }}
          <input type="file" accept=".zip" hidden :disabled="uploading" @change="onZip" />
        </label>
        <button class="link small" type="button" @click="refreshCandidates">刷新目录候选</button>
        <span v-if="candidates.length" class="hint">
          目录里可选的候选：{{ candidates.slice(0, 4).map((item) => item.name).join('、') }}
        </span>
      </div>
      <p v-if="uploadError" class="form-error">{{ uploadError }}</p>

      <div class="form-actions">
        <button
          class="primary small"
          type="button"
          :disabled="!activeName || business.busy === 'dataset'"
          @click="submit"
        >
          导入
        </button>
      </div>
    </div>

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
              <tr><th>划分</th><th>文件名</th><th>尺寸</th><th>框</th><th>配套图</th></tr>
            </thead>
            <tbody>
              <tr v-for="sample in samples" :key="sample.id">
                <td>{{ sample.split }}</td>
                <td class="mono">{{ sample.stem }}</td>
                <td class="mono">{{ sample.width }}×{{ sample.height }}</td>
                <td>{{ sample.boxes.length }}</td>
                <td class="mono">{{ sample.x_rel ? sample.x_rel.split(/[\\/]/).pop() : '—' }}</td>
              </tr>
            </tbody>
          </table>
          <p v-else class="empty-hint">这个划分里没有样本。</p>
        </div>
      </div>
    </div>
  </section>
</template>
