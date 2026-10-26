import { defineStore } from 'pinia'
import { ref } from 'vue'
import type { Defect, Annotation, DetectionModel } from '@/types'
import { getAdapter } from '@/services'

export const useInspectionStore = defineStore('inspection', () => {
  const defects = ref<Defect[]>([])
  const models = ref<DetectionModel[]>([])
  const annotations = ref<Record<string, Annotation>>({})
  const loading = ref(false)
  const loaded = ref(false)

  async function load() {
    if (loaded.value) return
    loading.value = true
    try {
      const [d, m] = await Promise.all([
        getAdapter().listDefects(''),
        getAdapter().listModels(),
      ])
      defects.value = d
      // 模型列表是分页结构，页面只用第一页的条目做选择
      models.value = m.items
      loaded.value = true
    } finally {
      loading.value = false
    }
  }

  function getAnnotation(imageId: string): Annotation {
    if (!annotations.value[imageId]) {
      annotations.value[imageId] = { imageId, boxes: [], noDefect: false }
    }
    return annotations.value[imageId]
  }

  function setAnnotation(imageId: string, ann: Annotation) {
    annotations.value[imageId] = ann
  }

  function clearAnnotation(imageId: string) {
    delete annotations.value[imageId]
  }

  // 统计从当前缺陷数据计算
  const pendingCount = () =>
    defects.value.filter((d) => d.reviewStatus === 'pending').length
  const reviewedCount = () =>
    defects.value.filter((d) => d.reviewStatus === 'done').length

  return {
    defects,
    models,
    annotations,
    loading,
    loaded,
    load,
    getAnnotation,
    setAnnotation,
    clearAnnotation,
    pendingCount,
    reviewedCount,
  }
})
