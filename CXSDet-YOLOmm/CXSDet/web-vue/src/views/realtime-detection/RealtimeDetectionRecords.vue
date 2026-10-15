<!-- MD5: 6c99c11cf6d93e94558aa63cf635e9c1 -->
<!--
版权所有 © 2025 羊羊小栈 (GJQ)

作者: 羊羊小栈
时间: 2025-09-06 23:44:27

本系统为原创作品，禁止二次销售！禁止将系统对应的视频、文档进行二次发布！
违者需立即停止侵权行为，并按照【羊羊小栈系统版权声明及保护条款】中规定数额进行赔偿，并承担相应法律责任。
-->

<template>
  <div class="realtime-detection-records">
    <!-- 页面标题区 -->
    <div class="page-header">
      <div class="header-content">
        <div class="header-left">
          <h1 class="page-title">实时检测记录</h1>
          <p class="page-subtitle">查看和管理光伏板缺陷实时检测的历史记录与会话数据</p>
        </div>
        <div class="header-right">
          <el-button @click="refreshRecords" :loading="loading" type="primary" class="refresh-btn">
            <el-icon><Refresh /></el-icon>
            刷新数据
          </el-button>
        </div>
      </div>
    </div>

    <!-- 主内容区域 -->
    <div class="main-content">
      <!-- 搜索和过滤面板 -->
      <div class="search-panel">
        <div class="search-header">
          <h3 class="search-title">
            <el-icon><Filter /></el-icon>
            数据筛选
          </h3>
        </div>
        <div class="search-content">
          <div class="search-section">
            <div class="search-left">
              <div class="search-filters">
                <div class="filter-group">
                  <label class="filter-label">会话ID</label>
                  <el-input
                    v-model="searchParams.session_id"
                    placeholder="请输入会话ID进行搜索"
                    clearable
                    @clear="handleSearch"
                    @keyup.enter="handleSearch"
                    class="filter-input"
                  >
                    <template #prefix>
                      <el-icon><Search /></el-icon>
                    </template>
                  </el-input>
                </div>
                <div class="filter-group">
                  <label class="filter-label">检测模型</label>
                  <el-select
                    v-model="searchParams.model_id"
                    placeholder="选择模型"
                    clearable
                    @change="handleSearch"
                    class="filter-select"
                  >
                    <el-option
                      v-for="model in availableModels"
                      :key="model.id"
                      :label="model.name"
                      :value="model.id"
                    />
                  </el-select>
                </div>
                <div class="filter-group">
                  <label class="filter-label">检测类别</label>
                  <el-select
                    v-model="searchParams.class_name"
                    placeholder="选择类别"
                    clearable
                    @change="handleSearch"
                    class="filter-select"
                  >
                    <el-option
                      v-for="(classInfo, className) in yoloClasses"
                      :key="className"
                      :label="classInfo.chinese_name || className"
                      :value="className"
                    />
                  </el-select>
                </div>
                <div class="filter-group search-buttons-group">
                  <label class="filter-label">&nbsp;</label>
                  <div class="search-buttons">
                    <el-button type="primary" @click="handleSearch" class="search-btn">
                      <el-icon><Search /></el-icon>
                      搜索
                    </el-button>
                    <el-button @click="resetSearch" class="clear-btn">
                      <el-icon><RefreshLeft /></el-icon>
                      重置
                    </el-button>
                  </div>
                </div>
              </div>
            </div>
            <div class="search-right">
              <div class="stats-info">
                <span class="stats-text">共 {{ total }} 条记录</span>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- 会话统计概览 -->
      <div class="sessions-overview">
        <div class="overview-header">
          <h3 class="overview-title">
            <el-icon><TrendCharts /></el-icon>
            检测会话概览
          </h3>
          <el-tag type="info">共 {{ detectionSessions.length }} 个会话</el-tag>
        </div>
        <div class="sessions-grid">
        <div
          v-for="session in paginatedSessions"
          :key="session.session_id"
          class="session-card"
          :class="{ 'session-active': searchParams.session_id === session.session_id }"
        >
          <div class="session-header">
            <div class="session-id" @click="filterBySession(session.session_id)" :title="session.session_id">
              {{ formatSessionId(session.session_id) }}
            </div>
            <div class="session-actions">
              <el-button
                size="small"
                type="danger"
                link
                @click.stop="deleteSession(session)"
                title="删除会话"
              >
                <el-icon><Delete /></el-icon>
              </el-button>
            </div>
          </div>
          <div class="session-stats" @click="filterBySession(session.session_id)">
            <div class="stat-row">
              <div class="stat-item">
                <span class="stat-label">模型:</span>
                <el-tag size="small" type="info">{{ session.model_name }}</el-tag>
              </div>
              <div class="stat-item" v-if="session.user_name">
                <span class="stat-label">用户:</span>
                <span class="stat-value">{{ session.user_name }}</span>
              </div>
            </div>
            <div class="stat-row">
              <div class="stat-item">
                <span class="stat-label">检测数:</span>
                <el-tag size="small" type="success">{{ session.detection_count }}</el-tag>
              </div>
              <div class="stat-item">
                <span class="stat-label">类别数:</span>
                <el-tag size="small" type="warning">{{ session.class_count }}</el-tag>
              </div>
            </div>
          </div>
        </div>
        
        <!-- 会话分页 -->
        <div v-if="detectionSessions.length > sessionsPerPage" class="sessions-pagination">
          <el-pagination
            v-model:current-page="currentSessionPage"
            :page-size="sessionsPerPage"
            :total="detectionSessions.length"
            layout="prev, pager, next"
            small
            @current-change="handleSessionPageChange"
          />
        </div>
        
        <div v-if="detectionSessions.length === 0" class="no-sessions">
          <el-icon class="empty-icon"><Calendar /></el-icon>
          <p>暂无检测会话</p>
          <small>开始实时检测后，会话信息将在此显示</small>
        </div>
      </div>
      </div>

      <!-- 检测记录表格 -->
      <div class="records-section">
        <div class="records-header">
          <h3 class="records-title">
            <el-icon><DataAnalysis /></el-icon>
            检测记录详情
          </h3>
          <div class="records-info">
            <el-tag type="success">共 {{ total }} 条记录</el-tag>
          </div>
        </div>
        <div class="records-table">
      <el-table
        :data="records"
        v-loading="loading"
        style="width: 100%"
        border
        stripe
      >
        <el-table-column prop="session_id" label="会话ID" width="200">
          <template #default="{ row }">
            <el-tag size="small" type="info">{{ formatSessionId(row.session_id) }}</el-tag>
          </template>
        </el-table-column>
        
        <el-table-column prop="model_name" label="模型" min-width="120" />
        
        <el-table-column prop="track_id" label="目标ID" width="100" align="center">
          <template #default="{ row }">
            <el-tag v-if="row.track_id" type="info" size="small">
              {{ row.track_id }}
            </el-tag>
            <span v-else>-</span>
          </template>
        </el-table-column>
        
        <el-table-column prop="class_name" label="检测类别" width="120">
          <template #default="{ row }">
            <el-tag 
              :color="getClassColor(row.class_name)" 
              size="small"
              style="color: white; border: none;"
            >
              {{ getClassDisplayName(row.class_name) }}
            </el-tag>
          </template>
        </el-table-column>
        
        <el-table-column prop="confidence" label="置信度" width="100" align="center">
          <template #default="{ row }">
            <span :class="getConfidenceClass(row.confidence)">
              {{ (row.confidence * 100).toFixed(1) }}%
            </span>
          </template>
        </el-table-column>
        
        <el-table-column label="检测结果" width="120" align="center">
          <template #default="{ row }">
            <el-button
              v-if="row.result_image_url"
              size="small"
              type="primary"
              link
              @click="showImagePreview(row.result_image_url)"
            >
              <el-icon><View /></el-icon>
              查看图片
            </el-button>
            <span v-else>-</span>
          </template>
        </el-table-column>
        
        <el-table-column prop="detection_time" label="检测时间" min-width="160">
          <template #default="{ row }">
            <div class="time-display">
              <el-icon class="time-icon"><Timer /></el-icon>
              <span>{{ formatDateTime(row.detection_time) }}</span>
            </div>
          </template>
        </el-table-column>
        
        <el-table-column prop="user_name" label="用户" width="120" v-if="showUserColumn">
          <template #default="{ row }">
            {{ row.user_name || '-' }}
          </template>
        </el-table-column>
        
        <el-table-column label="操作" width="100" align="center">
          <template #default="{ row }">
            <el-button
              size="small"
              type="danger"
              link
              @click="deleteRecord(row)"
            >
              <el-icon><Delete /></el-icon>
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <!-- 分页 -->
      <div class="pagination">
        <el-pagination
          v-model:current-page="currentPage"
          v-model:page-size="pageSize"
          :page-sizes="[10, 20, 50, 100]"
          :total="total"
          layout="total, sizes, prev, pager, next, jumper"
          @size-change="handleSizeChange"
          @current-change="handleCurrentChange"
        />
        </div>
        </div>
      </div>
    </div>

    <!-- 图片预览对话框 -->
    <el-dialog 
      v-model="showPreview" 
      title="检测结果" 
      width="900px"
      :close-on-click-modal="false"
      class="preview-dialog"
    >
      <div class="image-preview">
        <div class="preview-header">
          <h3>检测结果图片</h3>
          <p>点击图片可以放大查看详细信息</p>
        </div>
        <div class="preview-image-container">
          <img :src="previewImageUrl" alt="检测结果" class="preview-image" />
        </div>
      </div>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted, computed } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh, Search, View, Delete, DataAnalysis, Filter, Calendar, TrendCharts, RefreshLeft, Timer } from '@element-plus/icons-vue'
import { 
  realtimeDetectionApi, 
  type RealtimeDetectionRecord,
  type DetectionSession
} from '@/api/realtime_detection'
import { modelApi, type Model, type YoloClassesConfig } from '@/api/model'
import { useUserStore } from '@/stores/user'
import { formatDateTime } from '@/utils/format'

const userStore = useUserStore()

// 响应式数据
const loading = ref(false)
const records = ref<RealtimeDetectionRecord[]>([])
const detectionSessions = ref<DetectionSession[]>([])
const availableModels = ref<Model[]>([])
const yoloClasses = ref<YoloClassesConfig>({})

// 分页数据
const currentPage = ref(1)
const pageSize = ref(20)
const total = ref(0)

// 会话分页
const currentSessionPage = ref(1)
const sessionsPerPage = 8

// 搜索参数
const searchParams = reactive({
  session_id: '',
  model_id: '',
  class_name: ''
})

// 图片预览
const showPreview = ref(false)
const previewImageUrl = ref('')

// 计算属性
const showUserColumn = computed(() => userStore.userInfo?.role === 1) // 管理员才显示用户列

// 分页的会话列表
const paginatedSessions = computed(() => {
  const startIndex = (currentSessionPage.value - 1) * sessionsPerPage
  const endIndex = startIndex + sessionsPerPage
  return detectionSessions.value.slice(startIndex, endIndex)
})

// 组件挂载时加载数据
onMounted(async () => {
  await Promise.all([
    fetchRecords(),
    fetchSessions(),
    fetchModels(),
    fetchYoloClasses()
  ])
})

// 获取检测记录
const fetchRecords = async () => {
  loading.value = true
  try {
    const params: Record<string, any> = {
      page: currentPage.value,
      page_size: pageSize.value,
      ...searchParams
    }
    
    // 过滤空值
    Object.keys(params).forEach(key => {
      if (params[key] === '' || params[key] == null) {
        delete params[key]
      }
    })
    
    console.log('发送到API的参数:', params)
    const response = await realtimeDetectionApi.getRecords(params)
    records.value = response.records
    total.value = response.total
    console.log('API返回结果:', response)
  } catch (error) {
    ElMessage.error('获取检测记录失败')
    console.error('Error fetching records:', error)
  } finally {
    loading.value = false
  }
}

// 获取检测会话
const fetchSessions = async () => {
  try {
    const response = await realtimeDetectionApi.getSessions()
    detectionSessions.value = response.sessions
  } catch (error) {
    ElMessage.error('获取检测会话失败')
    console.error('Error fetching sessions:', error)
  }
}

// 获取模型列表
const fetchModels = async () => {
  try {
    const response = await modelApi.list({ page_size: 100 })
    // 只显示已发布的模型
    availableModels.value = response.models.filter(model => model.status === 3)
  } catch (error) {
    console.error('Error fetching models:', error)
  }
}

// 获取YOLO类别配置
const fetchYoloClasses = async () => {
  try {
    const response = await modelApi.getYoloClasses()
    yoloClasses.value = response.classes
    console.log('YOLO类别配置:', yoloClasses.value)
  } catch (error) {
    console.error('Error fetching YOLO classes:', error)
  }
}

// 刷新记录
const refreshRecords = async () => {
  await Promise.all([
    fetchRecords(),
    fetchSessions()
  ])
}

// 搜索处理
const handleSearch = () => {
  currentPage.value = 1
  console.log('搜索参数:', searchParams)
  fetchRecords()
}

// 重置搜索
const resetSearch = () => {
  searchParams.session_id = ''
  searchParams.model_id = ''
  searchParams.class_name = ''
  handleSearch()
}

// 按会话过滤
const filterBySession = (sessionId: string) => {
  searchParams.session_id = sessionId
  handleSearch()
}

// 分页处理
const handleSizeChange = (size: number) => {
  pageSize.value = size
  currentPage.value = 1
  fetchRecords()
}

const handleCurrentChange = (page: number) => {
  currentPage.value = page
  fetchRecords()
}

// 会话分页处理
const handleSessionPageChange = (page: number) => {
  currentSessionPage.value = page
}

// 显示图片预览
const showImagePreview = (imageUrl: string) => {
  previewImageUrl.value = imageUrl
  showPreview.value = true
}

// 获取类别显示名称
const getClassDisplayName = (className: string) => {
  return yoloClasses.value[className]?.chinese_name || className
}

// 获取类别颜色
const getClassColor = (className: string) => {
  return yoloClasses.value[className]?.color || '#409eff'
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

// 获取置信度样式
const getConfidenceClass = (confidence: number) => {
  if (confidence >= 0.8) return 'confidence-high'
  if (confidence >= 0.6) return 'confidence-medium'
  return 'confidence-low'
}

// 删除记录
const deleteRecord = async (record: any) => {
  try {
    await ElMessageBox.confirm(
      `确定要删除此检测记录吗？`,
      '确认删除',
      {
        type: 'warning',
        confirmButtonText: '确定',
        cancelButtonText: '取消'
      }
    )
    
    await realtimeDetectionApi.deleteRecord(record.id)
    ElMessage.success('删除成功')
    
    // 刷新数据
    await refreshRecords()
    
  } catch (error) {
    if (error !== 'cancel') {
      ElMessage.error('删除失败')
      console.error('Error deleting record:', error)
    }
  }
}

// 删除会话
const deleteSession = async (session: DetectionSession) => {
  try {
    await ElMessageBox.confirm(
      `确定要删除会话 "${session.session_id}" 及其所有相关检测记录吗？\n此操作将删除 ${session.detection_count} 条检测记录。`,
      '确认删除会话',
      {
        type: 'warning',
        confirmButtonText: '确定',
        cancelButtonText: '取消',
        dangerouslyUseHTMLString: true
      }
    )
    
    const result = await realtimeDetectionApi.deleteSession(session.session_id)
    ElMessage.success(`删除成功！已删除 ${result.deleted_records_count} 条记录和 ${result.deleted_files_count} 个文件`)
    
    // 刷新数据
    await refreshRecords()
    
    // 如果当前正在按此会话过滤，清空过滤条件
    if (searchParams.session_id === session.session_id) {
      searchParams.session_id = ''
    }
    
  } catch (error) {
    if (error !== 'cancel') {
      ElMessage.error('删除会话失败')
      console.error('Error deleting session:', error)
    }
  }
}
</script>

<!--
版权所有 © 2025 羊羊小栈 (GJQ)

作者：羊羊小栈
时间：2025-09-06 23:44:27

本系统为原创作品，禁止2次销售！禁止将系统对应的视频、文档进行二次发布！
违者需立即停止侵权行为，并按照【羊羊小栈系统版权声明及保护条款】中规定数额进行赔偿，并承担相应法律责任。
-->

<style scoped>
.realtime-detection-records {
  min-height: 100vh;
  background: linear-gradient(135deg, #ffffff 0%, #fafafa 50%, #f5f5f5 100%);
  padding: 0;
}

/* 页面头部样式 */
.page-header {
  background: linear-gradient(135deg, #ffffff 0%, #f8f8f8 100%);
  color: #333333;
  padding: 32px 24px;
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.08);
  border-bottom: 1px solid #e5e5e5;
}

.header-content {
  max-width: 1400px;
  margin: 0 auto;
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  gap: 32px;
}

.header-left {
  flex: 1;
}

.page-title {
  font-size: 32px;
  font-weight: 600;
  margin: 0 0 8px 0;
  color: #333333;
}

.page-subtitle {
  font-size: 16px;
  color: #666666;
  margin: 0;
}

.header-right {
  display: flex;
  gap: 12px;
}

.refresh-btn {
  height: 36px;
  padding: 0 16px;
  font-size: 14px;
  font-weight: 500;
}

/* 主内容区域 */
.main-content {
  max-width: 1400px;
  margin: 0 auto;
  padding: 32px 24px;
  display: flex;
  flex-direction: column;
  gap: 24px;
}

/* 搜索面板 */
.search-panel {
  background: white;
  border-radius: 16px;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.08);
  overflow: hidden;
}

.search-header {
  padding: 20px 24px;
  background: linear-gradient(135deg, #ffffff 0%, #fafafa 100%);
  border-bottom: 1px solid #e5e5e5;
}

.search-title {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
  color: #333333;
  display: flex;
  align-items: center;
  gap: 8px;
}

.search-content {
  padding: 24px;
}

.search-section {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 24px;
}

.search-left {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.search-filters {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  align-items: flex-end;
}

.filter-group {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 200px;
}

.search-buttons-group {
  min-width: auto;
  flex-shrink: 0;
}

.filter-label {
  font-size: 14px;
  font-weight: 500;
  color: #666666;
}

.filter-input,
.filter-select {
  width: 100%;
}

.search-buttons {
  display: flex;
  align-items: center;
  gap: 8px;
}

.search-btn,
.clear-btn {
  min-width: 80px;
}

.search-right {
  display: flex;
  align-items: center;
  gap: 10px;
}

.stats-info {
  font-size: 14px;
  color: #606266;
}

.stats-text {
  font-weight: 500;
}

@media (max-width: 1200px) {
  .search-filters {
    flex-direction: column;
    align-items: stretch;
  }
  
  .filter-group {
    min-width: auto;
  }
}

@media (max-width: 768px) {
  .search-section {
    flex-direction: column;
    gap: 16px;
  }
  
  .search-left {
    gap: 12px;
  }
  
  .search-filters {
    gap: 12px;
  }
  
  .search-buttons {
    width: 100%;
    justify-content: center;
  }
  
  .search-btn,
  .clear-btn {
    flex: 1;
    min-width: auto;
  }
}

/* 会话概览 */
.sessions-overview {
  background: white;
  border-radius: 16px;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.08);
  overflow: hidden;
}

.overview-header {
  padding: 20px 24px;
  background: linear-gradient(135deg, #ffffff 0%, #fafafa 100%);
  border-bottom: 1px solid #e5e5e5;
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.overview-title {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
  color: #333333;
  display: flex;
  align-items: center;
  gap: 8px;
}

.sessions-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 16px;
  padding: 24px;
  min-height: 200px;
}

.sessions-pagination {
  display: flex;
  justify-content: center;
  padding: 16px 24px 24px;
  border-top: 1px solid #f0f0f0;
}

.session-card {
  background: linear-gradient(135deg, #ffffff 0%, #fafafa 100%);
  border: 1px solid #e5e5e5;
  border-radius: 12px;
  padding: 20px;
  transition: all 0.3s ease;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.04);
}

.session-card:hover {
  border-color: #cccccc;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.12);
  transform: translateY(-4px);
}

.session-active {
  border-color: #999999 !important;
  background: linear-gradient(135deg, #f8f8f8 0%, #f0f0f0 100%) !important;
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.15);
}

.session-active .session-id {
  color: #333333;
  font-weight: bold;
}

.session-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
  padding-bottom: 8px;
  border-bottom: 1px solid #f0f0f0;
}

.session-id {
  font-weight: bold;
  color: #333333;
  font-size: 14px;
  cursor: pointer;
  transition: color 0.3s;
}

.session-id:hover {
  color: #666666;
}

.session-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}


.session-stats {
  display: flex;
  flex-direction: column;
  gap: 12px;
  cursor: pointer;
}

.stat-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
}

.stat-item {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  flex: 1;
}

.stat-label {
  color: #666666;
}

.stat-value {
  font-weight: 500;
  color: #333333;
}

.no-sessions {
  text-align: center;
  padding: 48px 24px;
  color: #999999;
}

.empty-icon {
  font-size: 48px;
  color: #cccccc;
  margin-bottom: 16px;
}

.no-sessions p {
  font-size: 16px;
  font-weight: 500;
  margin: 0 0 8px 0;
}

.no-sessions small {
  font-size: 14px;
  color: #bbbbbb;
}

/* 记录区域 */
.records-section {
  background: white;
  border-radius: 16px;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.08);
  overflow: hidden;
  width: 100%;
}

.records-header {
  padding: 20px 24px;
  background: linear-gradient(135deg, #ffffff 0%, #fafafa 100%);
  border-bottom: 1px solid #e5e5e5;
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.records-title {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
  color: #333333;
  display: flex;
  align-items: center;
  gap: 8px;
}

.records-info {
  display: flex;
  gap: 12px;
}

.records-table {
  padding: 0;
  width: 100%;
}

.records-table :deep(.el-table) {
  border-radius: 0;
  width: 100%;
  table-layout: auto;
}

.records-table :deep(.el-table__body-wrapper) {
  overflow-x: auto;
}

.records-table :deep(.el-table__header) {
  background: #fafafa;
}

.records-table :deep(.el-table th) {
  background: #fafafa !important;
  color: #333333;
  font-weight: 600;
  border: none;
  text-align: center;
}

.records-table :deep(.el-table td) {
  border: none;
  padding: 16px 12px;
}

.time-display {
  display: flex;
  align-items: center;
  gap: 6px;
  color: #666666;
}

.time-icon {
  font-size: 14px;
  color: #999999;
}

.records-table :deep(.el-table__row:hover td) {
  background: #f8f8f8;
}

.pagination {
  display: flex;
  justify-content: center;
  margin-top: 20px;
  margin-bottom: 32px;
  width: 100%;
  padding: 16px 24px;
}

/* 图片预览对话框样式 */
.preview-dialog .el-dialog__body {
  padding: 20px 24px;
}

.image-preview {
  text-align: center;
}

.preview-header {
  margin-bottom: 20px;
  padding-bottom: 16px;
  border-bottom: 1px solid #e4e7ed;
}

.preview-header h3 {
  margin: 0 0 8px 0;
  font-size: 18px;
  color: #303133;
  font-weight: 600;
}

.preview-header p {
  margin: 0;
  font-size: 14px;
  color: #606266;
}

.preview-image-container {
  background: #f8f9fa;
  border-radius: 8px;
  padding: 20px;
  border: 1px solid #e4e7ed;
}

.preview-image {
  max-width: 100%;
  height: auto;
  border-radius: 6px;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1);
  transition: transform 0.3s ease;
}

.preview-image:hover {
  transform: scale(1.02);
}

.confidence-high {
  color: #22c55e;
  font-weight: bold;
}

.confidence-medium {
  color: #f59e0b;
  font-weight: 500;
}

.confidence-low {
  color: #ef4444;
}

/* 响应式设计 */
@media (max-width: 1200px) {
  .sessions-grid {
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  }
}

@media (max-width: 1024px) {
  .main-content {
    padding: 24px 16px;
  }
  
  .sessions-grid {
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  }
}

@media (max-width: 768px) {
  .page-header {
    padding: 24px 16px;
  }
  
  .header-content {
    flex-direction: column;
    align-items: stretch;
    gap: 20px;
  }
  
  .page-title {
    font-size: 24px;
  }
  
  .refresh-btn {
    width: 100%;
  }
  
  .main-content {
    padding: 16px;
    gap: 16px;
  }
  
  .sessions-grid {
    grid-template-columns: 1fr;
    padding: 16px;
  }
  
  .records-table :deep(.el-table td) {
    padding: 12px 8px;
    font-size: 14px;
  }
  
  .pagination {
    padding: 16px;
  }
  
  .sessions-pagination {
    padding: 12px 16px 16px;
  }
}

@media (max-width: 480px) {
  .page-title {
    font-size: 20px;
  }
  
  .sessions-grid {
    padding: 12px;
  }
  
  .session-card {
    padding: 16px;
  }
}
</style>
