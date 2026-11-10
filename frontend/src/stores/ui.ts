import { defineStore } from 'pinia'
import { ref, watch } from 'vue'

export type ThemeMode = 'light' | 'dark'

const STORAGE_KEY = 'hx17-theme'
const DOCK_KEY = 'hx17-dock-collapsed'
const WIDTH_KEY = 'hx17-assistant-width'
const CHAT_FONT_KEY = 'hx17-chat-font-size'

/** 与 v1.6 一致：助手栏 260–640，默认 336。 */
export const ASSISTANT_MIN = 260
export const ASSISTANT_MAX = 640
export const ASSISTANT_DEFAULT = 336

/** 会话字号：12–24px，步长 1，默认 14（与本次改动前的外观一致）。 */
export const CHAT_FONT_MIN = 12
export const CHAT_FONT_MAX = 24
export const CHAT_FONT_DEFAULT = 14

/**
 * 本机偏好读写。隐私模式/配额满/被策略禁用时 localStorage 会直接抛错，
 * 这里统一吞掉：读不到按默认值走，写不进去就只在本次会话生效，页面不因此报错。
 */
function readLocal(key: string): string | null {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}

function writeLocal(key: string, value: string): void {
  try {
    localStorage.setItem(key, value)
  } catch {
    /* 存不下不影响本次使用 */
  }
}

/** 把任意输入收敛成 12–24 的整数；空/NaN/越界都回退到合法值，默认 14。 */
export function normaliseChatFontSize(raw: unknown): number {
  if (raw === null || raw === undefined) return CHAT_FONT_DEFAULT
  const text = typeof raw === 'string' ? raw.trim() : raw
  if (text === '') return CHAT_FONT_DEFAULT
  const value = typeof text === 'number' ? text : Number(text)
  if (!Number.isFinite(value)) return CHAT_FONT_DEFAULT
  return Math.max(CHAT_FONT_MIN, Math.min(CHAT_FONT_MAX, Math.round(value)))
}

function readChatFontSize(): number {
  return normaliseChatFontSize(readLocal(CHAT_FONT_KEY))
}

function readAssistantWidth(): number {
  const saved = parseInt(readLocal(WIDTH_KEY) ?? '', 10)
  if (!Number.isNaN(saved) && saved >= ASSISTANT_MIN && saved <= ASSISTANT_MAX) return saved
  return ASSISTANT_DEFAULT
}

function initialTheme(): ThemeMode {
  const saved = readLocal(STORAGE_KEY)
  if (saved === 'light' || saved === 'dark') return saved
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

/** 外观偏好只存本机 localStorage，不涉及账号数据。 */
export const useUiStore = defineStore('ui', () => {
  const theme = ref<ThemeMode>(initialTheme())
  /** 业务页右侧助手是否被收起。只影响这台电脑的显示，不改服务端任何设置。 */
  const dockCollapsed = ref(readLocal(DOCK_KEY) === '1')
  /** 右侧助手宽度（像素）。只存本机，不影响别人和后台。 */
  const assistantWidth = ref(readAssistantWidth())
  /** 会话字号（px）：只影响对话正文与输入区，只存本机。 */
  const chatFontSize = ref(readChatFontSize())

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

  /** 会话字号通过 CSS 变量下发，样式集中在 styles.css，不逐组件传 prop。 */
  function applyChatFontSize(value: number): void {
    document.documentElement.style.setProperty('--chat-font-size', `${value}px`)
  }

  function setChatFontSize(value: number): void {
    chatFontSize.value = normaliseChatFontSize(value)
  }

  function resetChatFontSize(): void {
    setChatFontSize(CHAT_FONT_DEFAULT)
  }

  watch(
    theme,
    (value) => {
      apply(value)
      writeLocal(STORAGE_KEY, value)
    },
    { immediate: true },
  )

  watch(dockCollapsed, (value) => {
    writeLocal(DOCK_KEY, value ? '1' : '0')
  })

  watch(assistantWidth, (value) => {
    writeLocal(WIDTH_KEY, String(value))
  })

  watch(
    chatFontSize,
    (value) => {
      applyChatFontSize(value)
      writeLocal(CHAT_FONT_KEY, String(value))
    },
    { immediate: true },
  )

  return {
    theme,
    setTheme,
    toggleTheme,
    dockCollapsed,
    toggleDock,
    assistantWidth,
    setAssistantWidth,
    resetAssistantWidth,
    chatFontSize,
    setChatFontSize,
    resetChatFontSize,
  }
})
