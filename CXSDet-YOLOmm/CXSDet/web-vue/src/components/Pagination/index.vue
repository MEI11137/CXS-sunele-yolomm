<!-- MD5: 58c3486135ab8ce63bf42b5751bd1772 -->
<!--
版权所有 © 2025 羊羊小栈 (GJQ)

作者: 羊羊小栈
时间: 2025-09-06 23:44:27

本系统为原创作品，禁止二次销售！禁止将系统对应的视频、文档进行二次发布！
违者需立即停止侵权行为，并按照【羊羊小栈系统版权声明及保护条款】中规定数额进行赔偿，并承担相应法律责任。
-->

<template>
  <div class="pagination-container">
    <el-pagination
      v-model:current-page="currentPage"
      v-model:page-size="pageSize"
      :page-sizes="[10, 20, 50, 100]"
      :layout="'total, sizes, prev, pager, next, jumper'"
      :total="total"
      @size-change="handleSizeChange"
      @current-change="handleCurrentChange"
    />
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps({
  current: {
    type: Number,
    default: 1
  },
  size: {
    type: Number,
    default: 10
  },
  total: {
    type: Number,
    default: 0
  }
})

const emit = defineEmits(['update:current', 'update:size', 'change'])

// 计算属性用于双向绑定
const currentPage = computed({
  get: () => props.current,
  set: (val: number) => emit('update:current', val)
})

const pageSize = computed({
  get: () => props.size,
  set: (val: number) => emit('update:size', val)
})

// 页码变化事件
const handleCurrentChange = (val: number) => {
  emit('change')
}

// 每页条数变化事件
const handleSizeChange = (val: number) => {
  emit('change')
}
</script>

<style lang="scss" scoped>
.pagination-container {
  margin-top: 20px;
  display: flex;
  justify-content: center;
}
</style> 