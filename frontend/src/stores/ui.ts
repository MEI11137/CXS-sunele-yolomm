import { defineStore } from 'pinia'
import { ref, watch } from 'vue'

export type ThemeMode = 'light' | 'dark'

const STORAGE_KEY = 'hx17-theme'
const DOCK_KEY = 'hx17-dock-collapsed'
const WIDTH_KEY = 'hx17-assistant-width'

/** 与 v1.6 一致：助手栏 260–640，默认 336。 */
export const ASSISTANT_MIN = 260
export const ASSISTANT_MAX = 640
export const ASSISTANT_DEFAULT = 336

function readAssistantWidth(): number {
  const saved = parseInt(localStorage.getItem(WIDTH_KEY) ?? '', 10)
  if (!Number.isNaN(saved) && saved >= ASSISTANT_MIN && saved <= ASSISTANT_MAX) return saved
  return ASSISTANT_DEFAULT
}

function initialTheme(): ThemeMode {
  const saved = localStorage.getItem(STORAGE_KEY)
  if (saved === 'light' || saved === 'dark') return saved
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

/** 外观偏好只存本机 localStorage，不涉及账号数据。 */
export const useUiStore = defineStore('ui', () => {
  const theme = ref<ThemeMode>(initialTheme())
  /** 业务页右侧助手是否被收起。只影响这台电脑的显示，不改服务端任何设置。 */
  const dockCollapsed = ref(localStorage.getItem(DOCK_KEY) === '1')
  /** 右侧助手宽度（像素）。只存本机，不影响别人和后台。 */
  const assistantWidth = ref(readAssistantWidth())

  function apply(value: ThemeMode): void {
    document.documentElement.dataset.theme = value
  }

  function setTheme(value: ThemeMode): void {
    theme.value = value
  }

  function toggleTheme(): void {
    theme.value = theme.value === 'dark' ? 'light' : 'dark'
  }

  function toggleDock(): void {
    dockCollapsed.value = !dockCollapsed.value
  }

  function setAssistantWidth(value: number): void {
    const width = Math.max(ASSISTANT_MIN, Math.min(ASSISTANT_MAX, Math.round(value)))
    assistantWidth.value = width
  }

  function resetAssistantWidth(): void {
    setAssistantWidth(ASSISTANT_DEFAULT)
  }

  watch(
    theme,
    (value) => {
      apply(value)
      localStorage.setItem(STORAGE_KEY, value)
    },
    { immediate: true },
  )

  watch(dockCollapsed, (value) => {
    localStorage.setItem(DOCK_KEY, value ? '1' : '0')
  })

  watch(assistantWidth, (value) => {
    localStorage.setItem(WIDTH_KEY, String(value))
  })

  return {
    theme,
    setTheme,
    toggleTheme,
    dockCollapsed,
    toggleDock,
    assistantWidth,
    setAssistantWidth,
    resetAssistantWidth,
  }
})
