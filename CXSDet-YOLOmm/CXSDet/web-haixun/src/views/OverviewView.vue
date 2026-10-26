<script setup lang="ts">
import { computed, ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useWorkspaceStore } from '@/stores/workspace'
import { useInspectionStore } from '@/stores/inspection'
import { getAdapter } from '@/services'
import { mockTurbines } from '@/services/mock-data'
import { IMAGES, bladeImageFor } from '@/assets/images'

const router = useRouter()
const workspace = useWorkspaceStore()
const inspection = useInspectionStore()

// 数量全部来自真实接口，不再用固定台数推算
const realStats = ref({ records: 0, models: 0, datasets: 0, loaded: false })

onMounted(async () => {
  try {
    const [r, m, d] = await Promise.all([
      getAdapter().listRecords({ page: 1, pageSize: 1 }),
      getAdapter().listModels({ page: 1, pageSize: 50 }),
      getAdapter().listDatasets({ page: 1, pageSize: 50 }),
    ])
    realStats.value = {
      records: r.total,
      models: m.items.filter((x) => x.status === 'available').length,
      datasets: d.total,
      loaded: true,
    }
  } catch {
    realStats.value = { records: 0, models: 0, datasets: 0, loaded: false }
  }
})

const stats = computed(() => {
  const defects = inspection.defects
  const pending = defects.filter((d) => d.reviewStatus === 'pending').length
  const available = inspection.models.filter((m) => m.status === 'available')
  const current = available[0]
  return {
    // 真实统计
    records: realStats.value.records,
    pendingReview: pending,
    models: realStats.value.models,
    datasets: realStats.value.datasets,
    currentModel: current ? `${current.name} ${current.version}` : '未发布',
  }
})

const selectedTurbine = ref<string | null>(null)

const pendingDefects = computed(() =>
  inspection.defects.filter((d) => d.reviewStatus === 'pending'),
)

function selectTurbine(id: string) {
  selectedTurbine.value = id === selectedTurbine.value ? null : id
}

function goReview(defectId: string) {
  router.push(`/review/${defectId}`)
}

function goNewInspection() {
  router.push('/inspection')
}

function formatConfidence(c: number) {
  return (c * 100).toFixed(0) + '%'
}
</script>

<template>
  <div class="page">
    <!-- 面包屑：当前项目路径 -->
    <div class="breadcrumb">
      <span>海巡智检</span>
      <span class="sep">/</span>
      <span class="current">{{ workspace.current?.name ?? '未选择工作区' }}</span>
    </div>

    <!-- 标题 + 新建巡检 -->
    <div class="page-header">
      <h1 class="page-title">风场巡检工作台</h1>
      <button class="btn btn-primary" data-guide="new-inspection" @click="goNewInspection">
        <span aria-hidden="true">＋</span> 新建巡检
      </button>
    </div>

    <!-- 指标带：整体容器 + 细竖线分隔 -->
    <div class="metrics panel" role="list" aria-label="巡检指标">
      <div class="metric" role="listitem">
        <div class="metric-value num">{{ realStats.loaded ? stats.records : '—' }}</div>
        <div class="metric-label">检测记录</div>
      </div>
      <div class="metric-sep" aria-hidden="true"></div>
      <div class="metric" role="listitem">
        <div class="metric-value num" :class="{ warn: stats.pendingReview > 0 }">{{ stats.pendingReview }}</div>
        <div class="metric-label">待人工复核</div>
      </div>
      <div class="metric-sep" aria-hidden="true"></div>
      <div class="metric" role="listitem">
        <div class="metric-value num">{{ realStats.loaded ? stats.models : '—' }}</div>
        <div class="metric-label">可用模型</div>
      </div>
      <div class="metric-sep" aria-hidden="true"></div>
      <div class="metric" role="listitem">
        <div class="metric-value num">{{ realStats.loaded ? stats.datasets : '—' }}</div>
        <div class="metric-label">数据集</div>
      </div>
    </div>

    <!-- 风场巡检分布 -->
    <div class="section">
      <h2 class="section-title">巡检分布（示意图，非真实台数）</h2>
      <div class="distribution">
        <!-- 左侧 60%：海上风机场景（真实照片） -->
        <div class="dist-scene">
          <img class="scene-img photo" :src="IMAGES.scene" alt="海上风机场景" />
          <div class="scene-overlay">
            <div class="scene-tag">海上风机巡检场景</div>
          </div>
        </div>
        <!-- 右侧 40%：风机阵列示意 -->
        <div class="dist-array" aria-label="风机阵列示意图">
          <div class="array-grid">
            <img class="array-bg photo" :src="IMAGES.windfarm" alt="" aria-hidden="true" />
            <button
              v-for="t in mockTurbines.list"
              :key="t.id"
              class="turbine-dot"
              :class="{ review: t.status === 'review', selected: selectedTurbine === t.id }"
              :style="{ left: t.x + '%', top: t.y + '%' }"
              :aria-label="`风机 ${t.id}${t.status === 'review' ? '，待复核' : ''}`"
              @click="selectTurbine(t.id)"
            >
              {{ t.id }}
            </button>
          </div>
          <div class="array-legend">
            <span class="legend-item"><span class="legend-dot"></span> 已巡检</span>
            <span class="legend-item"><span class="legend-dot warn"></span> 待复核</span>
            <span class="legend-note">点位示意</span>
          </div>
        </div>
      </div>
    </div>

    <!-- 待复核缺陷表格 -->
    <div class="section">
      <div class="section-head">
        <h2 class="section-title">待复核缺陷</h2>
        <span class="tag tag-warn num">{{ pendingDefects.length }} 项</span>
      </div>
      <div class="panel table-wrap">
        <table class="data-table">
          <thead>
            <tr>
              <th>影像</th>
              <th>风机编号</th>
              <th>叶片 / 文件</th>
              <th>疑似缺陷</th>
              <th class="num-col">置信度</th>
              <th>复核状态</th>
              <th class="action-col">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="d in pendingDefects" :key="d.id">
              <td>
                <div class="thumb" :aria-label="`${d.turbineId} 叶片影像缩略图`">
                  <img class="thumb-img photo" :src="bladeImageFor(d.turbineId)" :alt="`${d.turbineId} 叶片`" loading="lazy" />
                </div>
              </td>
              <td><span class="mono num">{{ d.turbineId }}</span></td>
              <td class="file-cell">
                <div class="file-name">{{ d.bladeFile }}</div>
              </td>
              <td><span class="tag">{{ d.suspectedType }}</span></td>
              <td class="num-col"><span class="num">{{ formatConfidence(d.confidence) }}</span></td>
              <td>
                <span class="tag tag-warn">待复核</span>
              </td>
              <td class="action-col">
                <button class="btn btn-sm" @click="goReview(d.id)">进入复核</button>
              </td>
            </tr>
            <tr v-if="!pendingDefects.length">
              <td colspan="7">
                <div class="empty">
                  <div class="empty-title">暂无待复核缺陷</div>
                  <div class="empty-desc">当前项目没有待复核的检测结果</div>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>

<style scoped>
.page {
  padding: var(--content-pad);
  max-width: 1200px;
  margin: 0 auto;
}
.breadcrumb {
  font-size: 12px;
  color: var(--text-secondary);
  margin-bottom: 12px;
  display: flex;
  align-items: center;
  gap: 6px;
}
.breadcrumb .sep {
  color: var(--border-control);
}
.breadcrumb .current {
  color: var(--text);
}
.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 20px;
}
.page-title {
  font-size: 24px;
  font-weight: 600;
  margin: 0;
  letter-spacing: -0.01em;
}

/* 指标带 */
.metrics {
  display: flex;
  align-items: stretch;
  padding: 20px 8px;
  margin-bottom: 24px;
}
.metric {
  flex: 1;
  text-align: center;
  padding: 0 12px;
}
.metric-value {
  font-size: 26px;
  font-weight: 600;
  color: var(--text);
  line-height: 1.2;
  margin-bottom: 6px;
}
.metric-value.warn {
  color: var(--warn-text);
}
.metric-value.model {
  font-size: 16px;
  line-height: 1.5;
  padding-top: 8px;
}
.metric-label {
  font-size: 12px;
  color: var(--text-secondary);
}
.metric-sep {
  width: 1px;
  background: var(--border);
}

/* 分布 */
.section {
  margin-bottom: 24px;
}
.section-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
}
.section-title {
  font-size: 16px;
  font-weight: 600;
  margin: 0 0 12px;
}
.section-head .section-title {
  margin: 0;
}
.distribution {
  display: flex;
  gap: 16px;
}
.dist-scene {
  flex: 6;
  position: relative;
  border-radius: var(--radius-md);
  overflow: hidden;
  border: 1px solid var(--border);
  background: var(--panel);
  min-height: 280px;
}
.scene-img {
  width: 100%;
  height: 320px;
  object-fit: cover;
  display: block;
}
.scene-overlay {
  position: absolute;
  inset: 0;
  background: linear-gradient(180deg, transparent 55%, rgba(0, 0, 0, 0.35) 100%);
  pointer-events: none;
}
.scene-tag {
  position: absolute;
  right: 10px;
  bottom: 10px;
  font-size: 11px;
  color: #fff;
  background: rgba(0, 0, 0, 0.55);
  padding: 4px 10px;
  border-radius: 4px;
  pointer-events: auto;
}
.dist-array {
  flex: 4;
  border-radius: var(--radius-md);
  border: 1px solid var(--border);
  background: var(--panel);
  display: flex;
  flex-direction: column;
}
.array-grid {
  position: relative;
  flex: 1;
  min-height: 260px;
  margin: 12px;
  border-radius: var(--radius-sm);
  overflow: hidden;
  border: 1px solid var(--border);
}
.array-bg {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: cover;
  opacity: 0.9;
}
.turbine-dot {
  position: absolute;
  transform: translate(-50%, -50%);
  width: 30px;
  height: 24px;
  border: 1px solid rgba(255, 255, 255, 0.7);
  background: rgba(23, 33, 42, 0.78);
  border-radius: 4px;
  font-size: 9px;
  color: #fff;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  font-family: var(--font-mono);
  z-index: 2;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.3);
}
.turbine-dot:hover {
  background: rgba(23, 33, 42, 0.92);
}
.turbine-dot.review {
  background: rgba(138, 75, 14, 0.92);
  border-color: #ffd9a3;
  color: #fff;
}
.turbine-dot.selected {
  outline: 2px solid #fff;
  outline-offset: 1px;
}
.array-legend {
  display: flex;
  gap: 16px;
  padding: 8px 12px;
  border-top: 1px solid var(--border);
  font-size: 12px;
  color: var(--text-secondary);
}
.legend-item {
  display: flex;
  align-items: center;
  gap: 6px;
}
.legend-dot {
  width: 10px;
  height: 10px;
  border-radius: 3px;
  background: var(--panel-secondary);
  border: 1px solid var(--border-control);
}
.legend-dot.warn {
  background: var(--warn-bg);
  border-color: var(--warn-text);
}
.legend-note {
  margin-left: auto;
  font-size: 11px;
  color: var(--text-secondary);
}

/* 表格 */
.table-wrap {
  overflow-x: auto;
}
.data-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.data-table th {
  text-align: left;
  padding: 10px 14px;
  font-size: 12px;
  color: var(--text-secondary);
  font-weight: 500;
  border-bottom: 1px solid var(--border);
  white-space: nowrap;
}
.data-table td {
  padding: 10px 14px;
  border-bottom: 1px solid var(--border);
  vertical-align: middle;
}
.data-table tbody tr:last-child td {
  border-bottom: none;
}
.data-table tbody tr:hover {
  background: var(--panel-secondary);
}
.thumb {
  border-radius: 4px;
  overflow: hidden;
  border: 1px solid var(--border);
  display: inline-flex;
  background: var(--panel-secondary);
}
.thumb-img {
  width: 48px;
  height: 32px;
  object-fit: cover;
  display: block;
}
.mono {
  font-family: var(--font-mono);
}
.num-col {
  text-align: right;
  font-variant-numeric: tabular-nums;
}
.action-col {
  text-align: right;
}
.file-name {
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-secondary);
}

/* 响应式 */
@media (max-width: 900px) {
  .distribution {
    flex-direction: column;
  }
  .dist-scene {
    flex: none;
  }
}
@media (max-width: 768px) {
  .page {
    padding: 16px;
  }
  .page-header {
    flex-wrap: wrap;
    gap: 12px;
  }
  .metrics {
    flex-wrap: wrap;
    padding: 12px;
    gap: 12px;
  }
  .metric {
    flex: 1 1 40%;
    min-width: 40%;
  }
  .metric-sep {
    display: none;
  }
  .review-body {
    flex-direction: column;
  }
}
</style>
