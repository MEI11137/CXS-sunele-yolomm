<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useGuideStore } from '@/stores/guide'
import { getAdapter } from '@/services'
import type { ManualInfo } from '@/services'
import DocumentLibrary from '@/components/DocumentLibrary.vue'
import { useResourceRefresh } from '@/utils/resourceLifecycle'

const guide = useGuideStore()

// 版本号与手册清单都来自后端，不再写死
const version = ref('')
const manuals = ref<ManualInfo[]>([])
const loading = ref(false)
const loadError = ref('')

async function load() {
  loading.value = true
  try {
    const [overview, list] = await Promise.all([
      getAdapter().getOverview(),
      getAdapter().listManuals(),
    ])
    version.value = overview.version
    manuals.value = list
  } catch (e) {
    loadError.value = '读取手册失败：' + (e as Error).message
  } finally {
    loading.value = false
  }
}
onMounted(load)
useResourceRefresh(load)
</script>

<template>
  <div class="page">
    <DocumentLibrary />
    <div class="page-header">
      <h1 class="page-title">资料与手册</h1>
    </div>

    <section class="section">
      <h2 class="section-title">当前版本说明</h2>
      <div class="panel manual-card">
        <h3 class="manual-title">海巡智检 · {{ version ? 'v' + version : '（版本读取中）' }}</h3>
        <p class="manual-desc">
          海上风电叶片巡检工作平台，面向影像巡检、检测计划确认、缺陷人工复核与项目资源管理。
        </p>
        <ul class="manual-list">
          <li><strong>工作区</strong>：影像、会话、任务、记录和统计按项目归属。</li>
          <li><strong>图片巡检</strong>：选择影像、指定检测模型，先核对计划再执行。</li>
          <li><strong>人工复核</strong>：在原图上修正类别、调整检测框，确认无缺陷后提交。</li>
          <li><strong>海巡助手</strong>：右侧全局对话栏，可发起巡检、核对计划、处理复核。</li>
        </ul>
      </div>
    </section>

    <section class="section">
      <h2 class="section-title">使用引导</h2>
      <div class="panel manual-card">
        <p class="manual-desc">首次进入会自动展示五步引导。如需再次查看：</p>
        <button class="btn" @click="guide.replay">重新播放引导</button>
      </div>
    </section>

    <section class="section">
      <h2 class="section-title">手册资料</h2>
      <div class="panel manual-card">
        <div v-if="loading" class="empty"><div class="empty-desc">正在读取手册…</div></div>
        <div v-else-if="loadError" class="empty">
          <div class="empty-title">读取失败</div>
          <div class="empty-desc">{{ loadError }}</div>
        </div>
        <div v-else-if="!manuals.length" class="empty">
          <div class="empty-title">暂无可用手册</div>
          <div class="empty-desc">后端尚未返回任何内置或自建手册。</div>
        </div>
        <table v-else class="data-table">
          <thead>
            <tr><th>名称</th><th>版本</th><th>来源</th><th>摘要</th></tr>
          </thead>
          <tbody>
            <tr v-for="m in manuals" :key="m.id">
              <td>{{ m.name }}</td>
              <td class="num">v{{ m.version }}</td>
              <td><span class="tag">{{ m.source || '内置' }}</span></td>
              <td class="hint">{{ m.summary || '—' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </div>
</template>

<style scoped>
.page {
  padding: var(--content-pad);
  max-width: 800px;
  margin: 0 auto;
}
.page-header {
  margin-bottom: 20px;
}
.page-title {
  font-size: 24px;
  font-weight: 600;
  margin: 0;
}
.section {
  margin-bottom: 24px;
}
.section-title {
  font-size: 16px;
  font-weight: 600;
  margin: 0 0 12px;
}
.manual-card {
  padding: 20px;
}
.manual-title {
  font-size: 16px;
  font-weight: 600;
  margin: 0 0 12px;
}
.manual-desc {
  font-size: 14px;
  line-height: 1.7;
  color: var(--text-secondary);
  margin: 0 0 12px;
}
.manual-list {
  margin: 0;
  padding-left: 18px;
  font-size: 13px;
  line-height: 1.8;
  color: var(--text);
}
</style>
