<script setup lang="ts">
/**
 * 侧栏用的 20 格线性图标。
 *
 * 为什么单独做一个小组件：v1.7 没有引入图标库，侧栏要用的图标只有十来个，
 * 直接内联 SVG 会在 SidebarNav 模板里重复十几段路径；集中在这里按名字取用，
 * 尺寸、线宽与颜色继承保持一致，也方便以后换主题色。
 *
 * @param name 图标名；未命中时渲染空 SVG（不报错，避免小写拼错把整栏搞崩）
 * @param size 边长（px），默认 16
 */
withDefaults(defineProps<{ name: string; size?: number }>(), { size: 16 })
</script>

<template>
  <svg
    :width="size"
    :height="size"
    viewBox="0 0 20 20"
    fill="none"
    stroke="currentColor"
    stroke-width="1.5"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
    focusable="false"
  >
    <template v-if="name === 'folder'">
      <path d="M2.9 6.1c0-.7.5-1.2 1.2-1.2h3.5l1.7 1.8h5.6c.7 0 1.2.5 1.2 1.2v6.2c0 .7-.5 1.2-1.2 1.2H4.1c-.7 0-1.2-.5-1.2-1.2z" />
    </template>
    <template v-else-if="name === 'chevron'">
      <path d="M7.6 5.2 13 10l-5.4 4.8z" fill="currentColor" stroke="none" />
    </template>
    <template v-else-if="name === 'search'">
      <circle cx="8.7" cy="8.7" r="4.4" />
      <path d="m12.1 12.1 4.4 4.4" />
    </template>
    <template v-else-if="name === 'sort'">
      <path d="M3.4 6.4h5.2" />
      <path d="M12.2 6.4h4.4" />
      <circle cx="10.4" cy="6.4" r="1.5" />
      <path d="M3.4 13.6h3.1" />
      <path d="M10.2 13.6h6.4" />
      <circle cx="8.3" cy="13.6" r="1.5" />
    </template>
    <template v-else-if="name === 'folder-plus'">
      <path d="M2.9 6.1c0-.7.5-1.2 1.2-1.2h3.2l1.6 1.8h5.9c.7 0 1.2.5 1.2 1.2v6.2c0 .7-.5 1.2-1.2 1.2H4.1c-.7 0-1.2-.5-1.2-1.2z" />
      <path d="M10 9.3v3.6M8.2 11.1h3.6" />
    </template>
    <template v-else-if="name === 'dots'">
      <circle cx="4.9" cy="10" r="1.15" fill="currentColor" stroke="none" />
      <circle cx="10" cy="10" r="1.15" fill="currentColor" stroke="none" />
      <circle cx="15.1" cy="10" r="1.15" fill="currentColor" stroke="none" />
    </template>
    <template v-else-if="name === 'chat-plus'">
      <rect x="3.4" y="3.9" width="13.2" height="12.2" rx="3.2" />
      <path d="M10 7v6M7 10h6" />
    </template>
    <template v-else-if="name === 'trash'">
      <path d="M3.9 6.2h12.2" />
      <path d="M8.1 6.2V4.9c0-.5.4-.9.9-.9h2c.5 0 .9.4.9.9v1.3" />
      <path d="M5.6 6.2l.7 8.1c0 .6.5 1.1 1.1 1.1h5.2c.6 0 1.1-.5 1.1-1.1l.7-8.1" />
    </template>
    <template v-else-if="name === 'gear'">
      <circle cx="10" cy="10" r="2.4" />
      <path d="M10 3.1v1.7M10 15.2v1.7M3.1 10h1.7M15.2 10h1.7M5.1 5.1l1.2 1.2M13.7 13.7l1.2 1.2M14.9 5.1l-1.2 1.2M6.3 13.7l-1.2 1.2" />
    </template>
    <template v-else-if="name === 'shield'">
      <path d="M10 3.1 15.7 5v4.4c0 3.3-2.2 5.5-5.7 6.6-3.5-1.1-5.7-3.3-5.7-6.6V5z" />
    </template>
    <template v-else-if="name === 'logout'">
      <path d="M12.4 6.1V4.7c0-.6-.5-1.1-1.1-1.1H5.2c-.6 0-1.1.5-1.1 1.1v10.6c0 .6.5 1.1 1.1 1.1h6.1c.6 0 1.1-.5 1.1-1.1v-1.4" />
      <path d="M8.7 10h8.2M14.2 7.3 16.9 10l-2.7 2.7" />
    </template>
  </svg>
</template>