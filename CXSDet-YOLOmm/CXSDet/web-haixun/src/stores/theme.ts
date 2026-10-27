import { defineStore } from 'pinia'
import { ref } from 'vue'

export type Theme = 'light' | 'dark'

const THEME_KEY = 'haixun.theme'

function readStoredTheme(): Theme {
  try {
    // index.html 已同步设置了 data-theme（含 URL 参数覆盖逻辑），以 DOM 现状为准
    const dom = document.documentElement.getAttribute('data-theme')
    if (dom === 'dark' || dom === 'light') return dom
    const t = localStorage.getItem(THEME_KEY)
    return t === 'dark' ? 'dark' : 'light'
  } catch {
    return 'light'
  }
}

export const useThemeStore = defineStore('theme', () => {
  const theme = ref<Theme>(readStoredTheme())

  function apply(t: Theme) {
    theme.value = t
    document.documentElement.setAttribute('data-theme', t)
    try {
      localStorage.setItem(THEME_KEY, t)
    } catch {
      /* ignore */
    }
  }

  function toggle() {
    apply(theme.value === 'light' ? 'dark' : 'light')
  }

  // 初始化时同步 DOM
  apply(theme.value)

  return { theme, apply, toggle }
})
