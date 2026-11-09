<script setup lang="ts">
import { onBeforeUnmount, watch } from 'vue'
import { isTopDialog, registerDialog, unregisterDialog } from '@/components/dialogStack'

const props = defineProps<{ title: string; open: boolean }>()
const emit = defineEmits<{ (event: 'close'): void }>()

let dialogId = 0

function onKeydown(event: KeyboardEvent): void {
  // 只在最上层响应：嵌套弹窗按 Esc 时先关小的，不连关外层。
  if (event.key !== 'Escape' || !props.open || !isTopDialog(dialogId)) return
  event.preventDefault()
  event.stopPropagation()
  emit('close')
}

watch(
  () => props.open,
  (open) => {
    if (typeof window === 'undefined') return
    if (open) {
      dialogId = registerDialog()
      window.addEventListener('keydown', onKeydown)
    } else {
      window.removeEventListener('keydown', onKeydown)
      unregisterDialog(dialogId)
    }
  },
  { immediate: true },
)

onBeforeUnmount(() => {
  if (typeof window === 'undefined') return
  window.removeEventListener('keydown', onKeydown)
  unregisterDialog(dialogId)
})
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="modal-backdrop" @click.self="emit('close')">
      <div class="modal" role="dialog" aria-modal="true" :aria-label="title">
        <header class="modal-header">
          <h2>{{ title }}</h2>
          <button class="link" type="button" @click="emit('close')">关闭</button>
        </header>
        <div class="modal-body">
          <slot />
        </div>
        <footer v-if="$slots.footer" class="modal-footer">
          <slot name="footer" />
        </footer>
      </div>
    </div>
  </Teleport>
</template>