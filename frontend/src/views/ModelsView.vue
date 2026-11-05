<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import * as api from '@/api/business'
import { errorCode, errorMessage } from '@/api/http'
import { formatBytes } from '@/lib/business'
import { useBusinessStore } from '@/stores/business'
import type { ModelSummary } from '@/types'

/**
 * 模型页：导入 PT、显式确认类别映射、发布到**本项目**。
 *
 * 类别名不靠翻译猜：导入时如果权重自带类别与数据集类别对不上，后端返回
 * ``NEED_CLASS_MAPPING`` 并给出两份清单，这里把两份清单摆出来让用户填映射。
 */

const route = useRoute()
const router = useRouter()
const business = useBusinessStore()

const projectId = computed(() => String(route.params.projectId ?? ''))
const models = computed(() => business.models[projectId.value] ?? [])
const datasets = computed(() => business.datasets[projectId.value] ?? [])

const form = ref({
  name: '',
  family: 'yolomm' as 'yolo' | 'yolomm',
  secondary: 'ir' as '' | 'ir' | 'edge' | 'depth',
  datasetId: '',
  relPath: 'weights/best.pt',
  assetId: '',
})
const weights = ref<{ id: string; filename: string; size: number }[]>([])
const mapping = ref<{
  native: { index: number; name: string }[]
  target: string[]
  values: Record<string, string>
} | null>(null)
const localError = ref('')
const uploading = ref(false)

onMounted(async () => {
  await business.loadDatasets(projectId.value)
  await business.loadModels(projectId.value)
  await loadWeights()
})

async function loadWeights(): Promise<void> {
  try {
    const data = await api.listAssets(projectId.value, { kind: 'weight', limit: 50 })
    weights.value = data.assets.map((asset) => ({
      id: asset.id,
      filename: asset.filename,
      size: asset.size,
    }))
  } catch {
    weights.value = []
  }
}

async function onWeight(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  uploading.value = true
  localError.value = ''
  try {
    const asset = await api.uploadAsset(projectId.value, file)
    if (asset.kind !== 'weight') {
      localError.value = `这个文件按内容被识别为 ${asset.kind}，不是模型权重`
    } else {
      form.value.assetId = asset.id
      await loadWeights()
    }
  } catch (failure) {
    localError.value = errorMessage(failure)
  } finally {
    uploading.value = false
    input.value = ''
  }
}

async function submit(): Promise<void> {
  localError.value = ''
  mapping.value = null
  const payload = {
    name: form.value.name.trim(),
    family: form.value.family,
    secondary_modality: form.value.family === 'yolomm' ? form.value.secondary || 'ir' : null,
    dataset_id: form.value.datasetId || null,
    rel_path: form.value.assetId ? undefined : form.value.relPath || undefined,
    asset_id: form.value.assetId || undefined,
  }
  try {
    const data = await api.importModel(projectId.value, payload)
    business.notice = `模型「${data.model.name}」已导入；确认类别映射后发布`
    await business.loadModels(projectId.value)
  } catch (failure) {
    if (errorCode(failure) === 'NEED_CLASS_MAPPING') {
      const raw = (failure as { details?: unknown }).details as
        | { native_classes?: { index: number; name: string }[]; dataset_classes?: string[] }
        | undefined
      const target = raw?.dataset_classes ?? []
      const native = raw?.native_classes ?? []
      mapping.value = {
        native,
        target,
        values: Object.fromEntries(native.map((item) => [String(item.index), ''])),
      }
      localError.value = '权重类别与数据集类别名称不一致：请逐项确认映射（不会自动猜）'
      return
    }
    localError.value = errorMessage(failure)
  }
}

async function confirmMapping(): Promise<void> {
  if (!mapping.value) return
  const classMap: Record<string, number> = {}
  for (const item of mapping.value.native) {
    const chosen = mapping.value.values[String(item.index)]
    if (chosen === '' || chosen === undefined) {
      localError.value = `类别 ${item.name} 还没有选择对应项`
      return
    }
    classMap[String(item.index)] = Number(chosen)
  }
  try {
    await api.importModel(projectId.value, {
      name: form.value.name.trim(),
      family: form.value.family,
      secondary_modality: form.value.family === 'yolomm' ? form.value.secondary || 'ir' : null,
      dataset_id: form.value.datasetId || null,
      rel_path: form.value.assetId ? undefined : form.value.relPath || undefined,
      asset_id: form.value.assetId || undefined,
      class_map: classMap,
    })
    mapping.value = null
    localError.value = ''
    business.notice = '类别映射已保存，模型已导入'
    await business.loadModels(projectId.value)
  } catch (failure) {
    localError.value = errorMessage(failure)
  }
}

async function publish(model: ModelSummary): Promise<void> {
  const ok = await business.publish(projectId.value, model.id)
  if (ok) await business.loadModels(projectId.value)
}

function useForRecords(): void {
  const published = models.value.find((model) => model.status === 'published')
  if (!published) {
    localError.value = '先发布一个模型，检测才能选它'
    return
  }
  void router.push({
    name: 'records',
    params: { projectId: projectId.value },
    query: { model: published.id },
  })
}
</script>

<template>
  <section class="page">
    <header class="page-header">
      <div>
        <h1>模型</h1>
        <p class="subtitle">
          本项目共 {{ models.length }} 个模型，已发布
          {{ models.filter((item) => item.status === 'published').length }} 个
          <span class="hint">· 发布只作用于当前项目，不会替换别人的模型</span>
        </p>
      </div>
      <div class="header-actions">
        <button class="ghost small" type="button" @click="business.loadModels(projectId)">刷新</button>
        <button class="ghost small" type="button" @click="useForRecords">去检测</button>
      </div>
    </header>

    <p v-if="business.error" class="banner banner-error" role="alert">{{ business.error }}</p>
    <p v-else-if="business.notice" class="banner banner-ok" role="status">{{ business.notice }}</p>

    <div class="cards">
      <article v-for="model in models" :key="model.id" class="card">
        <p class="card-head">
          {{ model.name }}
          <span class="tag" :class="model.status === 'published' ? 'tag-on' : 'tag-off'">
            {{ model.status === 'published' ? '已发布' : model.status === 'archived' ? '已归档' : '未发布' }}
          </span>
        </p>
        <div class="kv">
          <span>家族</span><span class="mono">{{ model.family }}</span>
          <span>模态</span>
          <span>
            {{ model.model_mode === 'multimodal' ? `RGB + ${model.secondary_modality ?? '?'}` : '仅 RGB' }}
          </span>
          <span>权重</span><span class="mono">{{ formatBytes(model.weight_size) }}</span>
          <span>类别</span>
          <span>{{ model.classes.map((item) => `${item.index}:${item.name}`).join('、') || '未知' }}</span>
          <span>数据集成</span><span class="mono">{{ model.dataset_id ?? '未绑定' }}</span>
        </div>
        <div class="card-actions">
          <button
            v-if="model.status !== 'published'"
            class="primary small"
            type="button"
            :disabled="business.busy === `publish:${model.id}`"
            @click="publish(model)"
          >
            发布到本项目
          </button>
          <span v-else class="hint">可以用于检测</span>
        </div>
      </article>
      <p v-if="!models.length" class="empty-hint">
        还没有模型。上传一个 .pt，或填写项目目录里已有的权重路径。
      </p>
    </div>

    <div class="card">
      <p class="card-head">导入模型</p>
      <div class="form-grid">
        <label class="field">
          <span>名称</span>
          <input v-model="form.name" placeholder="例如 双模态热斑 v1" />
        </label>
        <label class="field">
          <span>家族</span>
          <select v-model="form.family">
            <option value="yolomm">yolomm（双模态）</option>
            <option value="yolo">yolo（单模态）</option>
          </select>
        </label>
        <label v-if="form.family === 'yolomm'" class="field">
          <span>第二模态</span>
          <select v-model="form.secondary">
            <option value="ir">红外</option>
            <option value="edge">边缘</option>
            <option value="depth">深度</option>
          </select>
        </label>
        <label class="field">
          <span>绑定数据集（用于类别对齐）</span>
          <select v-model="form.datasetId">
            <option value="">不绑定</option>
            <option v-for="dataset in datasets" :key="dataset.id" :value="dataset.id">
              {{ dataset.name }}（{{ dataset.classes.join('/') || '无类别表' }}）
            </option>
          </select>
        </label>
        <label class="field">
          <span>权重路径（项目内相对路径）</span>
          <input v-model="form.relPath" placeholder="weights/best.pt" :disabled="Boolean(form.assetId)" />
        </label>
        <label class="field">
          <span>或使用已上传的权重</span>
          <select v-model="form.assetId">
            <option value="">不使用</option>
            <option v-for="asset in weights" :key="asset.id" :value="asset.id">
              {{ asset.filename }}（{{ formatBytes(asset.size) }}）
            </option>
          </select>
        </label>
      </div>
      <div class="inline-form">
        <label class="button ghost small">
          {{ uploading ? '上传中…' : '上传 .pt' }}
          <input type="file" accept=".pt" hidden :disabled="uploading" @change="onWeight" />
        </label>
      </div>
      <p v-if="localError" class="form-error">{{ localError }}</p>

      <div v-if="mapping" class="card">
        <p class="card-head">类别映射（必须逐项确认）</p>
        <table class="table">
          <thead>
            <tr><th>权重里的类别</th><th>对应数据集类别</th></tr>
          </thead>
          <tbody>
            <tr v-for="item in mapping.native" :key="item.index">
              <td>{{ item.index }}: {{ item.name }}</td>
              <td>
                <select v-model="mapping.values[String(item.index)]">
                  <option value="">请选择</option>
                  <option v-for="(name, index) in mapping.target" :key="name" :value="String(index)">
                    {{ index }}: {{ name }}
                  </option>
                </select>
              </td>
            </tr>
          </tbody>
        </table>
        <div class="form-actions">
          <button class="primary small" type="button" @click="confirmMapping">保存映射并导入</button>
        </div>
      </div>

      <div class="form-actions">
        <button
          class="primary small"
          type="button"
          :disabled="!form.name.trim() || business.busy === 'model'"
          @click="submit"
        >
          导入
        </button>
      </div>
    </div>
  </section>
</template>
