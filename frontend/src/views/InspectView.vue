<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import * as api from '@/api/business'
import { errorMessage } from '@/api/http'
import { inspectFieldHint, inspectSecondaryLabel } from '@/lib/business'
import { useBusinessStore } from '@/stores/business'
import type { AssetSummary, DetectionRecord, ModelSummary } from '@/types'

/**
 * 手动图片巡检：不经过对话，也能用同一套检测服务跑一张图。
 *
 * 边界：
 * * 模型必须是**本项目已发布**的（未发布/别的项目不会出现在下拉里）；
 * * 单模态只收一张 RGB；多模态必须给配套图，edge 模态允许“由 RGB 派生”（会记录来源）；
 * * 检测、落库、结果图都走 `services.detection`——这里的按钮和 Agent 工具是同一个执行者，
 *   不存在“网页一套、助手一套”的分叉。
 */

const route = useRoute()
const router = useRouter()
const business = useBusinessStore()

const projectId = computed(() => String(route.params.projectId ?? ''))
const models = computed(() => business.publishedModels(projectId.value))
const selectedModelId = ref('')
const model = computed<ModelSummary | null>(
  () => models.value.find((item) => item.id === selectedModelId.value) ?? null,
)

const multimodal = computed(() => model.value?.model_mode === 'multimodal')
const secondary = computed(() => String(model.value?.secondary_modality ?? 'ir'))
const secondaryLabel = computed(() => inspectSecondaryLabel(secondary.value))

const images = ref<AssetSummary[]>([])
const rgbId = ref('')
const xId = ref('')
const deriveEdge = ref(true)
const confidence = ref(0.25)
const uploading = ref('')
const localError = ref('')
const record = ref<DetectionRecord | null>(null)

const hint = computed(() => {
  if (!model.value) return '先选一个已发布模型。'
  return inspectFieldHint({
    mode: String(model.value.model_mode),
    secondary: secondary.value,
    hasX: Boolean(xId.value),
    deriveEdge: deriveEdge.value,
  })
})

const rgbOptions = computed(() => images.value.filter((item) => item.role !== 'ir' && item.role !== 'edge'))
const xOptions = computed(() =>
  images.value.filter((item) => item.role === secondary.value || item.role === 'x' || item.role === 'image'),
)

onMounted(async () => {
  await business.loadModels(projectId.value)
  await loadImages()
  if (!selectedModelId.value && models.value.length) selectedModelId.value = models.value[0].id
})

watch(selectedModelId, () => {
  xId.value = ''
  deriveEdge.value = true
  record.value = null
})

async function loadImages(): Promise<void> {
  try {
    const data = await api.listAssets(projectId.value, { kind: 'image', limit: 100 })
    images.value = data.assets
  } catch (failure) {
    localError.value = errorMessage(failure)
  }
}

async function onUpload(event: Event, target: 'rgb' | 'x'): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  uploading.value = target
  localError.value = ''
  try {
    const role = target === 'rgb' ? 'rgb' : secondary.value
    const asset = await api.uploadAsset(projectId.value, file, { role })
    if (asset.kind !== 'image') {
      localError.value = `这个文件按内容被识别为 ${asset.kind}，不是图片`
    } else {
      await loadImages()
      if (target === 'rgb') rgbId.value = asset.id
      else xId.value = asset.id
    }
  } catch (failure) {
    localError.value = errorMessage(failure)
  } finally {
    uploading.value = ''
    input.value = ''
  }
}

async function submit(): Promise<void> {
  if (!model.value || !rgbId.value) {
    localError.value = '需要选择一个已发布模型和一张 RGB 图片'
    return
  }
  localError.value = ''
  business.error = ''
  business.busy = 'detect'
  try {
    const payload: {
      model_id: string
      rgb_asset_id: string
      confidence: number
      x_asset_id?: string | null
    } = {
      model_id: model.value.id,
      rgb_asset_id: rgbId.value,
      confidence: confidence.value,
    }
    if (multimodal.value && xId.value) payload.x_asset_id = xId.value
    const data = await api.detectOne(projectId.value, payload)
    record.value = data.record
    if (data.record.status === 'failed') {
      localError.value = data.record.error?.message || '检测失败，没有生成结果'
    } else {
      business.notice = `检测完成，记录 ${data.record.id}`
    }
    await business.loadRecords(projectId.value)
  } catch (failure) {
    localError.value = errorMessage(failure)
  } finally {
    business.busy = ''
  }
}

function openRecords(): void {
  void router.push({ name: 'records', params: { projectId: projectId.value } })
}
</script>

<template>
  <section class="page">
    <header class="page-header">
      <div>
        <h1>图片巡检</h1>
        <p class="subtitle">
          手动选图、选模型跑一次检测
          <span class="hint">· 与助手调用的是同一个检测服务，结果直接进识别记录</span>
        </p>
      </div>
      <div class="header-actions">
        <button class="ghost small" type="button" @click="loadImages">刷新图片</button>
        <button class="ghost small" type="button" @click="openRecords">识别记录</button>
      </div>
    </header>

    <p v-if="business.error" class="banner banner-error" role="alert">{{ business.error }}</p>
    <p v-if="localError" class="banner banner-error" role="alert">{{ localError }}</p>

    <div class="card">
      <p class="card-head">检测参数</p>
      <div class="form-grid">
        <label class="field">
          <span>已发布模型</span>
          <select v-model="selectedModelId">
            <option value="">选择模型</option>
            <option v-for="item in models" :key="item.id" :value="item.id">
              {{ item.name }}（{{ item.model_mode === 'multimodal' ? `双模态 ${item.secondary_modality}` : '单模态' }}）
            </option>
          </select>
        </label>
        <label class="field">
          <span>置信度</span>
          <input v-model.number="confidence" type="number" min="0.01" max="0.99" step="0.01" />
        </label>
        <label class="field">
          <span>RGB 图片</span>
          <select v-model="rgbId">
            <option value="">选择项目内图片</option>
            <option v-for="item in rgbOptions" :key="item.id" :value="item.id">
              {{ item.filename }}（{{ item.width }}×{{ item.height }}）
            </option>
          </select>
        </label>
        <label v-if="multimodal" class="field">
          <span>{{ secondaryLabel }}</span>
          <select v-model="xId" :disabled="secondary === 'edge' && deriveEdge">
            <option value="">不选（仅 edge 可派生）</option>
            <option v-for="item in xOptions" :key="item.id" :value="item.id">
              {{ item.filename }}（{{ item.role }}）
            </option>
          </select>
        </label>
      </div>

      <div class="inline-form">
        <label class="button ghost small">
          {{ uploading === 'rgb' ? '上传中…' : '上传 RGB' }}
          <input type="file" accept="image/*" hidden :disabled="uploading !== ''" @change="onUpload($event, 'rgb')" />
        </label>
        <label v-if="multimodal && secondary !== 'edge'" class="button ghost small">
          {{ uploading === 'x' ? '上传中…' : `上传 ${secondary.toUpperCase()}` }}
          <input type="file" accept="image/*" hidden :disabled="uploading !== ''" @change="onUpload($event, 'x')" />
        </label>
        <label v-if="multimodal && secondary === 'edge'" class="inline-field">
          <input v-model="deriveEdge" type="checkbox" />
          <span class="hint">没有现成配图时，由 RGB 派生边缘图</span>
        </label>
        <span v-if="images.length" class="hint">项目内已有 {{ images.length }} 张图片</span>
      </div>
      <p class="hint">{{ hint }}</p>

      <div class="form-actions">
        <button
          class="primary small"
          type="button"
          :disabled="!model || !rgbId || business.busy === 'detect'"
          @click="submit"
        >
          {{ business.busy === 'detect' ? '检测中…' : '运行检测' }}
        </button>
      </div>
    </div>

    <article v-if="record" class="card">
      <p class="card-head">本次结果 · {{ record.id }}</p>
      <div class="kv">
        <span>状态</span><span>{{ record.status }}</span>
        <span>命中</span><span>{{ record.hit_count }}</span>
        <span>置信度</span><span class="mono">{{ record.confidence }}</span>
        <span>配图来源</span><span>{{ record.x_origin }}</span>
        <span>时间</span><span class="mono">{{ record.created_at_local }}</span>
      </div>
      <p v-if="record.error" class="form-error">{{ record.error.message }}</p>
      <ul v-if="record.boxes.length" class="business-items">
        <li v-for="(box, index) in record.boxes" :key="index">
          <span>{{ box.class_name }}</span>
          <span class="mono">{{ box.confidence.toFixed(3) }}</span>
          <span class="mono">{{ box.bbox.map((value) => Math.round(value)).join(',') }}</span>
        </li>
      </ul>
      <figure v-if="record.result_url" class="result-figure">
        <img :src="record.result_url" alt="检测结果" />
      </figure>
      <div class="card-actions">
        <button class="ghost small" type="button" @click="openRecords">去识别记录核查</button>
      </div>
    </article>

    <p v-if="!models.length" class="empty-hint">
      还没有已发布的模型。先到「模型」页导入 best.pt、确认类别映射并发布。
    </p>
    <p v-else-if="!images.length" class="empty-hint">
      项目里还没有图片资产：先用上面的「上传 RGB」选一张，或从「数据集」页导入。
    </p>

    <p class="hint">支持的文件：JPEG/PNG/BMP/WEBP/TIFF；单张上限由服务端判定，超限会明确报错。</p>
  </section>
</template>
