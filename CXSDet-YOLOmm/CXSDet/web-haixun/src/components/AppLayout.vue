<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import SideNav from './SideNav.vue'
import AssistantPanel from './AssistantPanel.vue'
import GuideOverlay from './GuideOverlay.vue'
import AuthModal from './AuthModal.vue'

const assistantOpen = ref(true)
const isMobile = ref(false)

// 助手栏可拖拽宽度
const ASSISTANT_MIN = 260
const ASSISTANT_MAX = 640
const ASSISTANT_DEFAULT = 336
const ASSISTANT_KEY = 'haixun.assistantWidth'

function readWidth(): number {
  try {
    const v = parseInt(localStorage.getItem(ASSISTANT_KEY) || '', 10)
    if (!Number.isNaN(v) && v >= ASSISTANT_MIN && v <= ASSISTANT_MAX) return v
  } catch {
    /* ignore */
  }
  return ASSISTANT_DEFAULT
}

const assistantWidth = ref(readWidth())
const dragging = ref(false)

function updateViewport() {
  isMobile.value = window.innerWidth <= 768
}

function startDrag(e: MouseEvent) {
  if (!assistantOpen.value) return
  dragging.value = true
  document.body.style.userSelect = 'none'
  document.body.style.cursor = 'col-resize'
  e.preventDefault()
}

function onDrag(e: MouseEvent) {
  if (!dragging.value) return
  // 从右边缘向左拖：宽度 = 窗口宽 - 鼠标X
  const w = window.innerWidth - e.clientX
  assistantWidth.value = Math.max(ASSISTANT_MIN, Math.min(ASSISTANT_MAX, w))
}

function endDrag() {
  if (!dragging.value) return
  dragging.value = false
  document.body.style.userSelect = ''
  document.body.style.cursor = ''
  try {
    localStorage.setItem(ASSISTANT_KEY, String(assistantWidth.value))
  } catch {
    /* ignore */
  }
}

function resetWidth() {
  assistantWidth.value = ASSISTANT_DEFAULT
  try {
    localStorage.setItem(ASSISTANT_KEY, String(ASSISTANT_DEFAULT))
  } catch {
    /* ignore */
  }
}

onMounted(() => {
  updateViewport()
  window.addEventListener('resize', updateViewport)
  window.addEventListener('mousemove', onDrag)
  window.addEventListener('mouseup', endDrag)
})
onBeforeUnmount(() => {
  window.removeEventListener('resize', updateViewport)
  window.removeEventListener('mousemove', onDrag)
  window.removeEventListener('mouseup', endDrag)
})

const assistantStyle = computed(() => ({
  width: (assistantOpen.value ? assistantWidth.value : 44) + 'px',
}))
</script>

<template>
  <div class="app-shell">
    <SideNav v-if="!isMobile" />
    <SideNav v-else class="mobile-nav" :mobile="true" />

    <main class="layout-main">
      <div class="content-scroll">
        <router-view v-slot="{ Component }">
          <keep-alive>
            <component :is="Component" />
          </keep-alive>
        </router-view>
      </div>
    </main>

    <template v-if="!isMobile">
      <!-- 拖拽分隔条（助手栏展开时显示） -->
      <div
        v-if="assistantOpen"
        class="resizer"
        :class="{ dragging }"
        role="separator"
        aria-orientation="vertical"
        aria-label="拖动调整助手栏宽度"
        @mousedown="startDrag"
        @dblclick="resetWidth"
        title="拖动调整宽度，双击恢复默认"
      ></div>
      <AssistantPanel
        :open="assistantOpen"
        :style="assistantStyle"
        @toggle="assistantOpen = !assistantOpen"
      />
    </template>
    <AssistantPanel v-else class="mobile-assistant" :open="false" :mobile="true" />

    <GuideOverlay />
    <AuthModal />
  </div>
</template>

<style scoped>
.app-shell {
  display: flex;
  height: 100vh;
  overflow: hidden;
}

.layout-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}

.content-scroll {
  flex: 1;
  overflow-y: auto;
  overflow-x: hidden;
}

.resizer {
  width: 5px;
  flex-shrink: 0;
  cursor: col-resize;
  background: transparent;
  position: relative;
  z-index: 5;
}
.resizer::after {
  content: '';
  position: absolute;
  top: 0;
  bottom: 0;
  left: 2px;
  width: 1px;
  background: var(--border);
  transition: background 0.12s, width 0.12s;
}
.resizer:hover::after,
.resizer.dragging::after {
  background: var(--accent);
  width: 2px;
  left: 1px;
}

@media (max-width: 768px) {
  .app-shell {
    flex-direction: column;
  }
  .layout-main {
    flex: 1;
    min-height: 0;
  }
}
</style>
