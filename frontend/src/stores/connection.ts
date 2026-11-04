import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { errorMessage, http } from '@/api/http'
import type { ModelConnection, ProviderPreset } from '@/types'

/** 当前用户自己的模型连接；管理员没有“看别人连接”的接口。 */
export const useConnectionStore = defineStore('connection', () => {
  const connection = ref<ModelConnection | null>(null)
  const providers = ref<ProviderPreset[]>([])
  const loading = ref(false)
  const checking = ref(false)
  const error = ref('')
  const notice = ref('')
  const loaded = ref(false)

  const hasConnection = computed(() => connection.value !== null)
  const capabilitiesValid = computed(
    () => connection.value !== null && !connection.value.capabilities_stale,
  )

  async function loadProviders(): Promise<void> {
    if (providers.value.length) return
    try {
      const data = await http.get<{ providers: ProviderPreset[] }>('/connections/providers')
      providers.value = data.providers
    } catch (failure) {
      error.value = errorMessage(failure)
    }
  }

  async function load(): Promise<void> {
    loading.value = true
    error.value = ''
    try {
      const data = await http.get<{ connection: ModelConnection | null }>('/connections/me')
      connection.value = data.connection
      loaded.value = true
    } catch (failure) {
      error.value = errorMessage(failure)
    } finally {
      loading.value = false
    }
  }

  async function save(payload: {
    provider: string
    base_url: string
    model: string
    display_name: string
    api_key?: string
  }): Promise<boolean> {
    loading.value = true
    error.value = ''
    notice.value = ''
    try {
      const data = await http.put<{ connection: ModelConnection }>('/connections/me', payload)
      connection.value = data.connection
      notice.value = '已保存。配置变化后旧的能力结论会立即失效，请重新检查。'
      return true
    } catch (failure) {
      error.value = errorMessage(failure)
      return false
    } finally {
      loading.value = false
    }
  }

  async function check(): Promise<boolean> {
    checking.value = true
    error.value = ''
    notice.value = ''
    try {
      const data = await http.post<{ connection: ModelConnection }>('/connections/me/check', {})
      connection.value = data.connection
      notice.value = data.connection.summary ?? '检查完成'
      return true
    } catch (failure) {
      error.value = errorMessage(failure)
      return false
    } finally {
      checking.value = false
    }
  }

  async function remove(): Promise<boolean> {
    loading.value = true
    error.value = ''
    try {
      await http.del('/connections/me')
      connection.value = null
      notice.value = '已删除个人连接'
      return true
    } catch (failure) {
      error.value = errorMessage(failure)
      return false
    } finally {
      loading.value = false
    }
  }

  function reset(): void {
    connection.value = null
    loaded.value = false
    error.value = ''
    notice.value = ''
  }

  return {
    connection,
    providers,
    loading,
    checking,
    error,
    notice,
    loaded,
    hasConnection,
    capabilitiesValid,
    loadProviders,
    load,
    save,
    check,
    remove,
    reset,
  }
})
