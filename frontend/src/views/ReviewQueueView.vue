<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import * as api from '@/api/business'
import { errorMessage } from '@/api/http'
import type { DetectionRecord } from '@/types'

/** 本轮人工队列：从真实记录/复核服务读取，按 flow_id 或指定 record_ids 定位。 */
const route = useRoute()
const router = useRouter()
const projectId = computed(() => String(route.params.projectId ?? ''))
const flowId = computed(() => String(route.query.flow ?? ''))
const view = ref<'pending' | 'draft' | 'rejected' | 'confirmed'>('pending')
const items = ref<DetectionRecord[]>([])
const counts = ref<Record<string, number>>({})
const flowIds = ref<string[]>([])
const selected = ref<string[]>([])
const error = ref('')
const loading = ref(false)

const requestedIds = computed(() =>
  String(route.query.record_ids ?? '')
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean),
)
const visible = computed(() =>
  flowId.value ? items.value.filter((item) => item.flow_id === flowId.value) : items.value,
)

function label(bucket: string): string {
  return { pending: '待复核', draft: '有草稿', rejected: '已驳回', confirmed: '已确认' }[bucket] ?? bucket
}

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const data = await api.listReviewQueue(projectId.value, {
      view: view.value,
      record_ids: requestedIds.value.length ? requestedIds.value : undefined,
      limit: 100,
    })
    items.value = data.items
    counts.value = data.counts
    flowIds.value = data.flow_ids
  } catch (failure) {
    error.value = errorMessage(failure)
  } finally {
    loading.value = false
  }
}

function open(recordId: string): void {
  void router.push({ name: 'review', params: { projectId: projectId.value, recordId } })
}

function withFlow(id: string): void {
  void router.push({ name: 'reviewQueue', params: { projectId: projectId.value }, query: { flow: id } })
}

onMounted(load)
watch([view, projectId, flowId, requestedIds], load)
</script>

<template>
  <section class="page">
    <header class="page-header">
      <div>
        <h1>人工复核队列</h1>
        <p class="subtitle">
          <span v-if="flowId">本轮批次 <span class="mono">{{ flowId }}</span></span>
          <span v-else>当前项目的真实记录</span>
          <span class="hint"> · AI 核查结论只作参考，人工状态独立</span>
        </p>
      </div>
      <div class="header-actions">
        <button class="ghost small" type="button" :disabled="loading" @click="load">刷新</button>
      </div>
    </header>

    <p v-if="error" class="banner banner-error" role="alert">{{ error }}</p>

    <div class="inline-form queue-tabs">
      <button
        v-for="bucket in (['pending', 'draft', 'rejected', 'confirmed'] as const)"
        :key="bucket"
        :class="view === bucket ? 'primary small' : 'ghost small'"
        type="button"
        @click="view = bucket"
      >
        {{ label(bucket) }}（{{ counts[bucket] ?? 0 }}）
      </button>
      <span class="hint">已选 {{ selected.length }} 条</span>
    </div>

    <div v-if="flowIds.length" class="card">
      <p class="card-head">本项目的巡检批次</p>
      <button
        v-for="id in flowIds"
        :key="id"
        class="link small"
        type="button"
        @click="withFlow(id)"
      >{{ id }}</button>
    </div>

    <table v-if="visible.length" class="table">
      <thead>
        <tr>
          <th></th><th>记录</th><th>批次</th><th>命中</th><th>类别</th><th>检测时间</th><th>AI 建议</th><th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="record in visible" :key="record.id">
          <td><input v-model="selected" type="checkbox" :value="record.id" /></td>
          <td class="mono">{{ record.id }}</td>
          <td class="mono">{{ record.flow_id || '—' }}</td>
          <td>{{ record.hit_count }}</td>
          <td>{{ Object.keys(record.class_summary).join('、') || '—' }}</td>
          <td>{{ record.created_at_local }}</td>
          <td>{{ record.latest_review?.verdict || '未核查' }}</td>
          <td><button class="ghost small" type="button" @click="open(record.id)">开始复核</button></td>
        </tr>
      </tbody>
    </table>
    <p v-else-if="!loading" class="empty-hint">这个视图里没有记录。</p>
  </section>
</template>

<style scoped>
.queue-tabs { flex-wrap: wrap; margin-bottom: 12px; }
</style>
