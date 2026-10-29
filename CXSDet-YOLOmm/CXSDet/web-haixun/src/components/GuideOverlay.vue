<script setup lang="ts">
import { ref, computed, watch, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { useGuideStore } from '@/stores/guide'

const guide = useGuideStore()

interface GuideStep {
  title: string
  desc: string
  target: string // data-guide 选择器
  anchor: 'top' | 'bottom' | 'left' | 'right' | 'center'
}

const steps: GuideStep[] = [
  {
    title: '先选择你的工作区',
    desc: '在左上角新建或切换个人工作区。影像、会话、任务和统计都归属于所选工作区，不同项目的数据互不混淆。',
    target: '[data-guide="workspace"]',
    anchor: 'bottom',
  },
  {
    title: '连接你的海巡助手',
    desc: '在「扩展与连接」中配置助手使用的大模型。这是个人大模型连接，可跳过，且与检测模型相互独立。',
    target: '[data-guide="extensions"]',
    anchor: 'left',
  },
  {
    title: '发起一次叶片巡检',
    desc: '点击「新建巡检」，选择影像、目标项目与检测模型。选择影像并不等于已上传，执行前会先核对计划。',
    target: '[data-guide="new-inspection"]',
    anchor: 'bottom',
  },
  {
    title: '先核对计划，再确认执行',
    desc: '右侧会展示检测计划，确认目标项目、影像数量与模型版本后，再决定确认、调整或拒绝。',
    target: '[data-guide="assistant"]',
    anchor: 'left',
  },
  {
    title: '回到原图完成人工复核',
    desc: '检测结果进入人工复核队列。到「人工复核」页面，在原图上修正类别、调整或补绘检测框，确认无缺陷后提交。',
    target: '[data-guide="review"]',
    anchor: 'left',
  },
]

const stepIdx = computed(() => guide.step)
const currentStep = computed(() => steps[stepIdx.value])
const rect = ref<{ top: number; left: number; width: number; height: number } | null>(null)
const cardPos = ref<{ top: number; left: number }>({ top: 0, left: 0 })

const CARD_W = 340
const PADDING = 16

function computeRect() {
  const step = currentStep.value
  if (!step) return
  const el = document.querySelector(step.target) as HTMLElement | null
  if (!el || el.offsetParent === null) {
    // 目标不可见（折叠等）：退化为居中卡片
    rect.value = null
    placeCardCenter()
    return
  }
  const r = el.getBoundingClientRect()
  rect.value = { top: r.top, left: r.left, width: r.width, height: r.height }
  placeCardNear(r, step.anchor)
}

function placeCardNear(r: DOMRect, anchor: GuideStep['anchor']) {
  const vw = window.innerWidth
  const vh = window.innerHeight
  let top = r.top
  let left = r.left
  switch (anchor) {
    case 'bottom':
      top = r.bottom + PADDING
      left = r.left
      break
    case 'top':
      top = r.top - PADDING
      left = r.left
      break
    case 'left':
      top = r.top
      left = r.left - CARD_W - PADDING
      break
    case 'right':
      top = r.top
      left = r.right + PADDING
      break
    case 'center':
      top = r.top + r.height / 2
      left = r.left + r.width / 2
      break
  }
  // 约束在屏幕内
  left = Math.max(PADDING, Math.min(left, vw - CARD_W - PADDING))
  top = Math.max(PADDING, Math.min(top, vh - 260))
  cardPos.value = { top, left }
}

function placeCardCenter() {
  cardPos.value = {
    top: Math.max(PADDING, window.innerHeight / 2 - 150),
    left: Math.max(PADDING, window.innerWidth / 2 - CARD_W / 2),
  }
}

function update() {
  if (guide.active) computeRect()
}

watch(
  () => [guide.step, guide.active],
  () => nextTick(update),
)

onMounted(() => {
  window.addEventListener('resize', update)
  window.addEventListener('scroll', update, true)
  document.addEventListener('keydown', onKey)
})
onBeforeUnmount(() => {
  window.removeEventListener('resize', update)
  window.removeEventListener('scroll', update, true)
  document.removeEventListener('keydown', onKey)
})

function onKey(e: KeyboardEvent) {
  if (!guide.active) return
  if (e.key === 'Escape') {
    e.preventDefault()
    guide.close()
  } else if (e.key === 'ArrowRight') {
    guide.next()
  } else if (e.key === 'ArrowLeft') {
    guide.prev()
  }
}

const isLast = computed(() => stepIdx.value === steps.length - 1)
</script>

<template>
  <div v-if="guide.active" class="guide-root" role="dialog" aria-modal="true" :aria-label="`引导 ${stepIdx + 1}/${steps.length}`">
    <!-- 聚光遮罩：有目标时由 hole 的 box-shadow 形成遮罩；无目标时用全屏 scrim -->
    <div v-if="!rect" class="guide-scrim"></div>
    <div
      v-if="rect"
      class="guide-hole"
      :style="{
        top: rect.top + 'px',
        left: rect.left + 'px',
        width: rect.width + 'px',
        height: rect.height + 'px',
      }"
      aria-hidden="true"
    ></div>

    <!-- 引导卡片 -->
    <div
      class="guide-card panel"
      :style="{ top: cardPos.top + 'px', left: cardPos.left + 'px' }"
    >
      <button class="guide-close" aria-label="关闭引导" @click="guide.close">
        <svg viewBox="0 0 16 16" width="14" height="14" stroke="currentColor" stroke-width="1.6" fill="none">
          <path d="M3 3l10 10M13 3L3 13" />
        </svg>
      </button>
      <div class="guide-progress">
        <span class="num">{{ stepIdx + 1 }}</span> / <span class="num">{{ steps.length }}</span>
      </div>
      <h3 class="guide-title">{{ currentStep.title }}</h3>
      <p class="guide-desc">{{ currentStep.desc }}</p>
      <div class="guide-actions">
        <button class="btn btn-ghost btn-sm" @click="guide.skip">跳过引导</button>
        <div class="guide-nav">
          <button class="btn btn-sm" :disabled="stepIdx === 0" @click="guide.prev">上一步</button>
          <button class="btn btn-primary btn-sm" @click="guide.next">
            {{ isLast ? '完成' : '下一步' }}
          </button>
        </div>
      </div>
      <!-- 步骤指示点 -->
      <div class="guide-dots" aria-hidden="true">
        <span
          v-for="i in steps.length"
          :key="i"
          class="dot"
          :class="{ active: i - 1 === stepIdx }"
        ></span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.guide-root {
  position: fixed;
  inset: 0;
  z-index: 1000;
}
.guide-scrim {
  position: absolute;
  inset: 0;
  background: var(--scrim);
}
/* 聚光孔：用 box-shadow 挖空 */
.guide-hole {
  position: absolute;
  border-radius: var(--radius-md);
  box-shadow: 0 0 0 9999px var(--scrim);
  z-index: 1001;
  pointer-events: none;
}
.guide-card {
  position: fixed;
  width: 340px;
  max-width: calc(100vw - 32px);
  z-index: 1002;
  box-shadow: var(--shadow-md);
  padding: 20px;
  border-radius: var(--radius-md);
}
.guide-close {
  position: absolute;
  top: 12px;
  right: 12px;
  width: 26px;
  height: 26px;
  border: none;
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  border-radius: var(--radius-sm);
  display: flex;
  align-items: center;
  justify-content: center;
}
.guide-close:hover {
  background: var(--bg-selected);
}
.guide-progress {
  font-size: 12px;
  color: var(--text-secondary);
  margin-bottom: 10px;
}
.guide-title {
  font-size: 16px;
  font-weight: 600;
  margin: 0 0 8px;
  padding-right: 24px;
  color: var(--text);
}
.guide-desc {
  font-size: 13px;
  line-height: 1.6;
  color: var(--text-secondary);
  margin: 0 0 16px;
}
.guide-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.guide-nav {
  display: flex;
  gap: 8px;
}
.guide-dots {
  display: flex;
  gap: 6px;
  margin-top: 16px;
  justify-content: center;
}
.dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--border-control);
}
.dot.active {
  background: var(--accent);
  width: 16px;
  border-radius: 3px;
}
</style>
