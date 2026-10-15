<!-- MD5: d928f4e111ba9947d1be0d5a9999604a -->
<!--
版权所有 © 2025 羊羊小栈 (GJQ)

作者：羊羊小栈
时间：2025-09-06 23:44:27

本系统为原创作品，禁止二次销售！禁止将系统对应的视频、文档进行二次发布！
违者需立即停止侵权行为，并按照【羊羊小栈系统版权声明及保护条款】中规定数额进行赔偿，并承担相应法律责任。
-->

<template>
  <div class="image-detection">
    <!-- 优化的页面头部 -->
    <div class="page-header">
      <div class="header-content">
        <div class="header-left">
          <div class="title-wrapper">
            <div class="title-icon">
              <el-icon><View /></el-icon>
            </div>
            <div>
              <h1 class="page-title">光伏板缺陷识别</h1>
              <p class="page-subtitle">智能检测光伏板表面缺陷，支持多种模型分析</p>
            </div>
          </div>
        </div>
        <div class="header-right">
          <div class="header-stats">
            <div class="stat-card">
              <div class="stat-icon">
                <el-icon><Setting /></el-icon>
              </div>
              <div class="stat-content">
                <span class="stat-number">{{ publishedModels.length }}</span>
                <span class="stat-label">可用模型</span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 优化的主要内容区域 -->
    <div class="main-content">
      <!-- 检测工作区 -->
      <div class="workspace">
        <!-- 左侧配置区域 -->
        <div class="config-section">
          <el-card class="config-card" shadow="never">
            <template #header>
              <div class="card-header">
                <el-icon class="header-icon"><Setting /></el-icon>
                <span>检测配置</span>
              </div>
            </template>
            
            <div class="config-content">
              <el-form :model="detectionForm" :rules="detectionRules" ref="detectionFormRef" label-position="top" class="detection-form">
                <!-- 模型选择 -->
                <el-form-item label="选择检测模型" prop="model_id">
                  <el-select v-model="detectionForm.model_id" placeholder="请选择AI检测模型" size="large" class="model-select">
                    <el-option
                      v-for="model in publishedModels"
                      :key="model.id"
                      :label="model.name"
                      :value="model.id"
                    >
                      <span class="model-name">{{ model.name }}</span>
                    </el-option>
                  </el-select>
                </el-form-item>
                
                <!-- 置信度设置 -->
                <el-form-item label="检测置信度">
                  <div class="confidence-section">
                    <div class="confidence-display">
                      <span class="confidence-value">{{ (detectionForm.confidence_threshold * 100).toFixed(0) }}%</span>
                      <span class="confidence-desc">置信度阈值</span>
                    </div>
                    <el-slider 
                      v-model="detectionForm.confidence_threshold" 
                      :min="0.1" 
                      :max="1" 
                      :step="0.05"
                      :format-tooltip="formatTooltip"
                      class="confidence-slider"
                    />
                    <div class="confidence-tips">
                      <span>0.1</span><span>较低</span><span>适中</span><span>较高</span><span>1.0</span>
                    </div>
                  </div>
                </el-form-item>
                
                <!-- 检测按钮 -->
                <div class="action-section">
                  <el-button 
                    type="primary" 
                    @click="handleDetect" 
                    :loading="detecting"
                    :disabled="!detectionForm.model_id || selectedImages.length === 0 || validBatchCount === 0"
                    class="detect-btn"
                    size="large"
                  >
                    <el-icon class="btn-icon"><Search /></el-icon>
                    <span>{{ detecting ? `批量检测中 ${batchProcessed}/${batchQueue.length}` : `开始批量检测（${validBatchCount}）` }}</span>
                  </el-button>
                </div>
              </el-form>
            </div>
          </el-card>
        </div>
        
        <!-- 右侧上传区域 -->
        <div class="upload-section">
          <el-card class="upload-card" shadow="never">
            <template #header>
              <div class="card-header">
                <el-icon class="header-icon"><Upload /></el-icon>
                <span>图片上传</span>
              </div>
            </template>
            
            <div class="upload-content">
              <el-upload
                ref="uploadRef"
                :auto-upload="false"
                :show-file-list="false"
                :on-change="handleFileChange"
                accept="image/*"
                multiple
                drag
                class="image-upload"
              >
                <div v-if="selectedImages.length === 0" class="upload-area">
                  <div class="upload-visual">
                    <div class="upload-icon-bg">
                      <el-icon class="upload-icon"><Picture /></el-icon>
                    </div>
                    <div class="upload-content-text">
                      <h3>批量上传 RGB 图片</h3>
                      <p class="upload-desc">点击多选或一次拖入多张图片</p>
                      <div class="upload-formats">
                        <el-tag size="small">JPG</el-tag>
                        <el-tag size="small">PNG</el-tag>
                        <el-tag size="small">GIF</el-tag>
                        <el-tag size="small">BMP</el-tag>
                        <el-tag size="small">WEBP</el-tag>
                      </div>
                      <p class="upload-limit">最大支持 10MB</p>
                    </div>
                  </div>
                </div>
                <div v-else class="preview-container">
                  <div class="preview-wrapper">
                    <img :src="imagePreview" alt="预览图" class="preview-image" />
                    <el-tag class="batch-count-tag" type="success">已选择 {{ selectedImages.length }} 张 RGB</el-tag>
                    <div class="preview-mask">
                      <div class="preview-actions">
                        <el-button type="danger" size="small" @click="reselectImage" class="remove-btn">
                          <el-icon><Delete /></el-icon>
                          重新选择
                        </el-button>
                      </div>
                    </div>
                  </div>
                </div>
              </el-upload>

              <div v-if="selectedModel?.model_mode === 'multimodal'" class="secondary-upload">
                <div class="secondary-title">
                  X 模态：{{ selectedModel.secondary_modality || 'X' }}
                  <el-tag v-if="selectedModel.secondary_modality === 'edge'" size="small" type="success">可由 RGB 自动生成</el-tag>
                  <el-tag v-else size="small" type="warning">必须上传</el-tag>
                </div>
                <el-upload
                  ref="xUploadRef"
                  :auto-upload="false"
                  :show-file-list="false"
                  :on-change="handleXFileChange"
                  accept="image/*"
                  multiple
                >
                  <el-button type="primary" plain>
                    {{ selectedXImages.length ? `已选择 ${selectedXImages.length} 张 ${selectedModel.secondary_modality || 'X'}` : `批量选择 ${selectedModel.secondary_modality || 'X'} 图片` }}
                  </el-button>
                </el-upload>
                <div class="secondary-tip">
                  {{ selectedModel.secondary_modality === 'edge' ? '未上传时系统会从 RGB 生成 edge；上传时使用你的配对图。' : 'IR、depth 等真实传感器模态不会使用零填充。' }}
                </div>
              </div>

              <div v-if="batchQueue.length" class="batch-queue">
                <div class="secondary-title">批量任务（按文件名自动配对）</div>
                <el-progress
                  v-if="detecting || batchProcessed"
                  :percentage="Math.round((batchProcessed / Math.max(batchQueue.length, 1)) * 100)"
                  :status="detecting ? undefined : 'success'"
                />
                <el-table :data="batchQueue" size="small" max-height="260" border>
                  <el-table-column type="index" label="#" width="50" />
                  <el-table-column prop="rgbFile.name" label="RGB 图片" min-width="170" show-overflow-tooltip />
                  <el-table-column label="X 图片" min-width="160" show-overflow-tooltip>
                    <template #default="scope">{{ scope.row.xFile?.name || '-' }}</template>
                  </el-table-column>
                  <el-table-column label="状态" width="100">
                    <template #default="scope">
                      <el-tag :type="batchStatusType(scope.row.status)" size="small">
                        {{ batchStatusText(scope.row.status) }}
                      </el-tag>
                    </template>
                  </el-table-column>
                  <el-table-column prop="message" label="说明" min-width="190" show-overflow-tooltip />
                </el-table>
              </div>
            </div>
          </el-card>
        </div>
      </div>

      <!-- 识别结果卡片 -->
      <el-card v-if="detectionResult" class="result-card" shadow="never">
        <template #header>
          <div class="card-header">
            <el-icon class="header-icon"><View /></el-icon>
            <span>识别结果</span>
          </div>
        </template>
        
        <div class="result-content">
          <!-- 图片对比区域 -->
          <div class="result-images">
            <div class="image-container">
              <div class="image-header">
                <h4>原始图片</h4>
                <el-tag type="info" size="small">输入</el-tag>
              </div>
              <div class="image-wrapper">
                <img :src="detectionResult.original_image_url" alt="原始图片" class="result-image" />
              </div>
            </div>
            
            <div v-if="detectionResult.result_image_url" class="image-container">
              <div class="image-header">
                <h4>识别结果图片</h4>
                <div class="image-actions">
                  <el-button 
                    size="small" 
                    type="info" 
                    @click="handleEnhanceImage"
                    :loading="enhancing"
                    class="action-btn"
                  >
                    <el-icon><MagicStick /></el-icon>
                    图像增强
                  </el-button>
                  <el-button 
                    v-if="enhancedImageUrl" 
                    size="small" 
                    type="primary" 
                    @click="handleAnalyzeDefects"
                    :loading="analyzing"
                    class="action-btn"
                  >
                    <el-icon><ChatDotRound /></el-icon>
                    缺陷分析
                  </el-button>
                </div>
              </div>
              <div class="image-wrapper">
                <img :src="currentDisplayImage" alt="识别结果" class="result-image" />
                <div v-if="enhancedImageUrl" class="image-toggle">
                  <el-switch
                    v-model="showEnhanced"
                    active-text="增强图像"
                    inactive-text="原始结果"
                    class="enhance-switch"
                  />
                </div>
              </div>
            </div>
          </div>
          
          <!-- 检测信息区域 -->
          <div class="detection-info">
            <div class="info-header">
              <h4>检测信息</h4>
              <el-tag :type="detectionResult.detection_count > 0 ? 'success' : 'info'" size="large">
                {{ detectionResult.detection_count }} 个检测对象
              </el-tag>
            </div>
            
            <el-descriptions :column="2" border class="info-descriptions">
              <el-descriptions-item label="使用模型">
                <el-tag type="primary">{{ detectionResult.model_name }}</el-tag>
              </el-descriptions-item>
              <el-descriptions-item label="置信度阈值">
                <el-tag type="warning">{{ (detectionResult.confidence_threshold * 100).toFixed(0) }}%</el-tag>
              </el-descriptions-item>
            </el-descriptions>
            
            <div v-if="detectionResult.detections.length > 0" class="detections-list">
              <h5>检测详情</h5>
              <el-table :data="detectionResult.detections" border class="detections-table">
                <el-table-column prop="class_name" label="缺陷类别" width="150" align="center">
                  <template #default="scope">
                    <el-tag
                      size="small"
                      :style="{
                        backgroundColor: getClassInfo(scope.row.class_name).color,
                        color: '#FFFFFF',
                        borderColor: getClassInfo(scope.row.class_name).color,
                        fontWeight: 'bold'
                      }"
                    >
                      {{ getClassInfo(scope.row.class_name).chineseName }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column prop="confidence" label="置信度" width="120" align="center">
                  <template #default="scope">
                    <el-progress 
                      :percentage="scope.row.confidence * 100" 
                      :color="getConfidenceColor(scope.row.confidence)"
                      :show-text="false"
                      class="confidence-progress"
                    />
                    <span class="confidence-text">{{ (scope.row.confidence * 100).toFixed(1) }}%</span>
                  </template>
                </el-table-column>
                <el-table-column prop="bbox" label="位置信息" align="center">
                  <template #default="scope">
                    <el-tag type="info" size="small">
                      [{{ scope.row.bbox.map((v: number) => Math.round(v)).join(', ') }}]
                    </el-tag>
                  </template>
                </el-table-column>
              </el-table>
            </div>
          </div>
          
          <!-- 分析结果区域 -->
          <div v-if="analysisResult || suggestions" class="analysis-section">
            <el-card v-if="analysisResult" class="analysis-card" shadow="never">
              <template #header>
                <div class="card-header">
                  <div class="card-header-left">
                    <el-icon class="header-icon"><ChatDotRound /></el-icon>
                    <span>多模态大模型缺陷分析结果</span>
                    <el-tooltip content="支持 Markdown 格式" placement="top">
                      <el-icon class="info-icon"><InfoFilled /></el-icon>
                    </el-tooltip>
                  </div>
                  <div class="card-header-right">
                    <el-button 
                      v-if="!suggestions" 
                      size="small" 
                      type="primary" 
                      @click="handleGenerateSuggestions"
                      :loading="generatingSuggestions"
                      class="generate-suggestions-btn"
                    >
                      <el-icon class="btn-icon"><Star /></el-icon>
                      生成建议
                    </el-button>
                  </div>
                </div>
              </template>
              <div class="analysis-content">
                <el-scrollbar max-height="400px">
                  <div v-html="formatAnalysisText(analysisResult)" class="analysis-text markdown-content"></div>
                </el-scrollbar>
              </div>
            </el-card>
            
            <el-card v-if="suggestions" class="suggestions-card" shadow="never">
              <template #header>
                <div class="card-header">
                  <div class="card-header-left">
                    <el-icon class="header-icon"><Star /></el-icon>
                    <span>改进建议</span>
                    <el-tooltip content="支持 Markdown 格式" placement="top">
                      <el-icon class="info-icon"><InfoFilled /></el-icon>
                    </el-tooltip>
                  </div>
                  <div class="card-header-right">
                    <el-button 
                      type="primary" 
                      size="small" 
                      @click="showSaveDialogWithTitle" 
                      class="save-result-btn"
                    >
                      <el-icon><Download /></el-icon>
                      保存结果
                    </el-button>
                  </div>
                </div>
              </template>
              <div class="suggestions-content">
                <el-scrollbar max-height="400px">
                  <div v-html="formatAnalysisText(suggestions)" class="suggestions-text markdown-content"></div>
                </el-scrollbar>
              </div>
            </el-card>
          </div>
        </div>
      </el-card>
    </div>

    <!-- 保存对话框 -->
    <el-dialog v-model="showSaveDialog" title="保存识别结果" width="500px" @close="resetSaveForm" class="save-dialog">
      <el-form :model="saveForm" :rules="saveRules" ref="saveFormRef" label-width="80px">
        <el-form-item label="标题" prop="title">
          <el-input v-model="saveForm.title" placeholder="请输入标题" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input 
            v-model="saveForm.description" 
            type="textarea" 
            placeholder="请输入描述" 
            :rows="3"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <span class="dialog-footer">
          <el-button @click="showSaveDialog = false">取消</el-button>
          <el-button type="primary" @click="handleSave" :loading="saving">保存</el-button>
        </span>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, reactive, computed, watch } from 'vue'
import { ElMessage, ElForm } from 'element-plus'
import { 
  Upload, 
  InfoFilled, 
  Setting, 
  View, 
  Download, 
  Search, 
  Delete, 
  MagicStick, 
  ChatDotRound, 
  Star,
  Picture
} from '@element-plus/icons-vue'
import { detectionApi, type DetectionResponse, type PublishedModel } from '@/api/detection'
import { modelApi, type YoloClassesConfig, type YoloClassInfo } from '@/api/model'
import { pairBatchFiles, type BatchDetectionItem, type BatchDetectionStatus } from '@/utils/batchDetection'

// 响应式数据
const detecting = ref(false)
const saving = ref(false)
const showSaveDialog = ref(false)
const selectedImage = ref<File | null>(null)
const selectedXImage = ref<File | null>(null)
const selectedImages = ref<File[]>([])
const selectedXImages = ref<File[]>([])
const batchQueue = ref<BatchDetectionItem[]>([])
const batchProcessed = ref(0)
const imagePreview = ref<string>('')
const detectionResult = ref<DetectionResponse | null>(null)
const publishedModels = ref<PublishedModel[]>([])
const yoloClasses = ref<YoloClassesConfig>({})

// 图像增强和分析相关状态
const enhancing = ref(false)
const analyzing = ref(false)
const generatingSuggestions = ref(false)
const showEnhanced = ref(false)
const enhancedImageUrl = ref<string>('')
const enhancedImageData = ref<{ bucket: string; object_key: string } | null>(null)
const analysisResult = ref<string>('')
const suggestions = ref<string>('')

// 表单数据
const detectionForm = reactive({
  model_id: '',
  confidence_threshold: 0.5,
  image: null as File | null
})

const saveForm = reactive({
  title: '',
  description: ''
})

// 表单引用
const detectionFormRef = ref<InstanceType<typeof ElForm>>()
const saveFormRef = ref<InstanceType<typeof ElForm>>()
const uploadRef = ref()
const xUploadRef = ref()

// 表单验证规则
const detectionRules = {
  model_id: [
    { required: true, message: '请选择识别模型', trigger: 'change' }
  ]
}

const saveRules = {
  title: [
    { required: true, message: '请输入标题', trigger: 'blur' }
  ]
}

// 格式化滑块提示
const formatTooltip = (val: number) => `${(val * 100).toFixed(0)}%`

// 格式化文件大小
const formatFileSize = (size?: number) => {
  if (!size) return ''
  const mb = size / (1024 * 1024)
  if (mb >= 1) {
    return `${mb.toFixed(1)} MB`
  }
  const kb = size / 1024
  return `${kb.toFixed(1)} KB`
}


// 获取置信度颜色
const getConfidenceColor = (confidence: number) => {
  if (confidence >= 0.8) return '#67c23a' // 绿色
  if (confidence >= 0.6) return '#e6a23c' // 橙色
  return '#f56c6c' // 红色
}

// 获取类别信息（中文名称和颜色）
const getClassInfo = (className: string) => {
  const classInfo = yoloClasses.value[className]
  if (classInfo) {
    return {
      chineseName: classInfo.chinese_name,
      color: classInfo.color,
      description: classInfo.description
    }
  }
  // 如果没有找到配置，返回默认值
  return {
    chineseName: className,
    color: '#6b7280',
    description: ''
  }
}

// 计算当前显示的图像URL
const currentDisplayImage = computed(() => {
  if (!detectionResult.value?.result_image_url) return ''
  return showEnhanced.value && enhancedImageUrl.value 
    ? enhancedImageUrl.value 
    : detectionResult.value.result_image_url
})

const selectedModel = computed(() =>
  publishedModels.value.find(model => model.id === detectionForm.model_id)
)

const requiresUploadedX = computed(() =>
  selectedModel.value?.model_mode === 'multimodal' &&
  selectedModel.value?.secondary_modality !== 'edge'
)

const validBatchCount = computed(() => batchQueue.value.filter(item => item.status !== 'failed').length)

const rebuildBatchQueue = () => {
  batchProcessed.value = 0
  batchQueue.value = pairBatchFiles(selectedImages.value, selectedXImages.value, {
    multimodal: selectedModel.value?.model_mode === 'multimodal',
    secondaryModality: selectedModel.value?.secondary_modality
  })
}

watch(() => detectionForm.model_id, rebuildBatchQueue)

const batchStatusText = (status: BatchDetectionStatus) => ({
  pending: '等待',
  processing: '检测中',
  success: '成功',
  failed: '失败'
}[status])

const batchStatusType = (status: BatchDetectionStatus) => ({
  pending: 'info',
  processing: 'warning',
  success: 'success',
  failed: 'danger'
}[status] as 'info' | 'warning' | 'success' | 'danger')

// 获取已发布的模型
const fetchPublishedModels = async () => {
  try {
    const response = await detectionApi.getPublishedModels()
    publishedModels.value = response.models
    
    // 自动选择最新的模型（第一个模型，因为后端按创建时间倒序返回）
    if (response.models && response.models.length > 0) {
      detectionForm.model_id = response.models[0].id
    }
  } catch (error) {
    ElMessage.error('获取模型列表失败')
    console.error('Error fetching published models:', error)
  }
}

// 获取YOLO类别配置
const fetchYoloClasses = async () => {
  try {
    const response = await modelApi.getYoloClasses()
    yoloClasses.value = response.classes
  } catch (error) {
    ElMessage.error('获取类别配置失败')
    console.error('Error fetching YOLO classes:', error)
  }
}

// 处理批量文件选择
const validateImageFile = (raw: File, label = '图片') => {
  const allowedTypes = ['image/jpeg', 'image/png', 'image/gif', 'image/bmp', 'image/webp']
  if (!allowedTypes.includes(raw.type)) {
    ElMessage.error(`${label}格式不受支持（仅 JPG、PNG、GIF、BMP、WEBP）`)
    return false
  }
  const maxSize = 10 * 1024 * 1024 // 10MB
  if (raw.size > maxSize) {
    ElMessage.error(`${label}大小不能超过10MB`)
    return false
  }
  return true
}

const setRgbFiles = (files: File[]) => {
  selectedImages.value = files.filter(file => validateImageFile(file, 'RGB 图片'))
  selectedImage.value = selectedImages.value[0] || null
  detectionForm.image = selectedImage.value
  imagePreview.value = ''
  if (!selectedImage.value) {
    rebuildBatchQueue()
    return
  }
  const reader = new FileReader()
  reader.onload = (e) => {
    imagePreview.value = e.target?.result as string
  }
  reader.readAsDataURL(selectedImage.value)
  detectionResult.value = null
  resetAnalysisStates()
  rebuildBatchQueue()
}

const handleFileChange = (_file: any, fileList: any[]) => {
  setRgbFiles(fileList.map(item => item.raw).filter(Boolean))
}

const handleXFileChange = (_file: any, fileList: any[]) => {
  selectedXImages.value = fileList
    .map(item => item.raw)
    .filter((raw): raw is File => Boolean(raw) && validateImageFile(raw, 'X 模态图片'))
  selectedXImage.value = selectedXImages.value[0] || null
  detectionResult.value = null
  resetAnalysisStates()
  rebuildBatchQueue()
}

// 移除图片
const removeImage = (e: Event) => {
  e.stopPropagation()
  selectedImage.value = null
  selectedXImage.value = null
  selectedImages.value = []
  selectedXImages.value = []
  batchQueue.value = []
  batchProcessed.value = 0
  detectionForm.image = null
  imagePreview.value = ''
  detectionResult.value = null
  resetAnalysisStates()
  uploadRef.value?.clearFiles()
  xUploadRef.value?.clearFiles()
}

// 重新选择图片
const reselectImage = (e: Event) => {
  e.stopPropagation()
  
  // 创建临时的input元素来触发文件选择
  const input = document.createElement('input')
  input.type = 'file'
  input.accept = 'image/*'
  input.multiple = true
  input.style.display = 'none'
  document.body.appendChild(input)
  
  input.onchange = (event) => {
    const files = Array.from((event.target as HTMLInputElement)?.files || [])
    if (files.length) setRgbFiles(files)
    document.body.removeChild(input)
  }
  
  input.click()
}

// 执行识别
const handleDetect = async () => {
  if (!detectionFormRef.value) return
  
  const valid = await detectionFormRef.value.validate().catch(() => false)
  if (!valid) return
  
  if (!selectedImages.value.length) {
    ElMessage.error('请至少选择一张 RGB 图片')
    return
  }
  rebuildBatchQueue()
  if (!validBatchCount.value) {
    ElMessage.error(`没有可执行任务，请补齐同名 ${selectedModel.value?.secondary_modality || 'X'} 图片`)
    return
  }
  
  try {
    detecting.value = true
    
    let succeeded = 0
    for (const item of batchQueue.value) {
      if (item.status === 'failed') {
        batchProcessed.value += 1
        continue
      }
      item.status = 'processing'
      item.message = '正在上传并检测'
      try {
        const formData = new FormData()
        formData.append('image', item.rgbFile)
        formData.append('model_id', detectionForm.model_id)
        formData.append('confidence_threshold', detectionForm.confidence_threshold.toString())
        if (item.xFile) formData.append('x_image', item.xFile)
        formData.append('request_key', crypto.randomUUID())
        const response = await detectionApi.detectImage(formData)
        detectionResult.value = response
        item.recordId = response.record_id
        item.status = 'success'
        item.message = `记录 ${response.record_id} 已自动保存`
        succeeded += 1
      } catch (error: any) {
        item.status = 'failed'
        item.message = error.response?.data?.message || error.response?.data?.msg || '识别失败'
      } finally {
        batchProcessed.value += 1
      }
    }
    resetAnalysisStates()
    const failed = batchQueue.value.length - succeeded
    if (succeeded) ElMessage.success(`批量检测完成：成功 ${succeeded} 张，失败 ${failed} 张`)
    else ElMessage.error('批量检测未成功，请查看任务说明')
  } finally {
    detecting.value = false
  }
}

// 保存识别结果
const handleSave = async () => {
  if (!saveFormRef.value) return
  
  const valid = await saveFormRef.value.validate().catch(() => false)
  if (!valid) return
  
  if (!detectionResult.value) {
    ElMessage.error('没有可保存的识别结果')
    return
  }
  
  try {
    saving.value = true
    
    // 构建完整的保存数据，包含新增的字段
    const enhancedSaveData = {
      ...detectionResult.value.save_data,
      enhanced_image_bucket: enhancedImageData.value?.bucket || undefined,
      enhanced_image_object_key: enhancedImageData.value?.object_key || undefined,
      analysis_result: analysisResult.value || undefined,
      suggestions: suggestions.value || undefined
    }
    
    await detectionApi.saveDetection({
      title: saveForm.title,
      description: saveForm.description,
      save_data: enhancedSaveData
    })
    
    ElMessage.success('识别结果保存成功')
    showSaveDialog.value = false
    resetSaveForm()
    
    // 保存成功后重置页面内容
    setTimeout(() => {
      resetPageContent()
    }, 1000) // 延迟1秒让用户看到成功消息
  } catch (error: any) {
    ElMessage.error(error.response?.data?.message || '保存失败')
  } finally {
    saving.value = false
  }
}

// 生成默认标题（不超过8个字）
const generateDefaultTitle = () => {
  if (!detectionResult.value) return ''
  
  // 生成4位随机数
  const randomNum = Math.floor(Math.random() * 9000) + 1000
  
  return `光伏板检测_${randomNum}`
}

// 生成默认描述（不超过30个字，包含检测类别）
const generateDefaultDescription = () => {
  if (!detectionResult.value || !detectionResult.value.detections.length) {
    return '未检测到缺陷，光伏板状态正常'
  }
  
  // 获取所有检测到的类别
  const detectedClasses = [...new Set(detectionResult.value.detections.map(d => d.class_name))]
  const chineseClasses = detectedClasses.map(className => getClassInfo(className).chineseName)
  
  const detectionCount = detectionResult.value.detection_count
  const modelName = detectionResult.value.model_name
  
  // 构建描述，确保不超过30个字
  let description = `检测到${detectionCount}个缺陷，包含：${chineseClasses.join('、')}`
  
  // 如果描述太长，截断类别列表
  if (description.length > 30) {
    const maxClasses = Math.floor((30 - 10) / 2) // 预留10个字符给其他内容
    const truncatedClasses = chineseClasses.slice(0, maxClasses)
    description = `检测到${detectionCount}个缺陷，包含：${truncatedClasses.join('、')}`
    
    if (chineseClasses.length > maxClasses) {
      description += '等'
    }
  }
  
  return description
}

// 显示保存对话框并自动填充标题和描述
const showSaveDialogWithTitle = () => {
  // 生成并设置默认标题和描述
  saveForm.title = generateDefaultTitle()
  saveForm.description = generateDefaultDescription()
  
  // 清除之前的验证错误
  saveFormRef.value?.clearValidate()
  
  // 显示对话框
  showSaveDialog.value = true
}

// 重置保存表单
const resetSaveForm = () => {
  saveForm.title = ''
  saveForm.description = ''
  saveFormRef.value?.clearValidate()
}

// 重置页面内容
const resetPageContent = () => {
  // 重置图片相关状态
  selectedImage.value = null
  selectedXImage.value = null
  selectedImages.value = []
  selectedXImages.value = []
  batchQueue.value = []
  batchProcessed.value = 0
  imagePreview.value = ''
  detectionForm.image = null
  
  // 重置检测结果
  detectionResult.value = null
  
  // 重置分析相关状态
  resetAnalysisStates()
  
  // 重置上传组件
  uploadRef.value?.clearFiles()
  xUploadRef.value?.clearFiles()
  
  // 重置表单验证
  detectionFormRef.value?.clearValidate()
}

// 重置分析相关状态
const resetAnalysisStates = () => {
  showEnhanced.value = false
  enhancedImageUrl.value = ''
  enhancedImageData.value = null
  analysisResult.value = ''
  suggestions.value = ''
}

// 图像增强处理
const handleEnhanceImage = async () => {
  if (!detectionResult.value?.enhancement_data?.result_image_bucket || 
      !detectionResult.value?.enhancement_data?.result_image_object_key) {
    ElMessage.error('缺少图像数据')
    return
  }
  
  try {
    enhancing.value = true
    
    const response = await detectionApi.enhanceImage({
      bucket: detectionResult.value.enhancement_data.result_image_bucket,
      object_key: detectionResult.value.enhancement_data.result_image_object_key
    })
    
    enhancedImageUrl.value = response.enhanced_image_url
    enhancedImageData.value = {
      bucket: response.enhanced_image_bucket,
      object_key: response.enhanced_image_object_key
    }
    
    // 自动切换到增强图像显示
    showEnhanced.value = true
    
    ElMessage.success('图像增强完成')
  } catch (error: any) {
    ElMessage.error(error.response?.data?.message || '图像增强失败')
  } finally {
    enhancing.value = false
  }
}

// 缺陷分析处理
const handleAnalyzeDefects = async () => {
  if (!enhancedImageData.value || !detectionResult.value?.enhancement_data?.detections) {
    ElMessage.error('缺少增强图像数据或检测结果')
    return
  }
  
  try {
    analyzing.value = true
    
    const response = await detectionApi.analyzeDefects({
      bucket: enhancedImageData.value.bucket,
      object_key: enhancedImageData.value.object_key,
      detections: detectionResult.value.enhancement_data.detections
    })
    
    analysisResult.value = response.analysis_result
    
    ElMessage.success('缺陷分析完成')
  } catch (error: any) {
    ElMessage.error(error.response?.data?.message || '缺陷分析失败')
  } finally {
    analyzing.value = false
  }
}

// 生成建议处理
const handleGenerateSuggestions = async () => {
  if (!analysisResult.value) {
    ElMessage.error('请先进行缺陷分析')
    return
  }
  
  try {
    generatingSuggestions.value = true
    
    const response = await detectionApi.generateSuggestions({
      analysis_result: analysisResult.value
    })
    
    suggestions.value = response.suggestions
    
    ElMessage.success('改进建议生成完成')
  } catch (error: any) {
    ElMessage.error(error.response?.data?.message || '生成建议失败')
  } finally {
    generatingSuggestions.value = false
  }
}

// 导入 marked 库用于 Markdown 渲染
import { marked } from 'marked'

// 配置 marked 选项
marked.setOptions({
  breaks: true, // 支持换行
  gfm: true    // 支持 GitHub 风格的 Markdown
})

// 格式化分析文本（支持完整的 Markdown 格式）
const formatAnalysisText = (text: string) => {
  if (!text) return ''
  
  try {
    // 使用 marked 渲染 Markdown
    return marked(text)
  } catch (error) {
    console.error('Markdown 渲染错误:', error)
    // 如果渲染失败，回退到基本格式处理
    return text
      .replace(/\n/g, '<br>')
      .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
      .replace(/\*(.*?)\*/g, '<em>$1</em>')
      .replace(/#{3,}\s*(.*?)$/gm, '<h4>$1</h4>')
      .replace(/#{2}\s*(.*?)$/gm, '<h3>$1</h3>')
      .replace(/#{1}\s*(.*?)$/gm, '<h2>$1</h2>')
  }
}

// 初始化
onMounted(() => {
  fetchPublishedModels()
  fetchYoloClasses()
})
</script>

<style scoped>
.batch-queue {
  width: 100%;
  margin-top: 18px;
}

.batch-queue .el-progress {
  margin: 10px 0;
}

.batch-count-tag {
  position: absolute;
  top: 12px;
  left: 12px;
  z-index: 2;
}

.secondary-upload {
  margin-top: 16px;
  padding: 14px;
  border: 1px solid #dcdfe6;
  border-radius: 8px;
  background: #f8fafc;
}

.secondary-title {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
  font-weight: 600;
}

.secondary-tip {
  margin-top: 8px;
  color: #606266;
  font-size: 12px;
  line-height: 1.5;
}

/* 主容器样式 */
.image-detection {
  min-height: 100vh;
  background: #ffffff;
  padding: 0;
}

/* 页面头部样式 */
.page-header {
  background: #ffffff;
  padding: 24px 0;
  margin-bottom: 24px;
  border-bottom: 1px solid #ebeef5;
  position: relative;
}

.header-content {
  display: flex;
  justify-content: space-between;
  align-items: center;
  max-width: 1200px;
  margin: 0 auto;
  padding: 0 24px;
  position: relative;
}

.header-left {
  flex: 1;
}

.title-wrapper {
  display: flex;
  align-items: center;
  gap: 20px;
}

.title-icon {
  width: 48px;
  height: 48px;
  background: #f5f7fa;
  border-radius: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.title-icon .el-icon {
  font-size: 20px;
  color: #409eff;
}

.page-title {
  margin: 0 0 8px 0;
  font-size: 24px;
  color: #303133;
  font-weight: 500;
  letter-spacing: -0.3px;
}

.page-subtitle {
  font-size: 14px;
  color: #909399;
  margin: 0;
  line-height: 1.5;
  font-weight: 400;
}

.header-right {
  display: flex;
  align-items: center;
}

.stat-card {
  display: flex;
  align-items: center;
  gap: 12px;
  background: #ffffff;
  border: 1px solid #ebeef5;
  border-radius: 4px;
  padding: 16px 20px;
  min-width: 120px;
}

.stat-icon {
  width: 36px;
  height: 36px;
  background: #f5f7fa;
  border-radius: 4px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.stat-icon .el-icon {
  font-size: 16px;
  color: #409eff;
}

.stat-content {
  display: flex;
  flex-direction: column;
}

.stat-number {
  font-size: 16px;
  font-weight: 500;
  color: #303133;
  line-height: 1;
}

.stat-label {
  font-size: 12px;
  color: #909399;
  margin-top: 2px;
}

/* 主内容区样式 */
.main-content {
  max-width: 1200px;
  margin: 0 auto;
  padding: 0 24px 32px;
}

/* 工作区布局 */
.workspace {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 24px;
  align-items: start;
}

/* 配置区域样式 */
.config-section {
  height: fit-content;
}

.config-card {
  border-radius: 4px;
  border: 1px solid #ebeef5;
  background: #ffffff;
  overflow: hidden;
}

.config-card :deep(.el-card__header) {
  background: #fafafa;
  border-bottom: 1px solid #ebeef5;
  padding: 18px 20px;
}

.config-content {
  padding: 24px;
}

/* 上传区域样式 */
.upload-section {
  height: fit-content;
}

.upload-card {
  border-radius: 4px;
  border: 1px solid #ebeef5;
  background: #ffffff;
  overflow: hidden;
}

.upload-card :deep(.el-card__header) {
  background: #fafafa;
  border-bottom: 1px solid #ebeef5;
  padding: 18px 20px;
}

.upload-content {
  padding: 24px;
}

/* 卡片头部样式 */
.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 16px;
  font-weight: 500;
  color: #303133;
}

.card-header-left {
  display: flex;
  align-items: center;
  gap: 8px;
}

.card-header-right {
  display: flex;
  align-items: center;
  gap: 8px;
}

.header-icon {
  font-size: 16px;
  color: #409eff;
}

.generate-suggestions-btn {
  background: #409eff;
  border-color: #409eff;
  color: #ffffff;
}

.generate-suggestions-btn:hover {
  background: #66b1ff;
  border-color: #66b1ff;
}

.generate-suggestions-btn .el-icon {
  color: #ffffff !important;
}

.save-result-btn {
  background: #67c23a;
  border-color: #67c23a;
  color: #ffffff;
}

.save-result-btn:hover {
  background: #85ce61;
  border-color: #85ce61;
}

.save-result-btn .el-icon {
  color: #ffffff !important;
}

/* 表单样式 */
.detection-form {
  display: flex;
  flex-direction: column;
  gap: 28px;
}

.detection-form :deep(.el-form-item__label) {
  font-weight: 500;
  color: #606266;
  font-size: 14px;
  line-height: 1.5;
}

/* 模型选择器样式 */
.model-select {
  width: 100%;
}

.model-select :deep(.el-input__wrapper) {
  border-radius: 4px;
  border: 1px solid #dcdfe6;
  transition: all 0.2s ease;
}

.model-select :deep(.el-input__wrapper:hover) {
  border-color: #c0c4cc;
}

.model-select :deep(.el-input__wrapper.is-focus) {
  border-color: #409eff;
  box-shadow: 0 0 0 2px rgba(64, 158, 255, 0.2);
}

.model-name {
  font-weight: 500;
  color: #303133;
  font-size: 14px;
  line-height: 1.4;
}

/* 置信度设置样式 */
.confidence-section {
  background: #f5f7fa;
  border-radius: 4px;
  padding: 20px;
  border: 1px solid #ebeef5;
}

.confidence-display {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 20px;
}

.confidence-value {
  font-size: 20px;
  font-weight: 500;
  color: #409eff;
}

.confidence-desc {
  font-size: 14px;
  color: #909399;
}

.confidence-slider {
  margin: 16px 0;
}

.confidence-slider :deep(.el-slider__runway) {
  height: 6px;
  background: #e4e7ed;
  border-radius: 3px;
}

.confidence-slider :deep(.el-slider__bar) {
  background: #409eff;
  border-radius: 3px;
}

.confidence-slider :deep(.el-slider__button) {
  border: 2px solid #409eff;
  background: #ffffff;
  width: 18px;
  height: 18px;
}

.confidence-tips {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
  color: #909399;
  margin-top: 8px;
}

.confidence-tips span:first-child,
.confidence-tips span:last-child {
  color: #606266;
  font-weight: 500;
}

/* 图片上传样式 */
.image-upload :deep(.el-upload) {
  width: 100%;
}

.image-upload :deep(.el-upload-dragger) {
  width: 100%;
  height: 280px;
  border: 2px dashed #d9d9d9;
  border-radius: 6px;
  background: #fafafa;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all 0.3s ease;
  position: relative;
}

.image-upload :deep(.el-upload-dragger:hover) {
  border-color: #409eff;
  background: #f5f7fa;
}

.upload-area {
  text-align: center;
  width: 100%;
  padding: 40px;
}

.upload-visual {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 20px;
}

.upload-icon-bg {
  width: 64px;
  height: 64px;
  background: #f5f7fa;
  border-radius: 6px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.upload-icon {
  font-size: 28px;
  color: #c0c4cc;
}

.upload-content-text h3 {
  margin: 0 0 8px 0;
  font-size: 18px;
  font-weight: 500;
  color: #303133;
}

.upload-desc {
  font-size: 14px;
  color: #909399;
  margin: 0 0 16px 0;
  line-height: 1.5;
}

.upload-formats {
  display: flex;
  gap: 8px;
  justify-content: center;
  flex-wrap: wrap;
  margin-bottom: 12px;
}

.upload-formats .el-tag {
  background: #f0f9ff;
  color: #409eff;
  border: 1px solid #b3d8ff;
  border-radius: 4px;
  font-weight: 500;
  font-size: 11px;
}

.upload-limit {
  font-size: 12px;
  color: #c0c4cc;
  margin: 0;
}

/* 图片预览样式 */
.preview-container {
  width: 100%;
  text-align: center;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  min-height: 280px;
}

.preview-wrapper {
  position: relative;
  display: inline-block;
  border-radius: 8px;
  overflow: hidden;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1);
  max-width: 100%;
}

.preview-image {
  max-width: 100%;
  max-height: 280px;
  object-fit: contain;
  display: block;
}

.preview-mask {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background: rgba(0, 0, 0, 0.7);
  display: flex;
  align-items: center;
  justify-content: center;
  opacity: 0;
  transition: all 0.3s ease;
}

.preview-wrapper:hover .preview-mask {
  opacity: 1;
}

.preview-actions {
  display: flex;
  gap: 12px;
}

.remove-btn {
  background: #ef4444;
  border: none;
  color: #ffffff;
  font-weight: 500;
  border-radius: 6px;
  padding: 8px 12px;
  font-size: 13px;
}

.remove-btn:hover {
  background: #dc2626;
  transform: translateY(-1px);
}


/* 操作按钮区域 */
.action-section {
  margin-top: 8px;
}

.detect-btn {
  width: 100%;
  height: 40px;
  font-size: 14px;
  font-weight: 500;
  background: #409eff;
  border-color: #409eff;
  border-radius: 4px;
  transition: all 0.2s ease;
  color: #ffffff;
}

.detect-btn:hover {
  background: #66b1ff;
  border-color: #66b1ff;
}

.detect-btn:disabled {
  background: #c0c4cc;
  border-color: #c0c4cc;
  cursor: not-allowed;
  opacity: 0.6;
}

.btn-icon {
  font-size: 18px;
  margin-right: 8px;
}

/* 结果卡片样式 */
.result-card {
  grid-column: 1 / -1;
  border-radius: 4px;
  border: 1px solid #ebeef5;
  background: #ffffff;
  margin-top: 24px;
  overflow: hidden;
}

.result-card :deep(.el-card__header) {
  background: #fafafa;
  border-bottom: 1px solid #ebeef5;
  padding: 18px 20px;
}

.result-content {
  margin-top: 20px;
}

.result-images {
  display: flex;
  gap: 20px;
  margin-bottom: 30px;
  flex-wrap: wrap;
}

.image-container {
  flex: 1;
  min-width: 300px;
  background-color: #ffffff;
  border-radius: 4px;
  border: 1px solid #ebeef5;
  overflow: hidden;
}

.image-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 16px 20px;
  border-bottom: 1px solid #ebeef5;
  background-color: #fafafa;
}

.image-header h4 {
  margin: 0;
  color: #303133;
  font-size: 14px;
  font-weight: 500;
}

.image-wrapper {
  padding: 20px;
  text-align: center;
}

.result-image {
  width: 100%;
  max-width: 400px;
  height: auto;
  border: 1px solid #eee;
  border-radius: 6px;
}

.image-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.action-btn {
  font-size: 14px;
}

.image-toggle {
  margin-top: 10px;
  display: flex;
  justify-content: center;
}

.enhance-switch .el-switch__core {
  background-color: #409eff;
  border-color: #409eff;
}

.enhance-switch .el-switch__core:after {
  background-color: #fff;
}

.enhance-switch .el-switch__core:hover:after {
  background-color: #fff;
}

.detection-info {
  margin-top: 30px;
  background-color: #ffffff;
  border-radius: 4px;
  border: 1px solid #ebeef5;
  padding: 20px;
}


.info-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 20px;
  padding-bottom: 15px;
  border-bottom: 1px solid #ebeef5;
}

.info-header h4 {
  margin: 0;
  color: #303133;
  font-size: 16px;
  font-weight: 500;
}

.info-descriptions .el-descriptions__header {
  margin-bottom: 15px;
}

.info-descriptions .el-descriptions__header .el-descriptions__title {
  font-size: 16px;
  color: #303133;
}

.detections-list {
  margin-top: 20px;
}

.detections-list h5 {
  margin: 0 0 15px 0;
  color: #303133;
  font-size: 14px;
}

.detections-table {
  width: 100%;
}

.detections-table th {
  background-color: #fafafa;
  font-weight: 500;
  color: #909399;
}

.detections-table tr:nth-child(even) {
  background: #fafafa;
}

.confidence-progress {
  margin-top: 5px;
  margin-bottom: 5px;
}

.confidence-text {
  font-size: 12px;
  color: #909399;
  margin-left: 5px;
}

.analysis-section {
  margin-top: 20px;
}

.analysis-card, .suggestions-card {
  margin-bottom: 20px;
}

.analysis-content, .suggestions-content {
  padding: 20px 0;
}

.analysis-text, .suggestions-text {
  line-height: 1.8;
  color: #333;
}

.analysis-text h2, .suggestions-text h2,
.analysis-text h3, .suggestions-text h3,
.analysis-text h4, .suggestions-text h4 {
  color: #409eff;
  margin: 16px 0 8px 0;
}

.analysis-text strong, .suggestions-text strong {
  color: #e6a23c;
  font-weight: 600;
}

.analysis-text em, .suggestions-text em {
  color: #67c23a;
  font-style: normal;
  background: #f0f9ff;
  padding: 2px 4px;
  border-radius: 3px;
}

/* Markdown 内容样式 */
.markdown-content {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
}

.markdown-content h1 {
  font-size: 24px;
  color: #303133;
  margin: 24px 0 16px 0;
  padding-bottom: 8px;
  border-bottom: 2px solid #409eff;
}

.markdown-content h2 {
  font-size: 20px;
  color: #409eff;
  margin: 20px 0 12px 0;
  padding-bottom: 6px;
  border-bottom: 1px solid #e4e7ed;
}

.markdown-content h3 {
  font-size: 18px;
  color: #409eff;
  margin: 16px 0 10px 0;
}

.markdown-content h4 {
  font-size: 16px;
  color: #409eff;
  margin: 14px 0 8px 0;
}

.markdown-content p {
  margin: 12px 0;
  line-height: 1.8;
}

.markdown-content ul, .markdown-content ol {
  margin: 12px 0;
  padding-left: 24px;
}

.markdown-content li {
  margin: 6px 0;
  line-height: 1.6;
}

.markdown-content blockquote {
  margin: 16px 0;
  padding: 12px 16px;
  background: #f8f9fa;
  border-left: 4px solid #409eff;
  color: #606266;
  font-style: italic;
}

.markdown-content code {
  background: #f4f4f5;
  padding: 2px 6px;
  border-radius: 3px;
  font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
  font-size: 14px;
  color: #e6a23c;
}

.markdown-content pre {
  background: #f8f9fa;
  padding: 16px;
  border-radius: 6px;
  overflow-x: auto;
  border: 1px solid #e4e7ed;
  margin: 16px 0;
}

.markdown-content pre code {
  background: none;
  padding: 0;
  color: #303133;
}

.markdown-content table {
  width: 100%;
  border-collapse: collapse;
  margin: 16px 0;
}

.markdown-content th, .markdown-content td {
  border: 1px solid #e4e7ed;
  padding: 8px 12px;
  text-align: left;
}

.markdown-content th {
  background: #f8f9fa;
  font-weight: 600;
  color: #409eff;
}

.markdown-content tr:nth-child(even) {
  background: #fafafa;
}

/* 卡片头部样式 */
.card-header {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 18px;
  font-weight: bold;
  color: #303133;
}

.card-header .el-icon {
  color: #409eff;
}

.card-header .el-icon:hover {
  color: #66b1ff;
}

.info-icon {
  color: #909399;
  font-size: 16px;
  cursor: help;
}

.info-icon:hover {
  color: #409eff;
}

.save-btn {
  background-color: #67c23a;
  border-color: #67c23a;
}

.save-btn:hover {
  background-color: #85ce61;
  border-color: #85ce61;
}

.save-dialog .el-dialog__header {
  background-color: #f5f7fa;
  border-bottom: 1px solid #ebeef5;
}

.save-dialog .el-dialog__title {
  color: #303133;
  font-weight: bold;
}

.save-dialog .el-dialog__body {
  padding: 20px;
}

.save-dialog .el-dialog__footer {
  background-color: #f5f7fa;
  border-top: 1px solid #ebeef5;
  padding: 10px 20px;
}

/* 模型选择器下拉选项样式 */
.model-select :deep(.el-select-dropdown__item) {
  padding: 12px 16px;
  line-height: 1.4;
}

.model-select :deep(.el-select-dropdown__item:hover) {
  background-color: #f5f7fa;
}

.model-select :deep(.el-select-dropdown__item.is-selected) {
  background-color: #ecf5ff;
  color: #409eff;
}

/* 表单行样式 */
.form-row {
  display: flex;
  gap: 20px;
  margin-bottom: 20px;
}

.form-item-full {
  flex: 1;
}

.form-item-half {
  flex: 0 0 calc(50% - 10px);
}

/* 模型选择器样式 */
.model-select {
  width: 100%;
}

/* 置信度滑块样式 */
.confidence-slider {
  width: 100%;
}

.custom-slider .el-slider__runway {
  height: 8px;
  background-color: #e4e7ed;
  border-radius: 4px;
}

.custom-slider .el-slider__bar {
  background-color: #409eff;
  border-radius: 4px;
}

.custom-slider .el-slider__button {
  border-color: #409eff;
  background-color: #409eff;
}

/* 置信度标签样式 */
.confidence-labels {
  display: flex;
  justify-content: space-between;
  width: 100%;
  margin-top: 5px;
  font-size: 12px;
  color: #909399;
}

.confidence-label {
  flex: 1;
  text-align: center;
}

/* 图片上传样式 */
.image-upload .el-upload-dragger {
  width: 100%;
  height: 200px;
  border: 2px dashed #dcdfe6;
  border-radius: 8px;
  background-color: #f5f7fa;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  transition: border-color 0.3s ease;
}

.image-upload .el-upload-dragger:hover {
  border-color: #409eff;
}

/* 上传图标包装器 */
.upload-icon-wrapper {
  margin-bottom: 20px;
}

.upload-icon {
  font-size: 48px;
  color: #909399;
}


/* 预览覆盖层样式 */
.preview-overlay {
  position: absolute;
  top: 10px;
  right: 10px;
}

/* 移除按钮样式 */
.remove-btn {
  background-color: #f56c6c;
  border-color: #f56c6c;
  color: #fff;
}

.remove-btn:hover {
  background-color: #f78989;
  border-color: #f78989;
}

/* 检测按钮样式 */
.detect-btn {
  width: 100%;
  height: 50px;
  font-size: 18px;
  font-weight: bold;
  background: linear-gradient(135deg, #409eff 0%, #66b1ff 100%);
  border: none;
  border-radius: 8px;
  transition: all 0.3s ease;
}

.detect-btn:hover {
  transform: translateY(-2px);
  box-shadow: 0 4px 12px rgba(64, 158, 255, 0.4);
}

.detect-btn:disabled {
  transform: none;
  box-shadow: none;
  opacity: 0.6;
}

/* 响应式设计 */
@media (max-width: 1024px) {
  .workspace {
    grid-template-columns: 1fr;
    gap: 20px;
  }
  
  .main-content {
    padding: 0 20px 32px;
  }
  
  .header-content {
    padding: 0 20px;
  }
  
  .title-wrapper {
    gap: 16px;
  }
  
  .title-icon {
    width: 50px;
    height: 50px;
  }
  
  .title-icon .el-icon {
    font-size: 24px;
  }
  
  .page-title {
    font-size: 28px;
  }
  
  .config-content,
  .upload-content {
    padding: 20px;
  }
}

@media (max-width: 768px) {
  .main-content {
    padding: 0 16px 32px;
  }
  
  .header-content {
    padding: 0 16px;
    flex-direction: column;
    gap: 20px;
    text-align: center;
  }
  
  .title-wrapper {
    justify-content: center;
  }
  
  .page-header {
    padding: 32px 0;
  }
  
  .upload-area {
    padding: 24px;
  }
  
  .image-upload :deep(.el-upload-dragger) {
    height: 240px;
  }
  
  .upload-icon-bg {
    width: 60px;
    height: 60px;
  }
  
  .upload-icon {
    font-size: 28px;
  }
  
  .config-content,
  .upload-content {
    padding: 16px;
  }
  
  .confidence-section {
    padding: 16px;
  }
  
  .confidence-value {
    font-size: 24px;
  }
}

.dialog-footer button:first-child {
  margin-right: 10px;
}

/* 新增样式 */
.result-image-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 15px;
}

.image-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.image-toggle {
  margin-top: 10px;
  display: flex;
  justify-content: center;
}

.analysis-section {
  margin-top: 20px;
}

.analysis-card, .suggestions-card {
  margin-bottom: 20px;
}

.analysis-content, .suggestions-content {
  padding: 20px 0;
}

.analysis-text, .suggestions-text {
  line-height: 1.8;
  color: #333;
}

.analysis-text h2, .suggestions-text h2,
.analysis-text h3, .suggestions-text h3,
.analysis-text h4, .suggestions-text h4 {
  color: #409eff;
  margin: 16px 0 8px 0;
}

.analysis-text strong, .suggestions-text strong {
  color: #e6a23c;
  font-weight: 600;
}

.analysis-text em, .suggestions-text em {
  color: #67c23a;
  font-style: normal;
  background: #f0f9ff;
  padding: 2px 4px;
  border-radius: 3px;
}

/* Markdown 内容样式 */
.markdown-content {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
}

.markdown-content h1 {
  font-size: 24px;
  color: #303133;
  margin: 24px 0 16px 0;
  padding-bottom: 8px;
  border-bottom: 2px solid #409eff;
}

.markdown-content h2 {
  font-size: 20px;
  color: #409eff;
  margin: 20px 0 12px 0;
  padding-bottom: 6px;
  border-bottom: 1px solid #e4e7ed;
}

.markdown-content h3 {
  font-size: 18px;
  color: #409eff;
  margin: 16px 0 10px 0;
}

.markdown-content h4 {
  font-size: 16px;
  color: #409eff;
  margin: 14px 0 8px 0;
}

.markdown-content p {
  margin: 12px 0;
  line-height: 1.8;
}

.markdown-content ul, .markdown-content ol {
  margin: 12px 0;
  padding-left: 24px;
}

.markdown-content li {
  margin: 6px 0;
  line-height: 1.6;
}

.markdown-content blockquote {
  margin: 16px 0;
  padding: 12px 16px;
  background: #f8f9fa;
  border-left: 4px solid #409eff;
  color: #606266;
  font-style: italic;
}

.markdown-content code {
  background: #f4f4f5;
  padding: 2px 6px;
  border-radius: 3px;
  font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
  font-size: 14px;
  color: #e6a23c;
}

.markdown-content pre {
  background: #f8f9fa;
  padding: 16px;
  border-radius: 6px;
  overflow-x: auto;
  border: 1px solid #e4e7ed;
  margin: 16px 0;
}

.markdown-content pre code {
  background: none;
  padding: 0;
  color: #303133;
}

.markdown-content table {
  width: 100%;
  border-collapse: collapse;
  margin: 16px 0;
}

.markdown-content th, .markdown-content td {
  border: 1px solid #e4e7ed;
  padding: 8px 12px;
  text-align: left;
}

.markdown-content th {
  background: #f8f9fa;
  font-weight: 600;
  color: #409eff;
}

.markdown-content tr:nth-child(even) {
  background: #fafafa;
}

/* 卡片头部样式 */
.card-header-with-actions {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.card-header-with-actions h4 {
  margin: 0;
}

.info-icon {
  color: #909399;
  font-size: 16px;
  cursor: help;
}

.info-icon:hover {
  color: #409eff;
}

@media (max-width: 768px) {
  .result-images {
    flex-direction: column;
  }
  
  .image-container {
    min-width: unset;
  }
  
  .result-image-header {
    flex-direction: column;
    align-items: flex-start;
    gap: 10px;
  }
  
  .image-actions {
    width: 100%;
    justify-content: flex-start;
  }
}
</style>
