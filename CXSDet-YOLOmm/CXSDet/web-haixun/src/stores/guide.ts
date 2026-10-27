import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

const GUIDE_KEY = 'haixun.guide.seen'

function readSeen(): boolean {
  try {
    // URL 参数 guide=off 可跳过自动引导（用于演示/测试）
    if (new URLSearchParams(window.location.search).get('guide') === 'off') return true
    return localStorage.getItem(GUIDE_KEY) === '1'
  } catch {
    return false
  }
}

export const useGuideStore = defineStore('guide', () => {
  // 是否已看过（按当前浏览器/用户维度持久化）
  const seen = ref(readSeen())
  // 当前是否正在展示（重播时临时置真，不写持久）
  const active = ref(false)
  const step = ref(0)

  const totalSteps = 5

  const shouldAutoShow = computed(() => {
    try {
      const qs = new URLSearchParams(window.location.search)
      // URL 参数 guide=on 强制重播（用于演示/测试）
      if (qs.get('guide') === 'on') return true
      if (qs.get('guide') === 'off') return false
    } catch {
      /* ignore */
    }
    return !seen.value
  })

  function initFromUrl() {
    try {
      const qs = new URLSearchParams(window.location.search)
      const s = parseInt(qs.get('guideStep') ?? '', 10)
      if (!Number.isNaN(s) && s >= 0 && s < totalSteps) {
        step.value = s
        active.value = true
        return true
      }
    } catch {
      /* ignore */
    }
    return false
  }

  function markSeen() {
    seen.value = true
    try {
      localStorage.setItem(GUIDE_KEY, '1')
    } catch {
      /* ignore */
    }
  }

  function open(auto = false) {
    active.value = true
    step.value = 0
    // 自动弹出首次，关闭时标记
    if (auto) {
      // 首次进入：一旦开始展示就标记为看过，避免刷新重复弹出
      markSeen()
    }
  }

  function close() {
    active.value = false
    markSeen()
  }

  function next() {
    if (step.value < totalSteps - 1) step.value++
    else close()
  }

  function prev() {
    if (step.value > 0) step.value--
  }

  function skip() {
    close()
  }

  function replay() {
    open(false)
  }

  return {
    seen,
    active,
    step,
    totalSteps,
    shouldAutoShow,
    initFromUrl,
    open,
    close,
    next,
    prev,
    skip,
    replay,
  }
})
