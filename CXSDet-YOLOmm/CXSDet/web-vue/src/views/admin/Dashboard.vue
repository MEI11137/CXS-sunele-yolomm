<!-- MD5: 5c21a63b9a3c28af3971c86f37a56b71 -->
<!-- 管理控制台页面 -->
<!--
版权所有©2025 羊羊小栈(GJQ)

作者:羊羊小栈
时间:2025-09-06 23:44:27

本系统为原创作品,禁止二次销售!禁止将系统对应的视频,文档进行二次发布!
违者需立即停止侵权行为,并按照【羊羊小栈系统版权声明及保护条款】中规定数额进行赔偿,并承担相应法律责任。
-->

<template>
  <div class="dashboard">
    <header class="page-head">
      <div>
        <h1 class="page-title">管理控制台</h1>
        <p class="page-sub">全系统的巡检规模与模型迭代概况。这里只统计已经发生的操作，不含预测值。</p>
      </div>
    </header>

    <!-- 统计概览区域 -->
    <section class="metric-strip hx-panel">
      <div class="metric">
        <span class="metric-label">账号</span>
        <span class="metric-value hx-mono">{{ stats.totalUsers }}</span>
        <span class="metric-note">可登录的用户</span>
      </div>
      <div class="metric">
        <span class="metric-label">数据集</span>
        <span class="metric-value hx-mono">{{ stats.totalDatasets }}</span>
        <span class="metric-note">含反馈版本</span>
      </div>
      <div class="metric">
        <span class="metric-label">模型</span>
        <span class="metric-value hx-mono">{{ stats.totalModels }}</span>
        <span class="metric-note">含未发布版本</span>
      </div>
      <div class="metric">
        <span class="metric-label">检测记录</span>
        <span class="metric-value hx-mono">{{ stats.totalDetections }}</span>
        <span class="metric-note">图片检测累计</span>
      </div>
    </section>

    <!-- 图表区域 -->
    <div class="charts-section">
      <div class="charts-grid">
        <!-- 检测趋势图 -->
        <div class="chart-card">
          <div class="chart-header">
            <h3 class="chart-title">检测趋势统计</h3>
            <el-select v-model="detectionTimeRange" size="small" @change="loadDetectionTrend">
              <el-option label="最近7天" value="7" />
              <el-option label="最近30天" value="30" />
            </el-select>
          </div>
          <div class="chart-container">
            <div ref="detectionTrendChart" class="chart"></div>
          </div>
        </div>

        <!-- 用户活跃度图 -->
        <div class="chart-card">
          <div class="chart-header">
            <h3 class="chart-title">用户活跃度分析</h3>
            <el-select v-model="userActivityRange" size="small" @change="loadUserActivity">
              <el-option label="最近7天" value="7" />
              <el-option label="最近30天" value="30" />
            </el-select>
          </div>
          <div class="chart-container">
            <div ref="userActivityChart" class="chart"></div>
          </div>
        </div>

        <!-- 最近检测记录 -->
        <div class="chart-card">
          <div class="chart-header">
            <h3 class="chart-title">最近检测记录</h3>
          </div>
          <div class="chart-container">
            <div class="detection-tabs">
              <el-tabs v-model="activeDetectionTab" @tab-change="handleTabChange">
                <el-tab-pane label="图片检测" name="image">
                  <div v-if="recentImageDetections.length === 0" class="no-data">
                    <el-icon class="empty-icon"><Document /></el-icon>
                    <p>暂无图片检测记录</p>
                  </div>
                  <div v-else class="detection-list">
                    <div v-for="detection in recentImageDetections" :key="detection.id" class="detection-item">
                      <div class="detection-info">
                        <h4 class="detection-title">{{ detection.title || '图片检测' }}</h4>
                        <p class="detection-meta">
                          <span class="user-name">{{ detection.user_name || '未知用户' }}</span>
                          <span class="model-name">{{ detection.model_name || '默认模型' }}</span>
                        </p>
                      </div>
                      <span class="detection-time">{{ formatTime(detection.create_time) }}</span>
                    </div>
                  </div>
                </el-tab-pane>
                
                <el-tab-pane label="视频检测" name="video">
                  <div v-if="recentVideoDetections.length === 0" class="no-data">
                    <el-icon class="empty-icon"><Document /></el-icon>
                    <p>暂无视频检测记录</p>
                  </div>
                  <div v-else class="detection-list">
                    <div v-for="detection in recentVideoDetections" :key="detection.id" class="detection-item">
                      <div class="detection-info">
                        <h4 class="detection-title">{{ detection.title || '视频检测' }}</h4>
                        <p class="detection-meta">
                          <span class="user-name">{{ detection.user_name || '未知用户' }}</span>
                          <span class="model-name">{{ detection.model_name || '默认模型' }}</span>
                        </p>
                      </div>
                      <span class="detection-time">{{ formatTime(detection.create_time) }}</span>
                    </div>
                  </div>
                </el-tab-pane>
                
                <el-tab-pane label="实时检测" name="realtime">
                  <div v-if="recentRealtimeSessions.length === 0" class="no-data">
                    <el-icon class="empty-icon"><Document /></el-icon>
                    <p>暂无实时检测会话</p>
                  </div>
                  <div v-else class="detection-list">
                    <div v-for="session in recentRealtimeSessions" :key="session.session_id" class="detection-item">
                      <div class="detection-info">
                        <h4 class="detection-title">{{ formatSessionId(session.session_id) }}</h4>
                        <p class="detection-meta">
                          <span class="user-name">{{ session.user_name || '未知用户' }}</span>
                          <span class="model-name">{{ session.model_name }}</span>
                        </p>
                        <div class="session-stats">
                          <el-tag size="small" type="success">{{ session.detection_count }} 次检测</el-tag>
                          <el-tag size="small" type="warning">{{ session.class_count }} 个类别</el-tag>
                        </div>
                      </div>
                      <span class="detection-time">{{ formatTime(session.start_time) }}</span>
                    </div>
                  </div>
                </el-tab-pane>
              </el-tabs>
            </div>
          </div>
        </div>

        <!-- 检测类型分布图 -->
        <div class="chart-card">
          <div class="chart-header">
            <h3 class="chart-title">检测类型分布</h3>
          </div>
          <div class="chart-container">
            <div ref="detectionTypeChart" class="chart"></div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, computed } from 'vue'
import { User, DataLine, Monitor, Document, Calendar } from '@element-plus/icons-vue'
import * as echarts from 'echarts'
import { useRouter } from 'vue-router'
import { getUserList } from '@/api/user'
import { datasetApi } from '@/api/dataset'
import { modelApi } from '@/api/model'
import { detectionApi } from '@/api/detection'
import { realtimeDetectionApi } from '@/api/realtime_detection'
import { videoDetectionApi } from '@/api/video_detection'

const router = useRouter()

// 图表引用
const detectionTrendChart = ref<HTMLElement>()
const userActivityChart = ref<HTMLElement>()
const detectionTypeChart = ref<HTMLElement>()

// 图表实例
let detectionTrendChartInstance: echarts.ECharts | null = null
let userActivityChartInstance: echarts.ECharts | null = null
let detectionTypeChartInstance: echarts.ECharts | null = null

// 统计数据
const stats = ref({
  totalUsers: 0,
  totalDatasets: 0,
  totalModels: 0,
  totalDetections: 0
})

// 时间范围选择
const detectionTimeRange = ref('30')
const userActivityRange = ref('30')

// 检测记录相关
const activeDetectionTab = ref('image')
const recentImageDetections = ref<any[]>([])
const recentVideoDetections = ref<any[]>([])
const recentRealtimeSessions = ref<any[]>([])



// 加载统计数据
const loadStats = async () => {
  try {
    // 获取用户统计
    const userResponse = await getUserList({ current: 1, size: 1 })
    stats.value.totalUsers = userResponse.total

    // 获取数据集统计
    const datasetResponse = await datasetApi.list({ page: 1, page_size: 1 })
    stats.value.totalDatasets = datasetResponse.total

    // 获取模型统计
    const modelResponse = await modelApi.list({ page: 1, page_size: 1 })
    stats.value.totalModels = modelResponse.total

    // 获取检测统计
    const detectionResponse = await detectionApi.getRecords({ page: 1, page_size: 1 })
    const videoResponse = await videoDetectionApi.getRecords({ page: 1, page_size: 1 })
    const realtimeResponse = await realtimeDetectionApi.getRecords({ page: 1, page_size: 1 })
    
    stats.value.totalDetections = detectionResponse.total + videoResponse.total + realtimeResponse.total
  } catch (error) {
    console.error('加载统计数据失败:', error)
  }
}

// 加载检测趋势图
const loadDetectionTrend = async () => {
  try {
    const days = parseInt(detectionTimeRange.value)
    const endDate = new Date()
    const startDate = new Date()
    startDate.setDate(startDate.getDate() - days + 1) // 包含今天

    // 获取检测记录
    const detectionResponse = await detectionApi.getRecords({ page: 1, page_size: 1000 })
    const videoResponse = await videoDetectionApi.getRecords({ page: 1, page_size: 1000 })
    const realtimeResponse = await realtimeDetectionApi.getRecords({ page: 1, page_size: 1000 })

    // 合并所有检测记录并过滤时间范围
    const allDetections: Array<{ time: string; type: string }> = []
    
    // 处理图片检测记录
    detectionResponse.records.forEach(r => {
      const recordDate = new Date(r.create_time)
      if (recordDate >= startDate && recordDate <= endDate) {
        allDetections.push({ time: r.create_time, type: 'image' })
      }
    })
    
    // 处理视频检测记录
    videoResponse.records.forEach(r => {
      const recordDate = new Date(r.create_time)
      if (recordDate >= startDate && recordDate <= endDate) {
        allDetections.push({ time: r.create_time, type: 'video' })
      }
    })
    
    // 处理实时检测记录
    realtimeResponse.records.forEach(r => {
      const recordDate = new Date(r.detection_time)
      if (recordDate >= startDate && recordDate <= endDate) {
        allDetections.push({ time: r.detection_time, type: 'realtime' })
      }
    })

    // 按日期分组统计
    const dateStats = new Map()
    for (let i = 0; i < days; i++) {
      const date = new Date(startDate)
      date.setDate(date.getDate() + i)
      const dateStr = date.toISOString().split('T')[0]
      dateStats.set(dateStr, { image: 0, video: 0, realtime: 0, total: 0 })
    }

    // 统计每天的数据
    allDetections.forEach(detection => {
      try {
        const recordDate = new Date(detection.time)
        const dateStr = recordDate.toISOString().split('T')[0]
        
        if (dateStats.has(dateStr)) {
          const stats = dateStats.get(dateStr)
          stats[detection.type]++
          stats.total++
        }
      } catch (error) {
        console.warn('解析检测记录时间失败:', detection.time, error)
      }
    })

    // 格式化日期显示
    const dates = Array.from(dateStats.keys()).map(dateStr => {
      const date = new Date(dateStr)
      return date.toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' })
    })
    
    const imageData = Array.from(dateStats.values()).map(s => s.image)
    const videoData = Array.from(dateStats.values()).map(s => s.video)
    const realtimeData = Array.from(dateStats.values()).map(s => s.realtime)

    console.log('检测趋势数据:', { dates, imageData, videoData, realtimeData })
    renderDetectionTrendChart(dates, imageData, videoData, realtimeData)
  } catch (error) {
    console.error('加载检测趋势数据失败:', error)
  }
}

// 加载用户活跃度图
const loadUserActivity = async () => {
  try {
    const days = parseInt(userActivityRange.value)
    const endDate = new Date()
    const startDate = new Date()
    startDate.setDate(startDate.getDate() - days + 1) // 包含今天

    // 获取所有检测记录来计算用户活跃度
    const detectionResponse = await detectionApi.getRecords({ page: 1, page_size: 1000 })
    const videoResponse = await videoDetectionApi.getRecords({ page: 1, page_size: 1000 })
    const realtimeResponse = await realtimeDetectionApi.getRecords({ page: 1, page_size: 1000 })

    // 合并所有检测记录
    const allDetections: Array<{ time: string; user_id: number }> = []
    
    // 处理图片检测记录
    detectionResponse.records.forEach(r => {
      const recordDate = new Date(r.create_time)
      if (recordDate >= startDate && recordDate <= endDate) {
        allDetections.push({ time: r.create_time, user_id: r.user_id })
      }
    })
    
    // 处理视频检测记录
    videoResponse.records.forEach(r => {
      const recordDate = new Date(r.create_time)
      if (recordDate >= startDate && recordDate <= endDate) {
        allDetections.push({ time: r.create_time, user_id: r.user_id })
      }
    })
    
    // 处理实时检测记录
    realtimeResponse.records.forEach(r => {
      const recordDate = new Date(r.detection_time)
      if (recordDate >= startDate && recordDate <= endDate) {
        allDetections.push({ time: r.detection_time, user_id: r.user_id })
      }
    })

    // 按日期分组统计活跃用户数
    const dateStats = new Map()
    for (let i = 0; i < days; i++) {
      const date = new Date(startDate)
      date.setDate(date.getDate() + i)
      const dateStr = date.toISOString().split('T')[0]
      dateStats.set(dateStr, new Set()) // 使用Set来统计每天的唯一用户
    }

    // 统计每天活跃的用户
    allDetections.forEach(detection => {
      try {
        const recordDate = new Date(detection.time)
        const dateStr = recordDate.toISOString().split('T')[0]
        
        if (dateStats.has(dateStr)) {
          const userSet = dateStats.get(dateStr)
          userSet.add(detection.user_id)
        }
      } catch (error) {
        console.warn('解析用户活跃度记录时间失败:', detection.time, error)
      }
    })

    // 格式化日期显示并获取活跃用户数
    const dates = Array.from(dateStats.keys()).map(dateStr => {
      const date = new Date(dateStr)
      return date.toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' })
    })
    
    const activeUsers = Array.from(dateStats.values()).map(userSet => userSet.size)

    console.log('用户活跃度数据:', { dates, activeUsers })
    console.log('原始检测记录数量:', {
      image: detectionResponse.records.length,
      video: videoResponse.records.length,
      realtime: realtimeResponse.records.length
    })
    renderUserActivityChart(dates, activeUsers)
  } catch (error) {
    console.error('加载用户活跃度数据失败:', error)
  }
}



// 加载检测类型分布图
const loadDetectionTypeDistribution = async () => {
  try {
    const detectionResponse = await detectionApi.getRecords({ page: 1, page_size: 1 })
    const videoResponse = await videoDetectionApi.getRecords({ page: 1, page_size: 1 })
    const realtimeResponse = await realtimeDetectionApi.getRecords({ page: 1, page_size: 1 })

    const data = [
      { value: detectionResponse.total, name: '图片检测' },
      { value: videoResponse.total, name: '视频检测' },
      { value: realtimeResponse.total, name: '实时检测' }
    ]

    renderDetectionTypeChart(data)
  } catch (error) {
    console.error('加载检测类型分布数据失败:', error)
  }
}

// 加载最近检测记录
const loadRecentDetections = async () => {
  try {
    // 加载图片检测记录
    const imageResponse = await detectionApi.getRecords({ page: 1, page_size: 5 })
    recentImageDetections.value = imageResponse.records
    
    // 加载视频检测记录
    const videoResponse = await videoDetectionApi.getRecords({ page: 1, page_size: 5 })
    recentVideoDetections.value = videoResponse.records
    
    // 加载实时检测会话
    const realtimeResponse = await realtimeDetectionApi.getSessions()
    recentRealtimeSessions.value = realtimeResponse.sessions.slice(0, 5) // 只取前5个会话
  } catch (error) {
    console.error('加载最近检测记录失败:', error)
  }
}

// 处理标签页切换
const handleTabChange = (tabName: any) => {
  activeDetectionTab.value = tabName
}

// 渲染检测趋势图
const renderDetectionTrendChart = (dates: string[], imageData: number[], videoData: number[], realtimeData: number[]) => {
  if (!detectionTrendChart.value) return

  if (detectionTrendChartInstance) {
    detectionTrendChartInstance.dispose()
  }

  detectionTrendChartInstance = echarts.init(detectionTrendChart.value)
  
  const option = {
    tooltip: {
      trigger: 'axis',
      axisPointer: {
        type: 'cross'
      },
      formatter: function(params: any) {
        let result = params[0].axisValue + '<br/>'
        params.forEach((param: any) => {
          result += param.marker + ' ' + param.seriesName + ': ' + param.value + '<br/>'
        })
        return result
      }
    },
    legend: {
      data: ['图片检测', '视频检测', '实时检测'],
      top: 10
    },
    grid: {
      left: '3%',
      right: '4%',
      bottom: '15%',
      containLabel: true
    },
    xAxis: {
      type: 'category',
      data: dates,
      axisLabel: {
        rotate: 45,
        fontSize: 12
      }
    },
    yAxis: {
      type: 'value',
      minInterval: 1,
      axisLabel: {
        fontSize: 12
      }
    },
    series: [
      {
        name: '图片检测',
        type: 'line',
        data: imageData,
        smooth: true,
        itemStyle: { color: '#409EFF' },
        lineStyle: { width: 3 },
        symbol: 'circle',
        symbolSize: 6
      },
      {
        name: '视频检测',
        type: 'line',
        data: videoData,
        smooth: true,
        itemStyle: { color: '#67C23A' },
        lineStyle: { width: 3 },
        symbol: 'circle',
        symbolSize: 6
      },
      {
        name: '实时检测',
        type: 'line',
        data: realtimeData,
        smooth: true,
        itemStyle: { color: '#E6A23C' },
        lineStyle: { width: 3 },
        symbol: 'circle',
        symbolSize: 6
      }
    ]
  }

  detectionTrendChartInstance.setOption(option)
}

// 渲染用户活跃度图
const renderUserActivityChart = (dates: string[], activeUsers: number[]) => {
  if (!userActivityChart.value) return

  if (userActivityChartInstance) {
    userActivityChartInstance.dispose()
  }

  userActivityChartInstance = echarts.init(userActivityChart.value)
  
  const option = {
    tooltip: {
      trigger: 'axis',
      axisPointer: {
        type: 'shadow'
      },
      formatter: function(params: any) {
        const param = params[0]
        return `${param.axisValue}<br/>${param.marker} 活跃用户数: <strong>${param.value}</strong> 人`
      }
    },
    grid: {
      left: '3%',
      right: '4%',
      bottom: '15%',
      containLabel: true
    },
    xAxis: {
      type: 'category',
      data: dates,
      axisLabel: {
        fontSize: 12,
        rotate: 45
      }
    },
    yAxis: {
      type: 'value',
      name: '用户数',
      minInterval: 1,
      axisLabel: {
        fontSize: 12
      }
    },
    series: [
      {
        name: '活跃用户数',
        type: 'line',
        data: activeUsers,
        smooth: true,
        itemStyle: { color: '#409EFF' },
        lineStyle: { width: 3 },
        symbol: 'circle',
        symbolSize: 6,
        areaStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: 'rgba(64, 158, 255, 0.3)' },
            { offset: 1, color: 'rgba(64, 158, 255, 0.1)' }
          ])
        }
      }
    ]
  }

  userActivityChartInstance.setOption(option)
}



// 渲染检测类型分布图
const renderDetectionTypeChart = (data: any[]) => {
  if (!detectionTypeChart.value) return

  if (detectionTypeChartInstance) {
    detectionTypeChartInstance.dispose()
  }

  detectionTypeChartInstance = echarts.init(detectionTypeChart.value)
  
  const option = {
    tooltip: {
      trigger: 'item',
      formatter: '{a} <br/>{b}: {c} ({d}%)'
    },
    legend: {
      orient: 'vertical',
      left: 'left'
    },
    series: [
      {
        name: '检测类型',
        type: 'pie',
        radius: ['40%', '70%'],
        avoidLabelOverlap: false,
        label: {
          show: false,
          position: 'center'
        },
        emphasis: {
          label: {
            show: true,
            fontSize: '18',
            fontWeight: 'bold'
          }
        },
        labelLine: {
          show: false
        },
        data: data,
        itemStyle: {
          borderRadius: 10,
          borderColor: '#fff',
          borderWidth: 2
        }
      }
    ]
  }

  detectionTypeChartInstance.setOption(option)
}

// 格式化时间
const formatTime = (timeStr: string) => {
  const date = new Date(timeStr)
  const now = new Date()
  const diff = now.getTime() - date.getTime()
  const days = Math.floor(diff / (1000 * 60 * 60 * 24))
  
  if (days === 0) {
    return '今天'
  } else if (days === 1) {
    return '昨天'
  } else if (days < 7) {
    return `${days}天前`
  } else {
    return date.toLocaleDateString('zh-CN')
  }
}

// 格式化会话ID显示
const formatSessionId = (sessionId: string) => {
  // 格式: RT_20250903_143022_123 -> 2025-09-03 14:30:22 #123
  const parts = sessionId.split('_')
  if (parts.length >= 4) {
    const date = parts[1]
    const time = parts[2]
    const random = parts[3]
    
    // 格式化日期: 20250903 -> 2025-09-03
    const formattedDate = `${date.substring(0,4)}-${date.substring(4,6)}-${date.substring(6,8)}`
    // 格式化时间: 143022 -> 14:30:22
    const formattedTime = `${time.substring(0,2)}:${time.substring(2,4)}:${time.substring(4,6)}`
    
    return `${formattedDate} ${formattedTime} #${random}`
  }
  
  // 如果格式不正确，直接返回原值
  return sessionId
}

// 页面跳转
const navigateTo = (path: string) => {
  router.push(path)
}



// 组件挂载
onMounted(async () => {
  console.log('开始加载管理控制台数据...')
  
  await loadStats()
  console.log('统计数据加载完成:', stats.value)
  
  await loadDetectionTrend()
  console.log('检测趋势图加载完成')
  
  await loadUserActivity()
  await loadDetectionTypeDistribution()
  await loadRecentDetections()

  console.log('管理控制台数据加载完成')
})

// 组件卸载时清理图表实例
onBeforeUnmount(() => {
  if (detectionTrendChartInstance) {
    detectionTrendChartInstance.dispose()
  }
  if (userActivityChartInstance) {
    userActivityChartInstance.dispose()
  }
  if (detectionTypeChartInstance) {
    detectionTypeChartInstance.dispose()
  }
})
</script>

<style scoped>
.dashboard {
  padding: 24px;
  background-color: #f5f7fa;
  min-height: 100vh;
}

/* 统计概览区域 */
.stats-overview {
  margin-bottom: 24px;
}

.stats-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
  gap: 20px;
}

.stat-card {
  background: white;
  border-radius: 12px;
  padding: 24px;
  display: flex;
  align-items: center;
  gap: 20px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06);
  transition: all 0.3s ease;
}

.stat-card:hover {
  transform: translateY(-4px);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.12);
}

.stat-icon {
  width: 60px;
  height: 60px;
  border-radius: 12px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 24px;
  color: white;
}

.user-icon {
  background: linear-gradient(135deg, #409EFF 0%, #66b1ff 100%);
}

.dataset-icon {
  background: linear-gradient(135deg, #67C23A 0%, #85ce61 100%);
}

.model-icon {
  background: linear-gradient(135deg, #E6A23C 0%, #ebb563 100%);
}

.detection-icon {
  background: linear-gradient(135deg, #F56C6C 0%, #f78989 100%);
}

.stat-content {
  flex: 1;
}

.stat-number {
  font-size: 32px;
  font-weight: 700;
  color: #303133;
  margin: 0 0 8px 0;
}

.stat-label {
  font-size: 14px;
  color: #909399;
  margin: 0;
}

/* 图表区域 */
.charts-section {
  margin-bottom: 24px;
}

.charts-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 24px;
}

.chart-card {
  background: white;
  border-radius: 16px;
  padding: 24px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06);
}

.chart-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 20px;
}

.chart-title {
  font-size: 18px;
  font-weight: 600;
  color: #303133;
  margin: 0;
  min-width: 200px;
  flex-shrink: 0;
}

.chart-container {
  height: 300px;
}

/* 检测记录标签页容器高度调整 */
.chart-container .detection-tabs {
  height: 100%;
  overflow-y: auto;
}

.chart {
  width: 100%;
  height: 100%;
}

/* 系统状态区域 */
.system-status {
  margin-bottom: 24px;
}

.status-grid {
  display: grid;
  grid-template-columns: 2fr 1fr;
  gap: 24px;
}

.status-card {
  background: white;
  border-radius: 16px;
  padding: 24px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06);
}

.status-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 20px;
}

.status-title {
  font-size: 18px;
  font-weight: 600;
  color: #303133;
  margin: 0;
}

.status-content {
  min-height: 200px;
}

.no-data {
  text-align: center;
  padding: 40px 20px;
  color: #909399;
}

.empty-icon {
  font-size: 48px;
  margin-bottom: 16px;
  opacity: 0.5;
}

.detection-list {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.detection-item {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  padding: 16px;
  background: #f8f9fa;
  border-radius: 8px;
  border-left: 4px solid #409EFF;
}

.detection-info {
  flex: 1;
}

.detection-title {
  font-size: 14px;
  font-weight: 600;
  color: #303133;
  margin: 0 0 8px 0;
}

.detection-meta {
  font-size: 12px;
  color: #909399;
  margin: 0;
}

.user-name {
  margin-right: 16px;
}

.model-name {
  background: #e1f3d8;
  color: #67c23a;
  padding: 2px 8px;
  border-radius: 4px;
}

.session-stats {
  display: flex;
  gap: 8px;
  margin-top: 8px;
}

.session-stats .el-tag {
  font-size: 11px;
  height: 20px;
  line-height: 18px;
}

.detection-time {
  font-size: 12px;
  color: #c0c4cc;
  white-space: nowrap;
}

.detection-tabs {
  width: 100%;
}

.detection-tabs :deep(.el-tabs__header) {
  margin-bottom: 16px;
}

.detection-tabs :deep(.el-tabs__nav-wrap) {
  padding: 0;
}

.detection-tabs :deep(.el-tabs__item) {
  font-size: 14px;
  padding: 0 20px;
}

.detection-tabs :deep(.el-tabs__content) {
  padding: 0;
}



/* 响应式设计 */
@media (max-width: 1200px) {
  .charts-grid {
    grid-template-columns: 1fr;
  }
  
  .status-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 768px) {
  .dashboard {
    padding: 16px;
  }
  
  .stats-grid {
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  }
  
  .chart-container {
    height: 250px;
  }
  
  .stat-number {
    font-size: 28px;
  }
}

/* —— 新版页头与指标条 —— */
.dashboard {
  padding: 0;
  background: transparent;
  min-height: auto;
  display: flex;
  flex-direction: column;
  gap: var(--hx-gap);
}

.page-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  padding-bottom: 14px;
  border-bottom: 1px solid var(--hx-line);
}

.page-title {
  margin: 0;
  font-size: 20px;
  line-height: 1.2;
  font-weight: 600;
  color: var(--hx-text);
  padding-left: 10px;
  border-left: 3px solid var(--hx-sea);
}

.page-sub {
  margin: 6px 0 0 13px;
  font-size: 12.5px;
  color: var(--hx-text-2);
}

.metric-strip {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
}

.metric {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 16px 18px;
  border-right: 1px solid var(--hx-line);
}

.metric:last-child {
  border-right: none;
}

.metric-label {
  font-size: 12px;
  color: var(--hx-text-3);
}

.metric-value {
  font-size: 26px;
  font-weight: 600;
  line-height: 1.15;
  color: var(--hx-text);
}

.metric-note {
  font-size: 11px;
  color: var(--hx-text-3);
}

/* 图表卡片收敛为仪器面板 */
.chart-card {
  background: var(--hx-paper);
  border: 1px solid var(--hx-line);
  border-radius: var(--hx-radius);
  padding: 0 0 12px;
  box-shadow: var(--hx-shadow-panel);
}

.chart-header {
  padding: 12px 16px;
  border-bottom: 1px solid var(--hx-line);
}

.chart-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--hx-text);
}

.chart-container {
  padding: 8px 12px 0;
}

@media (max-width: 900px) {
  .metric-strip {
    grid-template-columns: 1fr 1fr;
  }
  .metric {
    border-bottom: 1px solid var(--hx-line);
  }
}
</style> 