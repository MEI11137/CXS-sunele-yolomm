import { defineStore } from 'pinia'
import { ref, watch } from 'vue'

export type ThemeMode = 'light' | 'dark'

const STORAGE_KEY = 'hx17-theme'

function initialTheme(): ThemeMode {
  const saved = localStorage.getItem(STORAGE_KEY)
  if (saved === 'light' || saved === 'dark') return saved
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

/** 外观偏好只存本机 localStorage，不涉及账号数据。 */
export const useUiStore = defineStore('ui', () => {
  const theme = ref<ThemeMode>(initialTheme())

  function apply(value: ThemeMode): void {
    document.documentElement.dataset.theme = value
  }

  function setTheme(value: ThemeMode): void {
    theme.value = value
  }

  function toggleTheme(): void {
    theme.value = theme.value === 'dark' ? 'light' : 'dark'
  }

  watch(
    theme,
    (value) => {
      apply(value)
      localStorage.setItem(STORAGE_KEY, value)
    },
    { immediate: true },
  )

  return { theme, setTheme, toggleTheme }
})
