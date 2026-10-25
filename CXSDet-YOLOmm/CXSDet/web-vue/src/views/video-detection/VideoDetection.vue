<!-- MD5: c4a5af1eb5afa9fe7a7cac7efb3be719 -->
<!--
版权所有 ©  2025 羊羊小栈 （GJQ）

作者：羊羊小栈
时间：2025-09-06 23:44:27

本系统为原创作品，禁止二次销售！禁止将系统对应的视频、文档进行二次发布！
违者需立即停止侵权行为，并按照【羊羊小栈系统版权声明及保护条款】中规定数额进行赔偿，并承担相应法律责任。
-->

<template>
  <div class="video-detection">
    <!-- 页面头部 -->
    <div class="page-header">
      <div class="header-content">
        <div class="header-left">
          <h1 class="page-title">视频巡检</h1>
          <p class="page-subtitle">上传海上风电场巡检视频，使用 AI 模型进行缺陷检测分析</p>
        </div>
        <div class="header-right">
          <div class="header-stats">
            <div class="stat-item">
              <span class="stat-number">{{ publishedModels.length }}</span>
              <span class="stat-label">可用模型</span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 主要内容区域 -->
    <div class="main-content">
      <!-- 检测配置卡片 -->
      <el-card class="config-card" shadow="never">
        <template #header>
          <div class="card-header">
            <el-icon class="header-icon"><Setting /></el-icon>
            <span>检测配置</span>
          </div>
        </template>
        
        <el-form :model="detectionForm" :rules="detectionRules" ref="detectionFormRef" label-width="140px" class="detection-form">
          <div class="form-row">
            <el-form-item label="任务标题" prop="title" class="form-item-full">
              <el-input v-model="detectionForm.title" placeholder="请输入任务标题" class="title-input" />
            </el-form-item>
          </div>
          
          <div class="form-row">
            <el-form-item label="任务描述" class="form-item-full">
              <el-input 
                v-model="detectionForm.description" 
                type="textarea" 
                placeholder="请输入任务描述" 
                :rows="3"
                class="description-input"
              />
            </el-form-item>
          </div>
          
          <div class="form-row">
            <el-form-item label="选择模型" prop="model_id" class="form-item-full">
              <el-select v-model="detectionForm.model_id" placeholder="请选择已发布的模型" class="model-select">
                <el-option
                  v-for="model in publishedModels"
                  :key="model.id"
                  :label="model.name"
                  :value="model.id"
                >
                  <div class="model-option">
                    <span class="model-name">{{ model.name }}</span>
                    <span v-if="model.description" class="model-desc">{{ model.description }}</span>
                  </div>
                </el-option>
              </el-select>
            </el-form-item>
          </div>
          
          <div class="form-row form-row-double">
            <el-form-item label="帧率设置" prop="frame_rate" class="form-item-half">
              <div class="frame-rate-input">
                <el-input-number 
                  v-model="detectionForm.frame_rate" 
                  :min="1" 
                  :max="videoFrameRate || 60" 
                  :step="1" 
                  controls-position="right"
                  class="frame-input"
                />
                <div class="frame-info">
                  <span class="frame-unit">fps（每秒处理帧数）</span>
                  <span v-if="videoFrameRate" class="frame-hint">
                    原视频: {{ videoFrameRate }}fps
                  </span>
                </div>
              </div>
              
              <!-- 帧率提示 -->
              <el-alert
                v-if="shouldShowFrameRateAlert()"
                title="帧率提示"
                type="info"
                show-icon
                :closable="false"
                class="frame-rate-alert"
              >
                <template #default>
                  原视频帧率为 <strong>{{ videoFrameRate }}fps</strong>，低于设置的 <strong>{{ detectionForm.frame_rate }}fps</strong>。
                  <br>
                  实际将按 <strong>{{ getActualFrameRate() }}fps</strong> 处理，这是最优的处理效率。
                </template>
              </el-alert>
              
              <!-- 处理说明 -->
              <div v-if="videoFrameRate" class="frame-explanation">
                <el-icon class="info-icon"><InfoFilled /></el-icon>
                <span class="explanation-text">
                  <template v-if="detectionForm.frame_rate <= videoFrameRate">
                    将每隔 {{ Math.round(videoFrameRate / detectionForm.frame_rate) }} 帧提取1帧进行AI检测
                  </template>
                  <template v-else>
                    将提取视频的所有 {{ videoFrameRate }} 帧进行AI检测
                  </template>
                </span>
              </div>
              
              <!-- 智能推荐 -->
              <div v-if="shouldShowFrameRateRecommendation()" class="frame-recommendation">
                <div class="recommendation-header">
                  <el-icon class="recommend-icon"><Star /></el-icon>
                  <span class="recommend-title">智能推荐</span>
                </div>
                <div class="recommendation-options">
                  <el-button 
                    size="small" 
                    type="primary" 
                    plain
                    @click="applyRecommendedFrameRate('optimal')"
                    class="recommend-btn"
                  >
                    使用最优帧率 {{ videoFrameRate }}fps
                  </el-button>
                  <el-button 
                    size="small" 
                    type="success" 
                    plain
                    @click="applyRecommendedFrameRate('half')"
                    class="recommend-btn"
                  >
                    使用节能帧率 {{ Math.floor((videoFrameRate || 1) / 2) }}fps
                  </el-button>
                </div>
              </div>
            </el-form-item>
            
            <el-form-item label="置信度阈值" prop="confidence_threshold" class="form-item-half">
              <div class="confidence-slider">
                <el-slider 
                  v-model="detectionForm.confidence_threshold" 
                  :min="0.1" 
                  :max="1" 
                  :step="0.05"
                  show-input
                  :format-tooltip="formatTooltip"
                  class="custom-slider"
                />
              </div>
            </el-form-item>
          </div>
          
          <div class="form-row">
            <el-form-item label="上传视频" prop="video" class="form-item-full">
              <el-upload
                ref="uploadRef"
                :auto-upload="false"
                :show-file-list="false"
                :on-change="handleFileChange"
                accept="video/*"
                drag
                class="video-upload"
              >
                <div v-if="!selectedVideo" class="upload-area">
                  <div class="upload-icon-wrapper">
                    <el-icon class="upload-icon"><VideoPlay /></el-icon>
                  </div>
                  <div class="upload-text">点击或拖拽视频到此处上传</div>
                  <div class="upload-tip">支持 MP4、AVI、MOV、MKV、FLV、WMV 格式，最大500MB</div>
                </div>
                <div v-else class="preview-container">
                  <div class="video-preview">
                    <video :src="videoPreview" controls class="preview-video" />
                    <div class="video-info">
                      <div class="info-item">
                        <el-icon><Document /></el-icon>
                        <span class="info-label">文件名:</span>
                        <span class="info-value">{{ selectedVideo.name }}</span>
                      </div>
                      <div class="info-item">
                        <el-icon><DataAnalysis /></el-icon>
                        <span class="info-label">文件大小:</span>
                        <span class="info-value">{{ formatFileSize(selectedVideo.size) }}</span>
                      </div>
                    </div>
                  </div>
                  <div class="preview-overlay">
                    <el-button type="danger" size="small" @click="removeVideo" class="remove-btn">
                      <el-icon><Delete /></el-icon>
                      移除视频
                    </el-button>
                  </div>
                </div>
              </el-upload>
            </el-form-item>
          </div>

          <div v-if="userStore.isAdmin()" class="form-row dataset-switch-row">
            <el-form-item label="同时生成数据集" class="form-item-full">
              <el-switch v-model="datasetForm.enabled" active-text="开启" inactive-text="关闭" />
              <span class="dataset-help">抽取原始帧并生成自动标注，之后可在待划分数据集中人工检查和分配。</span>
            </el-form-item>
          </div>

          <div v-if="userStore.isAdmin() && datasetForm.enabled" class="dataset-options">
            <el-alert
              title="生成的数据集先保存为待划分草稿，不会自动拆分或直接用于训练。"
              type="info"
              :closable="false"
              show-icon
            />
            <div class="form-row form-row-double">
              <el-form-item label="数据集名称" required class="form-item-half">
                <el-input v-model="datasetForm.name" maxlength="100" show-word-limit placeholder="请输入唯一的数据集名称" />
              </el-form-item>
              <el-form-item label="数据集模式" class="form-item-half">
                <el-radio-group v-model="datasetForm.mode" @change="handleDatasetModeChange">
                  <el-radio-button label="single">单模态</el-radio-button>
                  <el-radio-button label="multimodal">RGB + X</el-radio-button>
                </el-radio-group>
              </el-form-item>
            </div>
            <div class="form-row form-row-double">
              <el-form-item label="抽帧间隔" class="form-item-half">
                <el-input-number v-model="datasetForm.frameInterval" :min="1" :max="10000" :step="1" />
                <span class="dataset-help">每隔 N 帧取一帧；N=2 时取第 1、3、5…帧</span>
              </el-form-item>
              <el-form-item label="自动标注模型" class="form-item-half">
                <el-select v-model="datasetForm.annotationModelId" placeholder="选择与数据集模式兼容的已发布模型">
                  <el-option
                    v-for="model in compatibleAnnotationModels"
                    :key="model.id"
                    :label="formatAnnotationModelLabel(model)"
                    :value="model.id"
                  />
                </el-select>
                <span v-if="!compatibleAnnotationModels.length" class="dataset-warning">当前模式暂无兼容的已发布模型</span>
              </el-form-item>
            </div>
            <div class="form-row form-row-double">
              <el-form-item label="标注置信度" class="form-item-half">
                <el-slider v-model="datasetForm.confidence" :min="0.05" :max="1" :step="0.05" show-input />
              </el-form-item>
              <el-form-item label="数据集说明" class="form-item-half">
                <el-input v-model="datasetForm.description" placeholder="可选" maxlength="300" />
              </el-form-item>
            </div>
            <template v-if="datasetForm.mode === 'multimodal'">
              <div class="form-row form-row-double">
                <el-form-item label="第二模态" class="form-item-half">
                  <el-select v-model="datasetForm.secondaryModality" placeholder="请选择" @change="refreshAnnotationModelDefault">
                    <el-option v-for="modality in availableSecondaryModalities" :key="modality" :label="modality" :value="modality" />
                  </el-select>
                </el-form-item>
                <el-form-item label="时间偏移（秒）" class="form-item-half">
                  <el-input-number v-model="datasetForm.secondaryTimeOffset" :step="0.05" :precision="3" />
                  <span class="dataset-help">正值表示第二路视频相对 RGB 时间轴后移</span>
                </el-form-item>
              </div>
              <div class="form-row">
                <el-form-item label="第二路视频" class="form-item-full">
                  <el-upload :auto-upload="false" :show-file-list="false" accept="video/*" :on-change="handleSecondaryVideoChange">
                    <el-button>选择第二路视频</el-button>
                  </el-upload>
                  <span v-if="datasetForm.secondaryVideo" class="dataset-file-name">{{ datasetForm.secondaryVideo.name }}</span>
                  <el-button v-if="datasetForm.secondaryVideo" link type="danger" @click="datasetForm.secondaryVideo = null">移除</el-button>
                </el-form-item>
              </div>
            </template>
            <div class="dataset-estimate">
              预计生成样本：{{ estimatedDatasetSampleCount ?? '选择视频后估算' }} 张
            </div>
          </div>
          
          <div class="form-row">
            <el-form-item class="form-item-full">
              <el-button
                type="primary"
                @click="handleDetect"
                :loading="detecting || frameRateDetecting"
                :disabled="!detectionForm.model_id || !selectedVideo || frameRateDetecting"
                class="detect-btn"
                size="large"
              >
                <el-icon v-if="!detecting && !frameRateDetecting"><VideoPlay /></el-icon>
                {{ detecting ? '提交中...' : frameRateDetecting ? '检测帧率中...' : '开始识别' }}
              </el-button>
            </el-form-item>
          </div>
        </el-form>
      </el-card>

      <!-- 处理进度卡片 -->
      <el-card v-if="currentTaskId" class="progress-card" shadow="never">
        <template #header>
          <div class="card-header">
            <el-icon class="header-icon"><Loading /></el-icon>
            <span>处理进度</span>
            <el-button 
              type="success" 
              size="small" 
              @click="viewResult" 
              :disabled="taskProgress.status !== VIDEO_DETECTION_STATUS.COMPLETED"
              class="view-result-btn"
            >
              <el-icon><View /></el-icon>
              查看结果
            </el-button>
          </div>
        </template>
        
        <div class="progress-content">
          <!-- 状态信息 -->
          <div class="status-info">
            <el-descriptions :column="3" border class="status-descriptions">
              <el-descriptions-item label="任务状态">
                <span class="status-text" :class="getVideoStatusType(taskProgress.status)">
                  {{ getVideoStatusText(taskProgress.status) }}
                </span>
              </el-descriptions-item>
              <el-descriptions-item label="读取进度">
                <span class="progress-text">{{ taskProgress.progress?.toFixed(1) || 0 }}%</span>
              </el-descriptions-item>
              <el-descriptions-item label="处理进度">
                <span class="progress-text">{{ taskProgress.processing_progress?.toFixed(1) || 0 }}%</span>
              </el-descriptions-item>
              <el-descriptions-item label="视频总帧数">
                <span class="info-text">{{ taskProgress.total_frames || '-' }}</span>
              </el-descriptions-item>
              <el-descriptions-item label="需要处理帧数">
                <span class="info-text">{{ taskProgress.target_frames || '-' }}</span>
              </el-descriptions-item>
              <el-descriptions-item label="已处理帧数">
                <span class="info-text">{{ taskProgress.actual_processed_frames || 0 }}</span>
              </el-descriptions-item>
              <el-descriptions-item label="设置帧率">
                <span class="info-text">{{ taskProgress.frame_rate || '-' }} fps</span>
              </el-descriptions-item>
              <el-descriptions-item label="开始时间">
                <span class="time-text">
                  {{ taskProgress.start_time ? formatDateTime(taskProgress.start_time) : '-' }}
                </span>
              </el-descriptions-item>
              <el-descriptions-item label="完成时间">
                <span class="time-text">
                  {{ taskProgress.complete_time ? formatDateTime(taskProgress.complete_time) : '-' }}
                </span>
              </el-descriptions-item>
            </el-descriptions>
            <div
              v-if="userStore.isAdmin() && taskProgress.dataset_generation_status && taskProgress.dataset_generation_status !== 'not_requested'"
              class="dataset-progress"
            >
              <el-alert
                v-if="taskProgress.dataset_generation_status === 'failed' && taskProgress.status === VIDEO_DETECTION_STATUS.COMPLETED"
                title="视频识别已完成，但待划分数据集生成失败；原视频结果不受影响。"
                :description="taskProgress.dataset_statistics?.error || '可在数据集管理页重试。'"
                type="warning"
                :closable="false"
                show-icon
              />
              <el-descriptions :column="3" border>
                <el-descriptions-item label="数据集生成状态">
                  {{ getDatasetGenerationStatusText(taskProgress.dataset_generation_status) }}
                </el-descriptions-item>
                <el-descriptions-item label="已抽取帧数">
                  {{ taskProgress.dataset_statistics?.extracted ?? 0 }}
                </el-descriptions-item>
                <el-descriptions-item label="第二模态已配对">
                  {{ taskProgress.dataset_statistics?.paired ?? 0 }}
                </el-descriptions-item>
                <el-descriptions-item label="缺少第二模态">
                  {{ taskProgress.dataset_statistics?.missing_secondary ?? 0 }}
                </el-descriptions-item>
                <el-descriptions-item label="最大配对误差">
                  {{ taskProgress.dataset_statistics?.max_pairing_error_ms == null ? '-' : `${taskProgress.dataset_statistics.max_pairing_error_ms.toFixed(1)} ms` }}
                </el-descriptions-item>
                <el-descriptions-item v-if="taskProgress.dataset_draft_id" label="待划分数据集">
                  <el-link type="primary" @click="openDatasetDraft(taskProgress.dataset_draft_id)">
                    {{ taskProgress.dataset_draft_id }}（打开划分页）
                  </el-link>
                </el-descriptions-item>
              </el-descriptions>
            </div>
          </div>
          
          <!-- 进度条 -->
          <div class="progress-bar-section">
            <!-- 视频读取进度 -->
            <div class="progress-item">
              <div class="progress-header">
                <span class="progress-label">视频读取进度</span>
                <span class="progress-info">{{ (taskProgress.progress || 0).toFixed(1) }}% ({{ taskProgress.processed_frames || 0 }}/{{ taskProgress.total_frames || 0 }} 帧)</span>
              </div>
              <el-progress 
                :percentage="taskProgress.progress || 0" 
                :status="taskProgress.status === VIDEO_DETECTION_STATUS.FAILED ? 'exception' : 
                         taskProgress.status === VIDEO_DETECTION_STATUS.COMPLETED ? 'success' : undefined"
                :stroke-width="8"
                class="progress-bar"
              />
            </div>

            <!-- AI处理进度 -->
            <div class="progress-item">
              <div class="progress-header">
                <span class="progress-label">AI处理进度</span>
                <span class="progress-info">{{ (taskProgress.processing_progress || 0).toFixed(1) }}% ({{ taskProgress.actual_processed_frames || 0 }}/{{ taskProgress.target_frames || 0 }} 帧)</span>
              </div>
              <el-progress 
                :percentage="taskProgress.processing_progress || 0" 
                :status="taskProgress.status === VIDEO_DETECTION_STATUS.FAILED ? 'exception' : 
                         taskProgress.status === VIDEO_DETECTION_STATUS.COMPLETED ? 'success' : undefined"
                :stroke-width="8"
                class="progress-bar"
                color="#67c23a"
              />
            </div>

            <!-- 详细信息展示 -->
            <div class="frame-rate-info">
              <div class="info-item">
                <span class="info-label">状态:</span>
                <span class="info-value status-value" :class="getVideoStatusType(taskProgress.status)">
                  {{ getVideoStatusText(taskProgress.status) }}
                </span>
              </div>
              <div class="info-item">
                <span class="info-label">帧率设置:</span>
                <span class="info-value">{{ taskProgress.frame_rate || '-' }} fps</span>
              </div>
              <div class="info-item">
                <span class="info-label">视频总帧数:</span>
                <span class="info-value">{{ taskProgress.total_frames || '-' }}</span>
              </div>
              <div class="info-item">
                <span class="info-label">需要处理:</span>
                <span class="info-value">{{ taskProgress.target_frames || '-' }} 帧</span>
              </div>
            </div>
          </div>
        </div>
      </el-card>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, reactive, onUnmounted, computed, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElForm } from 'element-plus'
import { 
  VideoPlay, 
  Setting, 
  Loading, 
  View, 
  Document, 
  DataAnalysis, 
  Delete,
  InfoFilled,
  Star
} from '@element-plus/icons-vue'
import { detectionApi } from '@/api/detection'
import type { PublishedModel } from '@/api/detection'
import { useUserStore } from '@/stores/user'
import { 
  videoDetectionApi, 
  type VideoDetectionProgress,
  getCompatibleAnnotationModels,
  getDefaultAnnotationModelId,
  appendVideoDatasetFields,
  VIDEO_DETECTION_STATUS,
  getVideoStatusText,
  getVideoStatusType
} from '@/api/video_detection'

const router = useRouter()
const userStore = useUserStore()

// 响应式数据
const detecting = ref(false)
const selectedVideo = ref<File | null>(null)
const videoPreview = ref<string>('')
const publishedModels = ref<PublishedModel[]>([])
const currentTaskId = ref<string>('')
const videoFrameRate = ref<number | null>(null)
const videoDuration = ref(0)
const frameRateDetecting = ref(false) // 帧率检测状态
const videoCompletionNotified = ref(false)
const taskProgress = ref<VideoDetectionProgress>({
  status: 0,
  progress: 0
})

// 轮询定时器
let progressTimer: NodeJS.Timeout | null = null

// 生成默认标题
const generateDefaultTitle = (): string => {
  const randomNum = Math.floor(Math.random() * 10000)
  return `海上风电场视频检测_${randomNum}`
}

// 生成默认描述
const generateDefaultDescription = (): string => {
  return '使用 AI 模型对海上风电场巡检视频进行缺陷检测，自动识别设备表面的裂纹、污垢、鸟粪等缺陷'
}

// 表单数据
const detectionForm = reactive({
  title: generateDefaultTitle(),
  description: generateDefaultDescription(),
  model_id: '',
  confidence_threshold: 0.5,
  frame_rate: 5,
  video: null as File | null
})

const datasetForm = reactive({
  enabled: false,
  name: '',
  description: '',
  mode: 'single' as 'single' | 'multimodal',
  frameInterval: 1,
  annotationModelId: '',
  confidence: 0.5,
  secondaryModality: '',
  secondaryTimeOffset: 0,
  secondaryVideo: null as File | null,
})

const availableSecondaryModalities = computed(() =>
  [...new Set(
    publishedModels.value
      .filter(model => model.model_mode === 'multimodal' && model.secondary_modality)
      .map(model => model.secondary_modality as string)
  )]
)

const compatibleAnnotationModels = computed(() =>
  getCompatibleAnnotationModels(
    publishedModels.value,
    datasetForm.mode,
    datasetForm.secondaryModality
  )
)

const estimatedDatasetSampleCount = computed(() => {
  if (!selectedVideo.value || !videoFrameRate.value || !videoDuration.value) return null
  const totalFrames = Math.round(videoFrameRate.value * videoDuration.value)
  return Math.ceil(totalFrames / Math.max(1, datasetForm.frameInterval))
})

const refreshAnnotationModelDefault = () => {
  if (compatibleAnnotationModels.value.some(model => model.id === datasetForm.annotationModelId)) return
  const selectedModel = publishedModels.value.find(model => model.id === detectionForm.model_id)
  datasetForm.annotationModelId = getDefaultAnnotationModelId(
    selectedModel,
    datasetForm.mode,
    datasetForm.secondaryModality
  )
}

watch(
  () => [datasetForm.enabled, datasetForm.mode, datasetForm.secondaryModality, detectionForm.model_id],
  () => {
    if (datasetForm.mode === 'single') {
      datasetForm.secondaryModality = ''
      datasetForm.secondaryTimeOffset = 0
      datasetForm.secondaryVideo = null
    } else if (!availableSecondaryModalities.value.includes(datasetForm.secondaryModality)) {
      datasetForm.secondaryModality = availableSecondaryModalities.value[0] || ''
    }
    refreshAnnotationModelDefault()
  }
)

// 表单引用
const detectionFormRef = ref<InstanceType<typeof ElForm>>()
const uploadRef = ref()

// 表单验证规则
const detectionRules = {
  title: [
    { required: true, message: '请输入任务标题', trigger: 'blur' }
  ],
  model_id: [
    { required: true, message: '请选择识别模型', trigger: 'change' }
  ]
}

// 格式化滑块提示
const formatTooltip = (val: number) => `${(val * 100).toFixed(0)}%`

// 格式化文件大小
const formatFileSize = (bytes: number): string => {
  if (bytes === 0) return '0 Bytes'
  const k = 1024
  const sizes = ['Bytes', 'KB', 'MB', 'GB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i]
}

// 获取视频帧率
const getVideoFrameRate = (videoElement: HTMLVideoElement): Promise<number> => {
  return new Promise((resolve) => {
    // 创建离屏canvas来计算帧率
    const canvas = document.createElement('canvas')
    const ctx = canvas.getContext('2d')
    let frameCount = 0
    let lastTime = performance.now()
    let frameRate = 30 // 默认帧率
    
    const countFrames = () => {
      if (ctx && videoElement.readyState >= 2) {
        frameCount++
        if (frameCount >= 30) { // 测量30帧来估算帧率
          const currentTime = performance.now()
          const timeDiff = (currentTime - lastTime) / 1000
          frameRate = Math.round(frameCount / timeDiff)
          resolve(Math.min(frameRate, 60)) // 限制最大60fps
          return
        }
      }
      requestAnimationFrame(countFrames)
    }
    
    // 如果无法精确测量，使用元数据或默认值
    setTimeout(() => {
      resolve(frameRate)
    }, 1000)
    
    countFrames()
  })
}

// 计算实际处理帧率
const getActualFrameRate = (): number => {
  if (!videoFrameRate.value) return detectionForm.frame_rate
  return Math.min(videoFrameRate.value, detectionForm.frame_rate)
}

// 检查帧率是否需要调整（只在小于时显示提示）
const shouldAdjustFrameRate = (): boolean => {
  return videoFrameRate.value !== null && detectionForm.frame_rate > videoFrameRate.value
}

// 是否显示帧率提示警告（只在设置的帧率小于视频帧率时显示）
const shouldShowFrameRateAlert = (): boolean => {
  return false // 不再显示帧率提示，因为已经自动调整
}

// 是否显示智能推荐（不再显示）
const shouldShowFrameRateRecommendation = (): boolean => {
  return false
}

// 应用推荐的帧率
const applyRecommendedFrameRate = (type: 'optimal' | 'half') => {
  if (!videoFrameRate.value) return
  
  if (type === 'optimal') {
    detectionForm.frame_rate = videoFrameRate.value
    ElMessage.success(`已设置为最优帧率 ${videoFrameRate.value}fps`)
  } else if (type === 'half') {
    const halfRate = Math.max(1, Math.floor(videoFrameRate.value / 2))
    detectionForm.frame_rate = halfRate
    ElMessage.success(`已设置为节能帧率 ${halfRate}fps，将提高处理速度`)
  }
}

// 格式化时间
const formatDateTime = (dateTime: string): string => {
  return new Date(dateTime).toLocaleString('zh-CN')
}

// 获取已发布的模型
const fetchPublishedModels = async () => {
  try {
    const response = await detectionApi.getPublishedModels()
    publishedModels.value = response.models
    
    // 默认选择最新的模型（按ID降序排列，取第一个）
    if (publishedModels.value.length > 0) {
      // API 已按发布时间倒序返回。
      detectionForm.model_id = publishedModels.value[0].id
    }
  } catch (error) {
    ElMessage.error('获取模型列表失败')
    console.error('Error fetching published models:', error)
  }
}

// 处理文件选择
const handleFileChange = async (file: any) => {
  if (!file.raw) return
  
  const allowedTypes = ['video/mp4', 'video/avi', 'video/mov', 'video/quicktime', 'video/x-msvideo', 'video/x-ms-wmv']
  if (!allowedTypes.includes(file.raw.type)) {
    ElMessage.error('请上传支持的视频格式（MP4、AVI、MOV、MKV、FLV、WMV）')
    return
  }
  
  const maxSize = 500 * 1024 * 1024 // 500MB
  if (file.raw.size > maxSize) {
    ElMessage.error('视频大小不能超过500MB')
    return
  }
  
  selectedVideo.value = file.raw
  detectionForm.video = file.raw
  
  // 创建预览
  const url = URL.createObjectURL(file.raw)
  videoPreview.value = url
  
  // 重置帧率信息
  videoFrameRate.value = null
  frameRateDetecting.value = true // 开始帧率检测

  // 检测视频帧率
  try {
    const video = document.createElement('video')
    video.src = url
    video.muted = true

    await new Promise((resolve) => {
      video.addEventListener('loadedmetadata', resolve)
    })
    videoDuration.value = Number.isFinite(video.duration) ? video.duration : 0

    // 尝试获取视频真实帧率
    let detectedFPS = 25 // 默认值，改为更合理的25fps

    // 方式1: 尝试从视频轨道获取帧率信息
    if (video.videoWidth && video.videoHeight && video.duration) {
      try {
        // 使用 requestVideoFrameCallback API (如果支持)
        if ('requestVideoFrameCallback' in video) {
          let frameCount = 0
          const startTime = performance.now()
          const maxSampleTime = 5000 // 最多采样5秒，以便检测低帧率视频

          const frameCallback = (now: number) => {
            frameCount++
            const elapsed = now - startTime

            // 采样足够的帧数或时间后计算帧率
            // 对于低帧率视频，需要更长时间才能采集到足够的帧
            if (frameCount >= 10 || elapsed >= maxSampleTime) {
              const fps = Math.round((frameCount * 1000) / elapsed)
              if (fps >= 1 && fps <= 60) {
                detectedFPS = fps
              }
              videoFrameRate.value = detectedFPS
              frameRateDetecting.value = false // 帧率检测完成
              // 如果设置的帧率大于视频实际帧率，直接设置为视频帧率
              if (detectionForm.frame_rate > detectedFPS) {
                detectionForm.frame_rate = detectedFPS
                console.log(`自动调整帧率: ${detectedFPS}fps`)
              }
              video.currentTime = 0 // 重置到开始
            } else if (video.currentTime < Math.min(5, video.duration)) {
              video.requestVideoFrameCallback(frameCallback)
            } else {
              // 如果没有足够的采样，使用已有数据
              if (frameCount > 0 && elapsed > 0) {
                const fps = Math.round((frameCount * 1000) / elapsed)
                if (fps >= 1 && fps <= 60) {
                  detectedFPS = fps
                }
              }
              videoFrameRate.value = detectedFPS
              frameRateDetecting.value = false // 帧率检测完成
              // 如果设置的帧率大于视频实际帧率，直接设置为视频帧率
              if (detectionForm.frame_rate > detectedFPS) {
                detectionForm.frame_rate = detectedFPS
                console.log(`自动调整帧率: ${detectedFPS}fps`)
              }
              video.currentTime = 0
            }
          }

          // 开始采样
          video.currentTime = 0
          video.play().then(() => {
            video.requestVideoFrameCallback(frameCallback)
          }).catch(() => {
            // 如果自动播放失败，直接使用默认帧率
            videoFrameRate.value = detectedFPS
            frameRateDetecting.value = false // 帧率检测完成
            // 如果设置的帧率大于视频实际帧率，直接设置为视频帧率
            if (detectionForm.frame_rate > detectedFPS) {
              detectionForm.frame_rate = detectedFPS
              console.log(`catch中调整帧率: ${detectedFPS}fps`)
            }
          })

          // 超时保护
          setTimeout(() => {
            if (videoFrameRate.value === null) {
              videoFrameRate.value = detectedFPS
            }
            frameRateDetecting.value = false // 超时保护中也要设置帧率检测完成
            // 如果设置的帧率大于视频实际帧率，直接设置为视频帧率
            if (detectionForm.frame_rate > detectedFPS) {
              detectionForm.frame_rate = detectedFPS
              console.log(`超时保护中调整帧率: ${detectedFPS}fps`)
            }
            video.pause()
            video.currentTime = 0
          }, 6000)

          return // 异步处理，直接返回
        }

      } catch (error) {
        console.warn('帧率检测失败，使用默认值', error)
      }
    }

    videoFrameRate.value = detectedFPS
    frameRateDetecting.value = false // 同步检测完成

    // 如果设置的帧率大于视频实际帧率，直接设置为视频帧率
    if (detectionForm.frame_rate > detectedFPS) {
      detectionForm.frame_rate = detectedFPS
      console.log(`同步调整帧率: ${detectedFPS}fps`)
    }

    // 清理临时video元素
    video.remove()

  } catch (error) {
    console.warn('无法检测视频帧率，使用默认值', error)
    videoFrameRate.value = 25
    frameRateDetecting.value = false // 异常情况也要设置检测完成
    // 如果设置的帧率大于视频实际帧率，直接设置为视频帧率
    if (detectionForm.frame_rate > 25) {
      detectionForm.frame_rate = 25
      console.log(`异常情况调整帧率: 25fps`)
    }
  }
}

const formatAnnotationModelLabel = (model: PublishedModel) =>
  `${model.name}（${model.model_mode === 'multimodal' ? `RGB + ${model.secondary_modality || 'X'}` : '单模态'}）`

const handleDatasetModeChange = (mode: string | number | boolean | undefined) => {
  if (mode === 'single') {
    datasetForm.secondaryModality = ''
    datasetForm.secondaryTimeOffset = 0
    datasetForm.secondaryVideo = null
  } else {
    datasetForm.secondaryModality = availableSecondaryModalities.value[0] || ''
  }
  refreshAnnotationModelDefault()
}

const handleSecondaryVideoChange = (uploadFile: any) => {
  const file = uploadFile.raw as File | undefined
  if (!file) return
  if (file.size > 500 * 1024 * 1024) {
    ElMessage.error('第二路视频不能超过500MB')
    return
  }
  const extension = file.name.split('.').pop()?.toLowerCase()
  if (!extension || !['mp4', 'avi', 'mov', 'mkv', 'flv', 'wmv'].includes(extension)) {
    ElMessage.error('请上传支持的视频格式（MP4、AVI、MOV、MKV、FLV、WMV）')
    return
  }
  datasetForm.secondaryVideo = file
}

const getDatasetGenerationStatusText = (status: NonNullable<VideoDetectionProgress['dataset_generation_status']>) => {
  const labels = {
    not_requested: '未开启',
    pending: '等待中',
    generating: '抽帧与标注中',
    completed: '已生成待人工划分',
    failed: '生成失败',
  }
  return labels[status]
}

const openDatasetDraft = (draftId: string) => {
  router.push({ name: 'DatasetDraftSplit', params: { draftId } })
}

// 移除视频
const removeVideo = (e: Event) => {
  e.stopPropagation()
  if (videoPreview.value) {
    URL.revokeObjectURL(videoPreview.value)
  }
  selectedVideo.value = null
  detectionForm.video = null
  videoPreview.value = ''
  videoFrameRate.value = null
  videoDuration.value = 0
  frameRateDetecting.value = false // 重置帧率检测状态
  uploadRef.value?.clearFiles()

  // 清除当前任务
  if (progressTimer) {
    clearInterval(progressTimer)
    progressTimer = null
  }
  currentTaskId.value = ''
  taskProgress.value = { status: 0, progress: 0 }
  datasetForm.secondaryVideo = null
}

// 执行视频识别
const handleDetect = async () => {
  if (!detectionFormRef.value) return
  
  const valid = await detectionFormRef.value.validate().catch(() => false)
  if (!valid) return
  
  if (!selectedVideo.value) {
    ElMessage.error('请选择视频')
    return
  }
  if (datasetForm.enabled) {
    if (!datasetForm.name.trim()) {
      ElMessage.error('请输入待划分数据集名称')
      return
    }
    if (!datasetForm.annotationModelId) {
      ElMessage.error('请选择兼容的自动标注模型')
      return
    }
    if (datasetForm.mode === 'multimodal' && (!datasetForm.secondaryModality || !datasetForm.secondaryVideo)) {
      ElMessage.error('多模态数据集需要选择第二模态并上传第二路视频')
      return
    }
  }
  
  try {
    detecting.value = true
    
    const formData = new FormData()
    formData.append('video', selectedVideo.value)
    formData.append('title', detectionForm.title)
    formData.append('description', detectionForm.description)
    formData.append('model_id', detectionForm.model_id)
    formData.append('confidence_threshold', detectionForm.confidence_threshold.toString())
    
    // 使用实际的处理帧率
    const actualFrameRate = getActualFrameRate()
    formData.append('frame_rate', actualFrameRate.toString())
    appendVideoDatasetFields(formData, datasetForm)
    
    // 如果实际帧率与设置不同，给出最终确认提示
    if (actualFrameRate !== detectionForm.frame_rate) {
      ElMessage.info(`将按 ${actualFrameRate}fps 进行处理（已优化为最佳效率）`)
    }
    
    const response = await videoDetectionApi.detectVideo(formData)
    currentTaskId.value = response.id
    videoCompletionNotified.value = false
    
    ElMessage.success(response.message || '视频识别任务创建成功')
    
    // 开始轮询进度
    startProgressPolling()
    
  } catch (error: any) {
    ElMessage.error(error.response?.data?.message || '提交识别任务失败')
  } finally {
    detecting.value = false
  }
}

// 开始轮询进度
const startProgressPolling = () => {
  if (progressTimer) {
    clearInterval(progressTimer)
  }
  
  progressTimer = setInterval(async () => {
    if (!currentTaskId.value) return
    
    try {
      const progress = await videoDetectionApi.getProgress(currentTaskId.value)
      
      // 添加调试日志
      console.log('进度查询结果:', {
        taskId: currentTaskId.value,
        status: progress.status,
        progress: progress.progress,
        total_frames: progress.total_frames,
        processed_frames: progress.processed_frames,
        timestamp: new Date().toLocaleTimeString()
      })
      
      taskProgress.value = progress

      const videoFinished = progress.status === VIDEO_DETECTION_STATUS.COMPLETED ||
        progress.status === VIDEO_DETECTION_STATUS.FAILED
      const datasetStatus = progress.dataset_generation_status || 'not_requested'
      const datasetFinished = ['not_requested', 'completed', 'failed'].includes(datasetStatus)
      if (videoFinished && !videoCompletionNotified.value) {
        videoCompletionNotified.value = true
        if (progress.status === VIDEO_DETECTION_STATUS.COMPLETED) {
          ElMessage.success('视频识别完成')
        } else {
          ElMessage.error('视频识别失败')
        }
      }
      // 视频与数据集状态独立；视频完成后继续轮询，直到抽帧/标注结束。
      if (videoFinished && datasetFinished) {
        if (progressTimer) {
          clearInterval(progressTimer)
          progressTimer = null
        }
      }
    } catch (error) {
      console.error('获取进度失败:', error)
    }
  }, 1500) // 每1.5秒轮询一次，提高响应性
}

// 查看结果
const viewResult = () => {
  router.push(`/video-detection-records`)
}

// 组件卸载时清理
onUnmounted(() => {
  if (progressTimer) {
    clearInterval(progressTimer)
  }
  if (videoPreview.value) {
    URL.revokeObjectURL(videoPreview.value)
  }
})

// 初始化
onMounted(() => {
  fetchPublishedModels()
})
</script>

<style scoped>
.video-detection {
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
  margin: 0 0 10px 0;
  font-size: 36px;
  color: #2c3e50;
  font-weight: bold;
}

.page-subtitle {
  margin: 0;
  font-size: 18px;
  color: #7f8c8d;
}

.header-right {
  display: flex;
  align-items: center;
}

.header-stats {
  display: flex;
  gap: 20px;
}

.stat-item {
  text-align: center;
}

.stat-number {
  font-size: 28px;
  font-weight: bold;
  color: #3498db;
  display: block;
}

.stat-label {
  font-size: 14px;
  color: #7f8c8d;
}

.main-content {
  max-width: 1200px;
  margin: 0 auto;
  padding: 0 20px;
}

.config-card {
  margin-bottom: 30px;
  border-radius: 12px;
  border: 1px solid #f0f2f5;
  box-shadow: 0 2px 8px 0 rgba(0, 0, 0, 0.06);
}

.card-header {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 20px;
  border-bottom: 1px solid #f8f9fa;
  font-weight: 600;
  color: #2c3e50;
}

.header-icon {
  font-size: 20px;
  color: #3498db;
}

.detection-form {
  padding: 30px;
  background-color: #ffffff;
}

.detection-form .el-form-item__label {
  font-weight: 600;
  color: #2c3e50;
  font-size: 15px;
}

.form-row {
  margin-bottom: 25px;
}

.form-row-double {
  display: flex;
  gap: 30px;
  align-items: flex-start;
}

.form-item-full {
  width: 100%;
}

.form-item-half {
  flex: 1;
  min-height: 100px;
}

.form-item-half:last-child {
  flex: 1.8;
}

.title-input, .description-input {
  width: 100%;
}

.model-select {
  width: 100%;
}

.model-select :deep(.el-select-dropdown__item) {
  height: auto;
  min-height: 70px;
  padding: 12px 20px;
  line-height: normal;
  display: flex;
  align-items: center;
}

.model-option {
  display: flex;
  flex-direction: column;
  gap: 6px;
  width: 100%;
  justify-content: center;
}

.model-name {
  font-weight: 600;
  color: #2c3e50;
  font-size: 14px;
}

.model-desc {
  font-size: 12px;
  color: #7f8c8d;
  line-height: 1.4;
  white-space: normal;
  word-wrap: break-word;
  word-break: break-all;
  max-width: 100%;
}

.confidence-slider {
  width: 100%;
  padding: 20px;
  background-color: #fafbfc;
  border-radius: 8px;
  border: 1px solid #ecf0f1;
  min-height: 80px;
  display: flex;
  flex-direction: column;
  justify-content: center;
}


.frame-rate-input {
  padding: 20px;
  background-color: #fafbfc;
  border-radius: 8px;
  border: 1px solid #ecf0f1;
  min-height: 80px;
}

.frame-rate-input > div:first-child {
  display: flex;
  align-items: center;
  gap: 15px;
  margin-bottom: 10px;
}

.frame-input {
  width: 150px;
}

.frame-info {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.frame-unit {
  color: #7f8c8d;
  font-size: 14px;
  font-weight: 500;
}

.frame-hint {
  color: #3498db;
  font-size: 12px;
  font-weight: 600;
  background-color: #ecf6ff;
  padding: 2px 8px;
  border-radius: 4px;
  display: inline-block;
}

.frame-rate-alert {
  margin-top: 15px;
  border-radius: 6px;
}

.progress-item {
  margin-bottom: 20px;
}

.progress-item:last-child {
  margin-bottom: 0;
}

.progress-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}

.progress-label {
  font-weight: 600;
  color: #2c3e50;
  font-size: 14px;
}

.progress-info {
  color: #7f8c8d;
  font-size: 12px;
}

.progress-detail {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 15px;
  background-color: #fafbfc;
  border-radius: 8px;
  border: 1px solid #ecf0f1;
}

.frame-rate-info {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  padding: 12px;
  background-color: #f8f9fa;
  border-radius: 6px;
  border: 1px solid #e9ecef;
  font-size: 13px;
  color: #495057;
}

.frame-rate-info .info-item {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 140px;
}

.frame-rate-info .info-label {
  font-weight: 600;
  color: #2c3e50;
}

.frame-rate-info .info-value {
  font-weight: 500;
  color: #3498db;
}

.frame-rate-alert .el-alert__content {
  font-size: 13px;
  line-height: 1.5;
}

.frame-explanation {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 12px;
  padding: 12px;
  background-color: #f8fbff;
  border-radius: 6px;
  border: 1px solid #e6f3ff;
}

.info-icon {
  color: #3498db;
  font-size: 16px;
  flex-shrink: 0;
}

.explanation-text {
  color: #5a6c7d;
  font-size: 13px;
  line-height: 1.4;
}

.frame-recommendation {
  margin-top: 15px;
  padding: 15px;
  background: linear-gradient(135deg, #fff9e6 0%, #ffeaa7 100%);
  border-radius: 8px;
  border: 1px solid #f39c12;
}

.recommendation-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}

.recommend-icon {
  color: #f39c12;
  font-size: 16px;
}

.recommend-title {
  color: #d68910;
  font-weight: 600;
  font-size: 14px;
}

.recommendation-options {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}

.recommend-btn {
  font-size: 12px;
  height: 28px;
  border-radius: 6px;
  transition: all 0.3s ease;
}

.recommend-btn:hover {
  transform: translateY(-1px);
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
}

.video-upload .el-upload-dragger {
  width: 100%;
  height: 240px;
  border: 2px dashed #d5dbdb;
  border-radius: 12px;
  background-color: #fdfdfd;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  transition: all 0.3s ease;
}

.video-upload .el-upload-dragger:hover {
  border-color: #3498db;
  background-color: #f8fbff;
  transform: translateY(-2px);
}

.upload-area {
  text-align: center;
  padding: 40px 20px;
  color: #7f8c8d;
}

.upload-icon-wrapper {
  margin-bottom: 20px;
  display: flex;
  justify-content: center;
  align-items: center;
  width: 80px;
  height: 80px;
  background: linear-gradient(135deg, #f8fbff 0%, #ecf6ff 100%);
  border-radius: 50%;
  margin: 0 auto 20px;
}

.upload-icon {
  font-size: 40px;
  color: #3498db;
}

.upload-text {
  font-size: 16px;
  font-weight: 600;
  color: #2c3e50;
  margin-bottom: 12px;
}

.upload-tip {
  font-size: 13px;
  color: #7f8c8d;
  line-height: 1.5;
}

.preview-container {
  position: relative;
  display: inline-block;
  width: 100%;
}

.video-preview {
  width: 100%;
  height: 280px;
  border-radius: 6px;
  overflow: hidden;
  background-color: #000;
  position: relative;
}

.preview-video {
  width: 100%;
  height: 100%;
  object-fit: contain;
  border-radius: 6px;
  position: relative;
  z-index: 1;
}

.video-info {
  position: absolute;
  bottom: 0;
  left: 0;
  background-color: rgba(0, 0, 0, 0.5);
  color: #fff;
  padding: 5px 10px;
  border-bottom-left-radius: 6px;
  border-bottom-right-radius: 6px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  width: 100%;
  box-sizing: border-box;
}

.info-item {
  display: flex;
  align-items: center;
  margin-right: 20px;
}

.info-label {
  font-size: 12px;
  color: #fff;
  margin-left: 5px;
}

.info-value {
  font-size: 14px;
  font-weight: bold;
}

.preview-overlay {
  position: absolute;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
  background-color: rgba(0, 0, 0, 0.5);
  border-radius: 6px;
  display: flex;
  justify-content: center;
  align-items: center;
  opacity: 0;
  transition: opacity 0.3s ease;
}

.preview-container:hover .preview-overlay {
  opacity: 1;
}

.remove-btn {
  background-color: #e74c3c;
  border-color: #e74c3c;
}

.remove-btn:hover {
  background-color: #c0392b;
  border-color: #c0392b;
}

.detect-btn {
  width: 100%;
  height: 52px;
  font-size: 18px;
  font-weight: 600;
  background: linear-gradient(135deg, #3498db 0%, #2980b9 100%);
  border: none;
  border-radius: 8px;
  transition: all 0.3s ease;
  box-shadow: 0 4px 12px rgba(52, 152, 219, 0.2);
}

.detect-btn:hover {
  transform: translateY(-2px);
  box-shadow: 0 6px 20px rgba(52, 152, 219, 0.3);
}

.detect-btn:disabled {
  background: #bdc3c7;
  transform: none;
  box-shadow: none;
}

.progress-card {
  margin-top: 30px;
  border-radius: 12px;
  border: 1px solid #f0f2f5;
  box-shadow: 0 2px 8px 0 rgba(0, 0, 0, 0.06);
}

.progress-content {
  padding: 25px;
  background-color: #ffffff;
}

.status-info {
  margin-bottom: 20px;
}

.status-descriptions {
  background-color: #fafbfc;
  border: 1px solid #ecf0f1;
  border-radius: 4px;
  padding: 15px;
}

.progress-bar-section {
  margin-top: 20px;
}

.progress-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 10px;
}

.progress-header h4 {
  margin: 0;
  font-size: 16px;
  color: #2c3e50;
}

.progress-text {
  font-size: 18px;
  font-weight: bold;
  color: #3498db;
}

.progress-bar {
  margin-top: 10px;
}

.progress-status {
  margin-top: 10px;
  text-align: right;
}

.status-tag {
  font-size: 14px;
}

.view-result-btn {
  margin-left: auto;
}

.custom-slider .el-slider__runway {
  height: 8px;
  background-color: #ecf0f1;
  border-radius: 4px;
}

.custom-slider .el-slider__bar {
  background-color: #3498db;
  border-radius: 4px;
}

.custom-slider .el-slider__button {
  border-color: #3498db;
  background-color: #3498db;
}

/* 普通文字样式 */
.status-text {
  font-weight: 600;
  font-size: 14px;
  padding: 2px 8px;
  border-radius: 4px;
  display: inline-block;
}

.status-text.success {
  color: #67c23a;
  background-color: #f0f9ff;
}

.status-text.warning {
  color: #e6a23c;
  background-color: #fdf6ec;
}

.status-text.danger {
  color: #f56c6c;
  background-color: #fef0f0;
}

.status-text.info {
  color: #909399;
  background-color: #f4f4f5;
}

.progress-text {
  font-weight: 600;
  color: #409eff;
  font-size: 14px;
}

.info-text {
  font-weight: 500;
  color: #303133;
  font-size: 14px;
}

.time-text {
  font-weight: 500;
  color: #606266;
  font-size: 13px;
}

.status-value {
  font-weight: 500;
}

.status-value.success {
  color: #67c23a;
}

.status-value.warning {
  color: #e6a23c;
}

.status-value.danger {
  color: #f56c6c;
}

.status-value.info {
  color: #909399;
}

.dataset-options {
  display: flex;
  flex-direction: column;
  gap: 14px;
  margin: 8px 0 22px 140px;
  padding: 18px;
  border: 1px solid #dce8f5;
  border-radius: 8px;
  background: #f8fbff;
}

.dataset-switch-row .dataset-help,
.dataset-help,
.dataset-file-name {
  margin-left: 12px;
  color: #7a8492;
  font-size: 12px;
}

.dataset-warning {
  display: block;
  margin-top: 6px;
  color: #e6a23c;
  font-size: 12px;
}

.dataset-estimate {
  color: #52657b;
  font-size: 13px;
}

.dataset-progress {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin-top: 16px;
}

@media (max-width: 768px) {
  .header-content {
    flex-direction: column;
    align-items: flex-start;
    gap: 10px;
  }

  .header-stats {
    width: 100%;
    justify-content: space-around;
  }

  .stat-item {
    text-align: center;
  }

  .form-row-double {
    flex-direction: column;
    gap: 0;
  }
  
  .form-item-half {
    width: 100%;
  }

  .dataset-options {
    margin-left: 0;
  }

  .video-preview {
    max-height: 200px;
  }

  .detection-form {
    padding: 20px;
  }

  .confidence-slider,
  .frame-rate-input {
    padding: 15px;
  }

  .frame-input {
    width: 120px;
  }
}
</style>
