<!-- MD5: f0fddd3ceaa2181d731db6c5d66abc43 -->
<!--
版权所有©2025羊羊小栈（GJQ）

作者：羊羊小栈
时间：2025-09-06 23:44:27

本系统为原创作品，禁止二次销售！禁止将系统对应的视频、文档进行二次发布！
违者需立即停止侵权行为，并按照【羊羊小栈系统版权声明及保护条款】中规定数额进行赔偿，并承担相应法律责任。
-->

<template>
  <div class="video-detection-records">
    <!-- 页面头部 -->
    <div class="page-header">
      <div class="header-content">
        <div class="header-left">
          <h1 class="page-title">视频识别记录</h1>
          <p class="page-subtitle">查看和管理所有视频识别历史记录</p>
        </div>
        <div class="header-right">
          <el-button type="primary" @click="$router.push('/video-detection')" class="new-btn">
            <el-icon><Plus /></el-icon>
            新建识别
          </el-button>
        </div>
      </div>
    </div>

    <!-- 主要内容区域 -->
    <div class="main-content">
      <!-- 搜索和统计区域 -->
      <el-card class="search-card" shadow="never">
        <div class="search-section">
          <div class="search-left">
            <el-input
              v-model="searchQuery"
              placeholder="搜索标题、描述或模型名称"
              class="search-input"
              @input="handleSearch"
              @keyup.enter="handleSearch"
              clearable
            >
              <template #prefix>
                <el-icon><Search /></el-icon>
              </template>
            </el-input>
            <div class="search-buttons">
              <el-button 
                type="primary" 
                @click="handleSearch"
                :loading="loading"
                class="search-btn"
              >
                <el-icon><Search /></el-icon>
                搜索
              </el-button>
              <el-button 
                @click="handleClear"
                class="clear-btn"
              >
                <el-icon><RefreshLeft /></el-icon>
                清空
              </el-button>
              <el-button 
                @click="handleRefresh"
                :loading="loading"
                class="refresh-btn"
              >
                <el-icon><Refresh /></el-icon>
                刷新
              </el-button>
            </div>
          </div>
          <div class="search-right">
            <div class="stats-info">
              <span class="stats-text">共 {{ total }} 条记录</span>
            </div>
          </div>
        </div>
      </el-card>

      <!-- 记录列表 -->
      <el-card class="records-card" shadow="never">
        <el-table
          :data="recordList"
          v-loading="loading"
          class="records-table"
          border
          stripe
        >
          <el-table-column prop="title" label="标题" width="200" show-overflow-tooltip>
            <template #default="scope">
              <div class="title-cell">
                <span class="title-text">{{ scope.row.title }}</span>
              </div>
            </template>
          </el-table-column>
          
          <el-table-column prop="model_name" label="使用模型" width="150">
            <template #default="scope">
              <el-tag type="primary" size="small">{{ scope.row.model_name }}</el-tag>
            </template>
          </el-table-column>
          
          <el-table-column v-if="userStore.isAdmin()" prop="user_name" label="用户" width="120">
            <template #default="scope">
              <el-tag type="info" size="small">{{ scope.row.user_name }}</el-tag>
            </template>
          </el-table-column>
          
          <el-table-column label="处理进度" width="140" align="center">
            <template #default="scope">
              <div class="progress-cell">
                <div v-if="scope.row.status === VIDEO_DETECTION_STATUS.PROCESSING" class="progress-info">
                  <el-progress 
                    :percentage="getProgress(scope.row)" 
                    :stroke-width="6"
                    :show-text="false"
                    class="mini-progress"
                  />
                  <span class="progress-text">{{ getProgress(scope.row).toFixed(1) }}%</span>
                </div>
                <div v-else-if="scope.row.status === VIDEO_DETECTION_STATUS.COMPLETED" class="completion-info">
                  <el-tag type="success" size="small">
                    <el-icon><Check /></el-icon>
                    完成
                  </el-tag>
                  <div class="frame-info">
                    {{ scope.row.processed_frames || 0 }}/{{ scope.row.total_frames || 0 }}
                  </div>
                </div>
                <div v-else class="status-info">
                  <el-tag :type="getVideoStatusType(scope.row.status)" size="small">
                    {{ getVideoStatusText(scope.row.status) }}
                  </el-tag>
                </div>
              </div>
            </template>
          </el-table-column>
          
          <el-table-column prop="description" label="描述" show-overflow-tooltip>
            <template #default="scope">
              <span class="description-text">{{ scope.row.description || '-' }}</span>
            </template>
          </el-table-column>
          
          <el-table-column label="操作" width="240" align="center" fixed="right">
            <template #default="scope">
              <div class="action-buttons">
                <el-button size="small" @click="viewRecord(scope.row)" class="action-btn view-btn">
                  <el-icon><View /></el-icon>
                  查看
                </el-button>
                <el-button size="small" @click="editRecord(scope.row)" class="action-btn edit-btn">
                  <el-icon><Edit /></el-icon>
                  编辑
                </el-button>
                <el-button 
                  size="small" 
                  type="danger" 
                  @click="deleteRecord(scope.row.id, scope.row.title)"
                  class="action-btn delete-btn"
                >
                  <el-icon><Delete /></el-icon>
                  删除
                </el-button>
              </div>
            </template>
          </el-table-column>
        </el-table>

        <!-- 分页器 -->
        <div class="pagination-wrapper">
          <el-pagination
            v-model:current-page="currentPage"
            v-model:page-size="pageSize"
            :page-sizes="[10, 20, 50, 100]"
            :total="total"
            layout="total, sizes, prev, pager, next, jumper"
            @size-change="handleSizeChange"
            @current-change="handleCurrentChange"
            class="pagination"
          />
        </div>
      </el-card>
    </div>

    <!-- 查看记录对话框 -->
    <el-dialog 
      v-model="showViewDialog" 
      title="视频识别记录详情" 
      width="90%"
      :close-on-click-modal="false"
      class="view-dialog"
      :style="{ 'max-width': '1400px' }"
    >
      <div v-if="currentRecord" class="record-detail">
        <!-- 基本信息 -->
        <div class="detail-section">
          <div class="section-header">
            <el-icon class="section-icon"><InfoFilled /></el-icon>
            <h3 class="section-title">基本信息</h3>
          </div>
          <div class="info-grid">
            <div class="info-item">
              <div class="info-label">标题</div>
              <div class="info-value info-text-primary">{{ currentRecord.title }}</div>
            </div>
            <div class="info-item">
              <div class="info-label">使用模型</div>
              <div class="info-value info-text-success">{{ currentRecord.model_name }}</div>
            </div>
            <div class="info-item">
              <div class="info-label">处理状态</div>
              <div class="info-value" :class="'info-text-' + getVideoStatusType(currentRecord.status)">
                {{ getVideoStatusText(currentRecord.status) }}
              </div>
            </div>
            <div v-if="userStore.isAdmin() && currentRecord.user_name" class="info-item">
              <div class="info-label">用户</div>
              <div class="info-value info-text-default">{{ currentRecord.user_name }}</div>
            </div>
            <div class="info-item">
              <div class="info-label">置信度阈值</div>
              <div class="info-value info-text-warning">{{ (currentRecord.confidence_threshold * 100).toFixed(0) }}%</div>
            </div>
            <div class="info-item">
              <div class="info-label">帧率设置</div>
              <div class="info-value info-text-info">{{ currentRecord.frame_rate }} fps</div>
            </div>
            <div class="info-item">
              <div class="info-label">视频总帧数</div>
              <div class="info-value info-text-primary">{{ currentRecord.total_frames || '-' }}</div>
            </div>
            <div class="info-item">
              <div class="info-label">当前读取帧数</div>
              <div class="info-value info-text-info">{{ currentRecord.processed_frames || 0 }}</div>
            </div>
            <div class="info-item">
              <div class="info-label">需要处理帧数</div>
              <div class="info-value info-text-warning">{{ currentRecord.target_frames || currentRecord.total_frames || '-' }}</div>
            </div>
            <div class="info-item">
              <div class="info-label">实际已处理帧数</div>
              <div class="info-value info-text-success">{{ currentRecord.actual_processed_frames || 0 }}</div>
            </div>
            <div class="info-item">
              <div class="info-label">读取进度</div>
              <div class="info-value info-text-info">{{ getReadProgress(currentRecord).toFixed(1) }}%</div>
            </div>
            <div class="info-item">
              <div class="info-label">处理进度</div>
              <div class="info-value info-text-success">{{ getProcessingProgress(currentRecord).toFixed(1) }}%</div>
            </div>
            <div class="info-item">
              <div class="info-label">创建时间</div>
              <div class="info-value info-text-time">{{ formatDateTime(currentRecord.create_time) }}</div>
            </div>
            <div class="info-item">
              <div class="info-label">开始时间</div>
              <div class="info-value info-text-time">{{ currentRecord.start_time ? formatDateTime(currentRecord.start_time) : '-' }}</div>
            </div>
            <div class="info-item">
              <div class="info-label">完成时间</div>
              <div class="info-value info-text-time">{{ currentRecord.complete_time ? formatDateTime(currentRecord.complete_time) : '-' }}</div>
            </div>
          </div>
          <div v-if="currentRecord.description" class="description-section">
            <div class="info-label">描述</div>
            <div class="info-value description-text">{{ currentRecord.description }}</div>
          </div>
        </div>

        <!-- 视频内容 -->
        <div class="detail-section">
          <div class="section-header">
            <el-icon class="section-icon"><VideoPlay /></el-icon>
            <h3 class="section-title">视频内容</h3>
          </div>
          
          <div class="video-content">
            <div class="video-container">
              <div class="video-header">
                <h4>原始视频</h4>
                <el-tag type="info" size="small">输入</el-tag>
              </div>
              <div class="video-wrapper">
                <video v-if="currentRecord.original_video_url" :src="currentRecord.original_video_url" controls class="video-player" />
              </div>
            </div>
            
            <div v-if="currentRecord.result_video_url" class="video-container">
              <div class="video-header">
                <h4>识别结果视频</h4>
                <el-tag type="success" size="small">输出</el-tag>
              </div>
              <div class="video-wrapper">
                <video :src="currentRecord.result_video_url" controls class="video-player" />
              </div>
            </div>
          </div>
        </div>

        <!-- 识别结果摘要 -->
        <div v-if="getDetectionSummary(currentRecord)" class="detail-section">
          <div class="section-header">
            <el-icon class="section-icon"><DataAnalysis /></el-icon>
            <h3 class="section-title">识别结果摘要</h3>
          </div>
          
          <div class="detection-summary">
            <div class="summary-stats">
              <div class="stat-item">
                <div class="stat-label">总检测数</div>
                <div class="stat-value total-detections">{{ getDetectionSummary(currentRecord).total_detections || 0 }}</div>
              </div>
              <div class="stat-item">
                <div class="stat-label">平均每帧检测数</div>
                <div class="stat-value avg-detections">{{ (getDetectionSummary(currentRecord).avg_detections_per_frame || 0).toFixed(2) }}</div>
              </div>
            </div>
            
            <div class="classes-section">
              <div class="classes-label">检测到的类别</div>
              <div class="classes-tags">
                <el-tag 
                  v-for="cls in getDetectionSummary(currentRecord).classes_detected" 
                  :key="cls" 
                  :style="{ backgroundColor: getClassColor(cls), color: '#fff', borderColor: getClassColor(cls) }"
                  class="class-tag"
                >
                  {{ getClassDisplayName(cls) }}
                </el-tag>
                <span v-if="!getDetectionSummary(currentRecord).classes_detected?.length" class="no-classes">无检测类别</span>
              </div>
            </div>
            
            <!-- 帧检测详情 -->
            <div v-if="getFrameDetectionsWithResults(currentRecord).length > 0" class="frame-detections">
              <div class="frame-detections-header">
                <h5>检测结果详情</h5>
                <div class="frame-detections-info">
                  共 {{ getFrameDetectionsWithResults(currentRecord).length }} 帧有检测结果
                  <span v-if="getFrameDetectionsWithResults(currentRecord).length >= 100">（显示前100帧）</span>
                </div>
              </div>
              
              <div class="frame-results-container">
                <div class="frame-results-scroll">
                  <div 
                    v-for="frameData in getFrameDetectionsWithResults(currentRecord)" 
                    :key="frameData.frame"
                    class="frame-result-item"
                  >
                    <div class="frame-header">
                      <span class="frame-number">第 {{ frameData.frame }} 帧</span>
                      <span class="detection-count">{{ frameData.detections.length }} 个检测结果</span>
                    </div>
                    
                    <div class="detections-list">
                      <div 
                        v-for="(detection, index) in frameData.detections" 
                        :key="index"
                        class="detection-item"
                      >
                        <span 
                          class="class-indicator"
                          :style="{ backgroundColor: getClassColor(detection.class_name) }"
                        ></span>
                        <span class="class-name">{{ getClassDisplayName(detection.class_name) }}</span>
                        <span class="confidence">{{ (detection.confidence * 100).toFixed(1) }}%</span>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </el-dialog>

    <!-- 编辑记录对话框 -->
    <el-dialog v-model="showEditDialog" title="编辑记录" width="500px" @close="resetEditForm" class="edit-dialog">
      <el-form
        ref="editFormRef"
        :model="editForm"
        :rules="editRules"
        label-width="80px"
      >
        <el-form-item label="标题" prop="title">
          <el-input v-model="editForm.title" placeholder="请输入标题" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input
            v-model="editForm.description"
            type="textarea"
            placeholder="请输入描述"
            :rows="3"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <span class="dialog-footer">
          <el-button @click="showEditDialog = false">取消</el-button>
          <el-button type="primary" @click="handleEdit" :loading="editLoading">
            保存
          </el-button>
        </span>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, reactive } from 'vue'
import { ElMessage, ElMessageBox, ElForm } from 'element-plus'
import { 
  Plus, 
  Search, 
  View, 
  Edit, 
  Delete, 
  Check, 
  InfoFilled, 
  VideoPlay, 
  DataAnalysis,
  RefreshLeft,
  Refresh
} from '@element-plus/icons-vue'
import { 
  videoDetectionApi, 
  type VideoDetectionRecord, 
  type UpdateVideoDetectionRequest,
  VIDEO_DETECTION_STATUS,
  getVideoStatusText,
  getVideoStatusType
} from '@/api/video_detection'
import { modelApi, type YoloClassesConfig } from '@/api/model'
import { useUserStore } from '@/stores/user'

// 使用用户store
const userStore = useUserStore()

// 响应式数据
const loading = ref(false)
const recordList = ref<VideoDetectionRecord[]>([])
const total = ref(0)
const currentPage = ref(1)
const pageSize = ref(10)
const searchQuery = ref('')
const yoloClasses = ref<YoloClassesConfig>({})

// 对话框显示状态
const showViewDialog = ref(false)
const showEditDialog = ref(false)

// 表单数据
const editForm = reactive<UpdateVideoDetectionRequest>({
  title: '',
  description: ''
})

// 表单引用
const editFormRef = ref<InstanceType<typeof ElForm>>()

// 加载状态
const editLoading = ref(false)

// 当前操作的记录
const currentRecord = ref<VideoDetectionRecord | null>(null)
const currentEditId = ref('')

// 表单验证规则
const editRules = {
  title: [
    { required: true, message: '请输入标题', trigger: 'blur' }
  ]
}

// 获取YOLO类别配置
const fetchYoloClasses = async () => {
  try {
    const response = await modelApi.getYoloClasses()
    yoloClasses.value = response.classes
  } catch (error) {
    console.error('Error fetching YOLO classes:', error)
  }
}

// 获取记录列表
const fetchRecords = async () => {
  try {
    loading.value = true
    const response = await videoDetectionApi.getRecords({
      page: currentPage.value,
      page_size: pageSize.value,
      search: searchQuery.value
    })
    recordList.value = response.records
    total.value = response.total
  } catch (error) {
    ElMessage.error('获取记录列表失败')
    console.error('Error fetching records:', error)
  } finally {
    loading.value = false
  }
}

// 格式化时间
const formatDateTime = (dateTime: string): string => {
  return new Date(dateTime).toLocaleString('zh-CN')
}

// 格式化日期
const formatDate = (dateTime: string): string => {
  return new Date(dateTime).toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric' })
}

// 格式化时间
const formatTime = (dateTime: string): string => {
  return new Date(dateTime).toLocaleTimeString('zh-CN', { hour: 'numeric', minute: 'numeric' })
}

// 计算处理进度
const getProgress = (record: any): number => {
  if (!record.total_frames || record.total_frames === 0) return 0
  return Math.min(100, (record.processed_frames / record.total_frames) * 100)
}

// 获取读取进度
const getReadProgress = (record: VideoDetectionRecord): number => {
  if (!record.total_frames || record.total_frames === 0) return 0
  return Math.min(100, (record.processed_frames / record.total_frames) * 100)
}

// 获取处理进度
const getProcessingProgress = (record: VideoDetectionRecord): number => {
  const targetFrames = record.target_frames || record.total_frames || 0
  const actualProcessed = record.actual_processed_frames || 0
  if (targetFrames === 0) return 0
  return Math.min(100, (actualProcessed / targetFrames) * 100)
}

// 获取检测结果摘要
const getDetectionSummary = (record: VideoDetectionRecord): any => {
  try {
    const result = JSON.parse(record.detection_result)
    return result.summary || {}
  } catch {
    return {}
  }
}

// 获取帧检测详情
const getFrameDetections = (record: VideoDetectionRecord): any[] => {
  try {
    const result = JSON.parse(record.detection_result)
    return result.detections_per_frame || []
  } catch {
    return []
  }
}

// 获取前100帧有检测结果的帧
const getFrameDetectionsWithResults = (record: VideoDetectionRecord): any[] => {
  try {
    const result = JSON.parse(record.detection_result)
    const allFrames = result.detections_per_frame || []
    // 过滤出有检测结果的帧，取前100帧
    return allFrames
      .filter((frame: any) => frame.detections && frame.detections.length > 0)
      .slice(0, 100)
  } catch {
    return []
  }
}

// 获取类别的中文名称
const getClassDisplayName = (className: string): string => {
  const classInfo = yoloClasses.value[className]
  return classInfo?.chinese_name || className
}

// 获取类别的颜色
const getClassColor = (className: string): string => {
  const classInfo = yoloClasses.value[className]
  return classInfo?.color || '#409eff'
}

// 搜索处理
let searchTimeout: NodeJS.Timeout | null = null
const handleSearch = () => {
  if (searchTimeout) {
    clearTimeout(searchTimeout)
  }
  searchTimeout = setTimeout(() => {
    currentPage.value = 1
    fetchRecords()
  }, 500)
}

// 清空搜索
const handleClear = () => {
  searchQuery.value = ''
  currentPage.value = 1
  fetchRecords()
}

// 刷新数据
const handleRefresh = () => {
  fetchRecords()
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

// 查看记录
const viewRecord = async (record: any) => {
  try {
    const response = await videoDetectionApi.getRecord(record.id)
    currentRecord.value = response
    showViewDialog.value = true
  } catch (error) {
    ElMessage.error('获取记录详情失败')
  }
}

// 编辑记录
const editRecord = (record: any) => {
  currentEditId.value = record.id
  editForm.title = record.title
  editForm.description = record.description || ''
  showEditDialog.value = true
}

const handleEdit = async () => {
  if (!editFormRef.value) return
  
  const valid = await editFormRef.value.validate().catch(() => false)
  if (!valid) return

  try {
    editLoading.value = true
    await videoDetectionApi.updateRecord(currentEditId.value, editForm)
    ElMessage.success('更新成功')
    showEditDialog.value = false
    resetEditForm()
    fetchRecords()
  } catch (error: any) {
    ElMessage.error(error.response?.data?.message || '更新失败')
  } finally {
    editLoading.value = false
  }
}

// 重置编辑表单
const resetEditForm = () => {
  Object.assign(editForm, {
    title: '',
    description: ''
  })
  editFormRef.value?.clearValidate()
}

// 删除记录
const deleteRecord = async (id: string, title: string) => {
  try {
    await ElMessageBox.confirm(`确定要删除记录"${title}"吗？这个操作不能撤销。`, '警告', {
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      type: 'warning'
    })
    
    await videoDetectionApi.deleteRecord(id)
    ElMessage.success('删除成功')
    fetchRecords()
  } catch (error: any) {
    if (error !== 'cancel') {
      ElMessage.error(error.response?.data?.message || '删除失败')
    }
  }
}

// 初始化
onMounted(() => {
  fetchYoloClasses()
  fetchRecords()
})
</script>

<style scoped>
.video-detection-records {
  padding: 20px;
  background-color: #ffffff;
  min-height: 100vh;
}

.page-header {
  background-color: #ffffff;
  padding: 30px 0;
  margin-bottom: 30px;
  border-radius: 12px;
  border: 1px solid #f0f2f5;
}

.header-content {
  display: flex;
  justify-content: space-between;
  align-items: center;
  max-width: 1200px;
  margin: 0 auto;
  padding: 0 20px;
}

.header-left {
  flex: 1;
}

.page-title {
  margin: 0 0 5px 0;
  font-size: 28px;
  color: #303133;
  font-weight: bold;
}

.page-subtitle {
  font-size: 16px;
  color: #606266;
}

.header-right {
  flex-shrink: 0;
}

.new-btn {
  margin-left: 20px;
}

.main-content {
  max-width: 1200px;
  margin: 0 auto;
}

.search-card {
  margin-bottom: 30px;
  border-radius: 12px;
  border: 1px solid #f0f2f5;
  box-shadow: 0 2px 8px 0 rgba(0, 0, 0, 0.06);
}

.search-section {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 20px;
  padding: 20px;
}

.search-left {
  flex: 1;
  display: flex;
  align-items: center;
  gap: 16px;
}

.search-input {
  flex: 1;
  max-width: 400px;
}

.search-buttons {
  display: flex;
  align-items: center;
  gap: 8px;
}

.search-btn {
  min-width: 80px;
}

.clear-btn {
  min-width: 80px;
}

.refresh-btn {
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

.records-card {
  margin-bottom: 30px;
  border-radius: 12px;
  border: 1px solid #f0f2f5;
  box-shadow: 0 2px 8px 0 rgba(0, 0, 0, 0.06);
}

.records-table {
  width: 100%;
  background-color: #ffffff;
}

.title-cell {
  display: flex;
  align-items: center;
}

.title-text {
  font-size: 16px;
  color: #303133;
  font-weight: bold;
  margin-right: 10px;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.desc-tag {
  margin-left: 10px;
}

.records-table .el-table__cell {
  padding: 12px 0;
}

.records-table .el-table__cell:first-child {
  padding-left: 20px;
}

.records-table .el-table__cell:last-child {
  padding-right: 20px;
}

.pagination-wrapper {
  text-align: right;
  margin-top: 20px;
}

.pagination .el-pagination__total,
.pagination .el-pagination__sizes,
.pagination .el-pagination__jump {
  margin-right: 10px;
}

/* 查看对话框样式 */
.view-dialog .el-dialog {
  max-width: 1400px;
  width: 90%;
  margin: 0 auto;
}

.view-dialog .el-dialog__body {
  padding: 20px 24px;
  max-height: 85vh;
  overflow-y: auto;
}

.record-detail {
  max-height: 80vh;
  overflow-y: auto;
}

/* 分区样式 */
.detail-section {
  margin-bottom: 32px;
  background: #fafafa;
  border-radius: 12px;
  padding: 24px;
  border: 1px solid #f0f0f0;
}

.detail-section:last-child {
  margin-bottom: 0;
}

/* 分区头部 */
.section-header {
  display: flex;
  align-items: center;
  margin-bottom: 20px;
  padding-bottom: 12px;
  border-bottom: 2px solid #e4e7ed;
}

.section-icon {
  font-size: 20px;
  color: #409eff;
  margin-right: 8px;
}

.section-title {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
  color: #303133;
}

/* 信息网格 */
.info-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 16px;
  margin-bottom: 16px;
}

.info-item {
  display: flex;
  flex-direction: column;
  padding: 18px;
  background: linear-gradient(135deg, #ffffff 0%, #f8f9fa 100%);
  border-radius: 10px;
  border: 1px solid #ebeef5;
  transition: all 0.3s ease;
  position: relative;
  overflow: hidden;
}

.info-item::before {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  width: 4px;
  height: 100%;
  background: #409eff;
  border-radius: 0 8px 8px 0;
  opacity: 0;
  transition: opacity 0.3s ease;
}

.info-item:hover {
  border-color: #c6e2ff;
  box-shadow: 0 6px 16px rgba(64, 158, 255, 0.12);
  transform: translateY(-2px);
}

.info-item:hover::before {
  opacity: 1;
}

.info-label {
  font-size: 12px;
  color: #909399;
  margin-bottom: 10px;
  font-weight: 500;
  text-transform: uppercase;
  letter-spacing: 0.8px;
}

.info-value {
  font-size: 16px;
  color: #303133;
  font-weight: 600;
  line-height: 1.5;
  word-break: break-word;
}

.description-section {
  margin-top: 16px;
  padding: 16px;
  background: white;
  border-radius: 8px;
  border: 1px solid #e4e7ed;
}

.description-text {
  line-height: 1.6;
  color: #606266;
}

/* 视频内容区域 */
.video-content {
  display: flex;
  gap: 20px;
  margin: 20px 0;
  flex-wrap: wrap;
}

.video-container {
  flex: 1;
  min-width: 400px;
  margin-bottom: 20px;
}

.video-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 15px;
  padding-bottom: 10px;
  border-bottom: 1px solid #ebeef5;
}

.video-header h4 {
  margin: 0;
  color: #303133;
  font-size: 16px;
  font-weight: 600;
}

.video-header .el-tag {
  margin-left: 10px;
}

.video-wrapper {
  width: 100%;
  min-height: 300px;
  max-height: 500px;
  border: 1px solid #eee;
  border-radius: 8px;
  overflow: hidden;
  background: white;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
  display: flex;
  align-items: center;
  justify-content: center;
}

.video-player {
  width: 100%;
  height: auto;
  max-height: 500px;
  object-fit: contain;
  display: block;
}

/* 检测摘要区域 */
.detection-summary {
  background: white;
  border-radius: 8px;
  padding: 20px;
  border: 1px solid #e4e7ed;
}

.summary-stats {
  display: flex;
  gap: 32px;
  margin-bottom: 24px;
  padding: 20px;
  background: #f8f9fa;
  border-radius: 8px;
  border: 1px solid #e4e7ed;
}

.stat-item {
  display: flex;
  flex-direction: column;
  align-items: center;
  flex: 1;
  min-width: 160px;
}

.stat-label {
  font-size: 13px;
  color: #606266;
  margin-bottom: 8px;
  font-weight: 500;
  text-align: center;
}

.stat-value {
  font-size: 28px;
  font-weight: bold;
  text-align: center;
  line-height: 1.2;
}

.stat-value.total-detections {
  color: #67c23a;
}

.stat-value.avg-detections {
  color: #e6a23c;
}

.classes-section {
  margin-bottom: 20px;
}

.classes-label {
  font-size: 14px;
  color: #606266;
  margin-bottom: 12px;
  font-weight: 500;
}

.classes-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.class-tag {
  margin: 0;
}

/* 帧检测详情样式 */
.frame-detections {
  margin-top: 24px;
}

.frame-detections-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
  padding-bottom: 12px;
  border-bottom: 1px solid #e4e7ed;
}

.frame-detections-header h5 {
  margin: 0;
  color: #303133;
  font-size: 16px;
  font-weight: 600;
}

.frame-detections-info {
  font-size: 14px;
  color: #606266;
  font-weight: 500;
}

.frame-results-container {
  background: white;
  border-radius: 8px;
  border: 1px solid #e4e7ed;
  overflow: hidden;
}

.frame-results-scroll {
  max-height: 400px;
  overflow-y: auto;
  padding: 8px;
}

/* 滚动条样式 */
.frame-results-scroll::-webkit-scrollbar {
  width: 6px;
}

.frame-results-scroll::-webkit-scrollbar-track {
  background: #f8f9fa;
  border-radius: 3px;
}

.frame-results-scroll::-webkit-scrollbar-thumb {
  background: #c0c4cc;
  border-radius: 3px;
}

.frame-results-scroll::-webkit-scrollbar-thumb:hover {
  background: #909399;
}

.frame-result-item {
  margin-bottom: 16px;
  padding: 12px;
  background: #fafbfc;
  border-radius: 6px;
  border: 1px solid #f0f2f5;
  transition: all 0.2s ease;
}

.frame-result-item:hover {
  background: #f8f9fa;
  border-color: #e4e7ed;
  transform: translateX(2px);
}

.frame-result-item:last-child {
  margin-bottom: 8px;
}

.frame-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}

.frame-number {
  font-size: 14px;
  font-weight: 600;
  color: #409eff;
}

.detection-count {
  font-size: 13px;
  color: #909399;
  font-weight: 500;
}

.detections-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.detection-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 8px;
  background: white;
  border-radius: 4px;
  border: 1px solid #f0f2f5;
  font-size: 13px;
}

.class-indicator {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}

.class-name {
  flex: 1;
  color: #303133;
  font-weight: 500;
}

.confidence {
  color: #67c23a;
  font-weight: 600;
  font-size: 12px;
}

.no-classes {
  color: #909399;
  font-style: italic;
}

.dialog-footer button:first-child {
  margin-right: 10px;
}

/* 进度单元格样式 */
.progress-cell {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
}

.progress-info {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 5px;
  width: 100%;
}

.mini-progress {
  width: 100%;
}

.progress-text {
  font-size: 12px;
  color: #409eff;
  font-weight: bold;
}

.completion-info {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 5px;
}

.frame-info {
  font-size: 12px;
  color: #67c23a;
  font-weight: bold;
}

.status-info {
  display: flex;
  justify-content: center;
}

/* 时间单元格样式 */
.time-cell {
  display: flex;
  flex-direction: column;
  align-items: center;
  font-size: 14px;
  color: #606266;
}

.time-date {
  font-size: 16px;
  color: #303133;
  font-weight: bold;
}

.time-time {
  font-size: 12px;
  color: #909399;
}

/* 描述内容样式 */
.description-content {
  color: #606266;
  line-height: 1.6;
  word-break: break-all;
}

/* 无类别提示样式 */
.no-classes {
  color: #909399;
  font-style: italic;
}

/* 中等屏幕适配 */
@media (max-width: 1200px) {
  .view-dialog .el-dialog {
    width: 95%;
    max-width: 1000px;
  }

  .info-grid {
    grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
  }

  .video-container {
    min-width: 350px;
    margin-bottom: 15px;
  }
}

@media (max-width: 768px) {
  .header-content {
    flex-direction: column;
    align-items: flex-start;
    gap: 10px;
  }

  .new-btn {
    margin-left: 0;
  }

  .search-section {
    flex-direction: column;
    align-items: stretch;
    gap: 10px;
  }

  .search-left {
    flex-direction: column;
    gap: 10px;
  }

  .search-input {
    width: 100%;
    max-width: none;
  }

  .search-buttons {
    width: 100%;
    justify-content: center;
  }

  .search-btn,
  .clear-btn,
  .refresh-btn {
    flex: 1;
    min-width: auto;
  }

  .video-content {
    flex-direction: column;
  }
  
  .video-container {
    min-width: unset;
    width: 100%;
    margin-bottom: 15px;
  }
  
  .video-player {
    max-height: 300px;
    width: 100%;
    height: auto;
  }

  /* 移动端对话框样式优化 */
  .view-dialog .el-dialog {
    width: 95%;
    max-width: none;
    margin: 0 auto;
  }

  .view-dialog .el-dialog__body {
    padding: 15px;
    max-height: 85vh;
  }

  .record-detail {
    max-height: 80vh;
  }

  .detail-section {
    padding: 16px;
    margin-bottom: 20px;
  }

  .info-grid {
    grid-template-columns: 1fr;
    gap: 12px;
  }

  .info-item {
    padding: 12px;
  }

  .section-header {
    margin-bottom: 15px;
    padding-bottom: 8px;
  }

  .section-title {
    font-size: 16px;
  }

  .video-wrapper {
    min-height: 200px;
    max-height: 300px;
  }

  .frame-table-container {
    padding: 12px;
  }

  .summary-stats {
    flex-direction: column;
    gap: 16px;
    padding: 16px;
  }

  .stat-item {
    min-width: auto;
  }

  .stat-value {
    font-size: 24px;
  }

  /* 帧检测详情移动端样式 */
  .frame-detections-header {
    flex-direction: column;
    align-items: flex-start;
    gap: 8px;
  }

  .frame-results-scroll {
    max-height: 300px;
    padding: 6px;
  }

  .frame-result-item {
    padding: 10px;
    margin-bottom: 12px;
  }

  .frame-header {
    flex-direction: column;
    align-items: flex-start;
    gap: 4px;
  }

  .detection-item {
    padding: 6px 10px;
    font-size: 12px;
  }

  .class-name {
    font-size: 12px;
  }

  .confidence {
    font-size: 11px;
  }
}

/* 简约信息文字样式 */
.info-text-primary {
  color: #409eff;
  font-weight: 600;
  font-size: 16px;
}

.info-text-success {
  color: #67c23a;
  font-weight: 600;
  font-size: 16px;
}

.info-text-warning {
  color: #e6a23c;
  font-weight: 600;
  font-size: 16px;
}

.info-text-danger {
  color: #f56c6c;
  font-weight: 600;
  font-size: 16px;
}

.info-text-info {
  color: #909399;
  font-weight: 600;
  font-size: 16px;
}

.info-text-default {
  color: #303133;
  font-weight: 600;
  font-size: 16px;
}

.info-text-time {
  color: #606266;
  font-weight: 500;
  font-size: 15px;
  line-height: 1.4;
}
</style>
