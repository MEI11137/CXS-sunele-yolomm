import { defineStore } from 'pinia'
import { ref } from 'vue'
import { ApiError, http } from '@/api/http'
import type { HealthData, MetaData } from '@/types'

/**
 * 运行时状态：只反映后端真实自报的信息。
 * 骨架阶段用它显示"哪些能力尚未实现"，不提供任何假数据。
 */
export const useRuntimeStore = defineStore('runtime', () => {
  const health = ref<HealthData | null>(null)
  const meta = ref<MetaData | null>(null)
  const loading = ref(false)
  const error = ref('')
  const loaded = ref(false)

  async function load(): Promise<void> {
    loading.value = true
    error.value = ''
    try {
      const [healthData, metaData] = await Promise.all([
        http.get<HealthData>('/health'),
        http.get<MetaData>('/meta'),
      ])
      health.value = healthData
      meta.value = metaData
      loaded.value = true
    } catch (failure) {
      health.value = null
      meta.value = null
      error.value =
        failure instanceof ApiError ? failure.message : '读取运行状态失败：' + String(failure)
    } finally {
      loading.value = false
    }
  }

  function featureEnabled(name: string): boolean {
    return Boolean(meta.value?.features?.[name])
  }

  return { health, meta, loading, error, loaded, load, featureEnabled }
})
