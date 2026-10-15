<!-- MD5: 0e8c8601f28a542e884fff985a9b4823 -->
<!--
版权所有 © 2025 羊羊小栈 (GJQ)

作者: 羊羊小栈
时间: 2025-09-06 23:44:27

本系统为原创作品,禁止二次销售!禁止将系统对应的视频、文档进行二次发布!
违者需立即停止侵权行为,并按照【羊羊小栈系统版权声明及保护条款】中规定数额进行赔偿,并承担相应法律责任。
-->

<template>
  <div class="realtime-detection">
    <!-- 页面标题和控制区 -->
    <div class="page-header">
      <div class="header-content">
        <div class="title-section">
          <h1 class="page-title">
            <el-icon class="title-icon"><VideoCamera /></el-icon>
            实时光伏板缺陷检测
          </h1>
          <p class="page-subtitle">基于YOLO模型的实时图像检测与跟踪</p>
          <div class="resource-tip-inline">
            <span class="tip-text-inline">模型训练期间请勿使用实时检测，避免资源争抢导致卡顿</span>
          </div>
        </div>
        <div class="control-panel">
          <div class="control-row">
            <div class="control-group">
              <label class="control-label">检测模型</label>
              <el-select 
                v-model="selectedModel" 
                placeholder="请选择检测模型" 
                class="model-select"
                :disabled="isDetecting"
              >
                <el-option
                  v-for="model in availableModels"
                  :key="model.id"
                  :label="model.name"
                  :value="model.id"
                />
              </el-select>
            </div>
            <div class="control-group">
              <label class="control-label">置信度阈值</label>
              <div class="threshold-control">
                <el-input-number
                  v-model="confidenceThreshold"
                  :min="0.1"
                  :max="1.0"
                  :step="0.05"
                  :precision="2"
                  class="confidence-input"
                  @change="onThresholdChange"
                />
                <div class="threshold-display">
                  <span class="threshold-value">{{ (confidenceThreshold * 100).toFixed(0) }}%</span>
                </div>
              </div>
            </div>
            <div class="control-group">
              <label class="control-label">检测帧率 (FPS)</label>
              <div class="framerate-control">
                <el-input-number
                  v-model="frameRate"
                  :min="1"
                  :max="10"
                  :step="1"
                  :precision="0"
                  class="framerate-input"
                  :disabled="isDetecting"
                />
                <div class="framerate-display">
                  <span class="framerate-value">{{ frameRate }} FPS</span>
                </div>
              </div>
            </div>
            <div class="control-group">
              <el-button
                type="primary"
                size="large"
                @click="toggleDetection"
                :disabled="!selectedModel"
                :loading="starting"
                class="detection-button"
              >
                <el-icon class="button-icon"><component :is="isDetecting ? VideoPause : VideoPlay" /></el-icon>
                {{ isDetecting ? '停止检测' : '开始检测' }}
              </el-button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 主检测区域 -->
    <div class="main-content">
      <div class="video-section">
        <!-- 隐藏的摄像头和canvas元素 -->
        <video ref="videoElement" style="display: none;" autoplay muted playsinline></video>
        <canvas ref="captureCanvas" style="display: none;"></canvas>
        
        <!-- 检测画面容器 -->
        <div class="video-display-container">
          <div class="video-header">
            <h3 class="video-title">
              <el-icon><Monitor /></el-icon>
              检测画面
            </h3>
            <div class="video-status">
              <el-tag :type="isDetecting ? 'success' : 'info'" effect="dark">
                <el-icon class="status-icon"><component :is="isDetecting ? VideoPlay : VideoPause" /></el-icon>
                {{ isDetecting ? '检测中' : '已停止' }}
              </el-tag>
            </div>
          </div>
          
          <div class="video-display">
            <img
              v-if="resultImage"
              :src="resultImage"
              alt="检测结果"
              class="result-image"
            />
            <div v-else class="video-placeholder">
              <div class="placeholder-content">
                <el-icon class="placeholder-icon"><Camera /></el-icon>
                <h4>摄像头画面</h4>
                <p>选择检测模型并点击"开始检测"启动摄像头</p>
              </div>
            </div>
          </div>
        </div>
        
        <!-- 检测统计卡片 -->
        <div class="stats-cards">
          <div class="stat-card session-card">
            <div class="stat-icon">
              <el-icon color="#409eff"><DataLine /></el-icon>
            </div>
            <div class="stat-content">
              <div class="stat-value session-id-display" :title="currentSessionId">{{ formatSessionIdDisplay(currentSessionId) || '未开始' }}</div>
              <div class="stat-label">检测会话</div>
            </div>
          </div>
          
          <div class="stat-card">
            <div class="stat-icon">
              <el-icon color="#67c23a"><Aim /></el-icon>
            </div>
            <div class="stat-content">
              <div class="stat-value">{{ sessionDetectionCount }}</div>
              <div class="stat-label">新检测目标</div>
            </div>
          </div>
          
          <div class="stat-card">
            <div class="stat-icon">
              <el-icon color="#e6a23c"><Stopwatch /></el-icon>
            </div>
            <div class="stat-content">
              <div class="stat-value fps-display" :class="getFpsClass(fps)">{{ fps.toFixed(1) }}</div>
              <div class="stat-label">FPS</div>
            </div>
          </div>
          
          <div class="stat-card">
            <div class="stat-icon">
              <el-icon color="#909399"><Setting /></el-icon>
            </div>
            <div class="stat-content">
              <div class="stat-value">{{ frameRate }}</div>
              <div class="stat-label">目标帧率</div>
            </div>
          </div>
        </div>
      </div>

      <!-- 检测结果侧边栏 -->
      <div class="sidebar">
        <div class="sidebar-card">
          <div class="sidebar-header">
            <h3 class="sidebar-title">
              <el-icon><List /></el-icon>
              最近检测目标
            </h3>
            <el-tag size="small" type="info">{{ recentDetections.length }}/10</el-tag>
          </div>
          
          <div class="detections-list">
            <div
              v-for="detection in recentDetections"
              :key="detection.id"
              class="detection-item"
            >
              <div class="detection-badge" :style="{ backgroundColor: getClassColor(detection.class_name) }">
                {{ detection.track_id }}
              </div>
              <div class="detection-details">
                <div class="detection-name" :style="{ color: getClassColor(detection.class_name) }">
                  {{ getClassDisplayName(detection.class_name) }}
                </div>
                <div class="detection-meta">
                  <span class="confidence">{{ (detection.confidence * 100).toFixed(1) }}%</span>
                  <span class="separator">•</span>
                  <span class="timestamp">{{ new Date().toLocaleTimeString() }}</span>
                </div>
              </div>
            </div>
          </div>
          
          <div v-if="recentDetections.length === 0" class="no-detections">
            <el-icon class="empty-icon"><Box /></el-icon>
            <p>暂无检测结果</p>
            <small>开始检测后，新发现的目标将在此显示</small>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { ElMessage } from 'element-plus'
import { 
  Camera, VideoCamera, Monitor, 
  DataLine, Aim, Stopwatch, List, Box, Refresh, Setting,
  VideoPlay, VideoPause, WarningFilled 
} from '@element-plus/icons-vue'
import { realtimeDetectionApi } from '@/api/realtime_detection'
import { modelApi, type Model, type YoloClassesConfig } from '@/api/model'
import { getCameraErrorMessage, requestCameraStream } from '@/utils/camera'

// 响应式数据
const videoElement = ref<HTMLVideoElement>()
const captureCanvas = ref<HTMLCanvasElement>()
const selectedModel = ref<string>('')
const confidenceThreshold = ref(0.5)
const frameRate = ref(5)
const isDetecting = ref(false)
const starting = ref(false)
const resettingTracker = ref(false)
const resultImage = ref<string>('')
const currentSessionId = ref<string>('')
const sessionDetectionCount = ref(0)
const fps = ref(0)

// 检测相关数据
const availableModels = ref<Model[]>([])
const yoloClasses = ref<YoloClassesConfig>({})
const recentDetections = ref<Array<{
  id: string
  track_id: number
  class_name: string
  confidence: number
  bbox: { x1: number, y1: number, x2: number, y2: number }
}>>([])

// 检测相关变量
let mediaStream: MediaStream | null = null
let detectionInterval: NodeJS.Timeout | null = null
let fpsInterval: NodeJS.Timeout | null = null
let frameCount = 0
let lastFpsTime = Date.now()

// 组件挂载时获取已发布的模型列表
onMounted(async () => {
  await Promise.all([
    fetchPublishedModels(),
    fetchYoloClasses()
  ])
})

// 组件卸载时清理资源
onUnmounted(() => {
  stopDetection()
})

// 获取已发布的模型列表
const fetchPublishedModels = async () => {
  try {
    const response = await modelApi.list({ page_size: 100 })
    // 只显示已发布的模型
    availableModels.value = response.models.filter(model => model.status === 3)
    
    if (availableModels.value.length > 0) {
      selectedModel.value = availableModels.value[0].id
    }
  } catch (error) {
    ElMessage.error('获取模型列表失败')
    console.error('Error fetching models:', error)
  }
}

// 开始/停止检测
const toggleDetection = async () => {
  if (isDetecting.value) {
    stopDetection()
  } else {
    await startDetection()
  }
}

// 开始检测
const startDetection = async () => {
  if (!selectedModel.value) {
    ElMessage.error('请选择检测模型')
    return
  }

  starting.value = true
  
  try {
    // 请求摄像头权限；桌面设备不支持后置摄像头约束时会自动回退。
    mediaStream = await requestCameraStream()
    
    if (videoElement.value) {
      videoElement.value.srcObject = mediaStream
      await videoElement.value.play()
    }

    // 生成检测会话ID
    currentSessionId.value = generateSessionId()
    sessionDetectionCount.value = 0
    recentDetections.value = []
    frameCount = 0
    lastFpsTime = Date.now()

    // 设置画布尺寸
    if (captureCanvas.value && videoElement.value) {
      captureCanvas.value.width = videoElement.value.videoWidth || 640
      captureCanvas.value.height = videoElement.value.videoHeight || 480
    }

    // 重置YOLO追踪器
    try {
      await realtimeDetectionApi.resetTracker(selectedModel.value)
      console.log('YOLO追踪器已重置')
    } catch (error) {
      console.warn('重置YOLO追踪器失败:', error)
      // 不阻断检测流程，只记录警告
    }

    // 根据设定的帧率开始定期检测
    const detectionIntervalMs = 1000 / frameRate.value
    detectionInterval = setInterval(() => {
      captureAndDetect()
    }, detectionIntervalMs)

    // 开始FPS计算
    fpsInterval = setInterval(() => {
      const now = Date.now()
      const elapsed = now - lastFpsTime
      fps.value = frameCount / (elapsed / 1000)
      frameCount = 0
      lastFpsTime = now
    }, 1000)

    isDetecting.value = true
    ElMessage.success('实时检测已开始')
  } catch (error: unknown) {
    ElMessage({
      message: getCameraErrorMessage(error),
      type: 'error',
      duration: 10000,
      showClose: true
    })
    console.error('Error starting detection:', error)
  } finally {
    starting.value = false
  }
}

// 停止检测
const stopDetection = () => {
  // 立即设置停止标志，防止后续的检测请求更新画面
  isDetecting.value = false
  
  // 清理定时器
  if (detectionInterval) {
    clearInterval(detectionInterval)
    detectionInterval = null
  }
  
  if (fpsInterval) {
    clearInterval(fpsInterval)
    fpsInterval = null
  }

  // 停止摄像头
  if (mediaStream) {
    mediaStream.getTracks().forEach(track => track.stop())
    mediaStream = null
  }

  // 立即清空画面和重置状态
  resultImage.value = ''
  currentSessionId.value = ''
  fps.value = 0
  
  // 重置最近检测结果
  recentDetections.value = []
  sessionDetectionCount.value = 0
  
  // 使用 nextTick 确保画面清空后再显示消息
  setTimeout(() => {
    ElMessage.info('实时检测已停止')
  }, 100)
}

// 捕获摄像头帧并进行检测
const captureAndDetect = async () => {
  if (!videoElement.value || !captureCanvas.value || !isDetecting.value) {
    return
  }

  try {
    const video = videoElement.value
    const canvas = captureCanvas.value
    const context = canvas.getContext('2d')
    
    if (!context) return

    // 将当前视频帧绘制到canvas上
    context.drawImage(video, 0, 0, canvas.width, canvas.height)
    
    // 将canvas转换为base64图片数据
    const imageData = canvas.toDataURL('image/jpeg', 0.8)
    
    // 发送给后端进行检测
    const response = await realtimeDetectionApi.detect({
      model_id: selectedModel.value,
      session_id: currentSessionId.value,
      image_data: imageData,
      confidence_threshold: confidenceThreshold.value
    })

    // 只有在检测状态为true时才更新结果图片和统计
    if (isDetecting.value) {
      // 更新结果图片
      resultImage.value = response.result_image
      
      // 更新检测统计
      if (response.new_detections && response.new_detections.length > 0) {
        sessionDetectionCount.value += response.new_detections.length
        
        // 添加到最近检测结果（保留最近10个）
        response.new_detections.forEach(detection => {
          recentDetections.value.unshift(detection)
        })
        
        if (recentDetections.value.length > 10) {
          recentDetections.value = recentDetections.value.slice(0, 10)
        }
      }

      frameCount++
    }
  } catch (error) {
    console.error('Detection error:', error)
    // 检测出错时不停止检测，只打印错误日志
  }
}

// 生成会话ID
const generateSessionId = (): string => {
  const now = new Date()
  const year = now.getFullYear()
  const month = String(now.getMonth() + 1).padStart(2, '0')
  const day = String(now.getDate()).padStart(2, '0')
  const hours = String(now.getHours()).padStart(2, '0')
  const minutes = String(now.getMinutes()).padStart(2, '0')
  const seconds = String(now.getSeconds()).padStart(2, '0')
  
  // 生成3位随机数字
  const randomNum = Math.floor(Math.random() * 900) + 100
  
  return `RT_${year}${month}${day}_${hours}${minutes}${seconds}_${randomNum}`
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

// 获取类别显示名称
const getClassDisplayName = (className: string) => {
  return yoloClasses.value[className]?.chinese_name || className
}

// 获取类别颜色
const getClassColor = (className: string) => {
  return yoloClasses.value[className]?.color || '#409eff'
}

// 阈值改变处理
const onThresholdChange = (value: number | undefined) => {
  if (value !== undefined && isDetecting.value) {
    // 检测过程中实时更新阈值
    confidenceThreshold.value = value
  }
}

// 重置追踪器
const resetTracker = async () => {
  if (!selectedModel.value) return
  
  resettingTracker.value = true
  try {
    await realtimeDetectionApi.resetTracker(selectedModel.value)
    // 清空最近检测结果
    recentDetections.value = []
    sessionDetectionCount.value = 0
    ElMessage.success('追踪器已重置')
  } catch (error) {
    ElMessage.error('重置追踪器失败')
    console.error('Error resetting tracker:', error)
  } finally {
    resettingTracker.value = false
  }
}

// 格式化会话ID显示（简化版）
const formatSessionIdDisplay = (sessionId: string) => {
  if (!sessionId) return ''
  
  const parts = sessionId.split('_')
  if (parts.length >= 4) {
    const date = parts[1]
    const time = parts[2]
    const random = parts[3]
    
    // 显示时间和编号
    const formattedTime = `${time.substring(0,2)}:${time.substring(2,4)}`
    return `${formattedTime} #${random}`
  }
  
  return sessionId
}

// 获取FPS状态样式
const getFpsClass = (fps: number) => {
  if (fps >= 4) return 'fps-good'
  if (fps >= 2) return 'fps-medium' 
  return 'fps-low'
}
</script>

<style scoped>
.realtime-detection {
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

.title-section {
  flex: 1;
}

.page-title {
  font-size: 32px;
  font-weight: 600;
  margin: 0 0 2px 0;
  display: flex;
  align-items: center;
  gap: 12px;
  line-height: 1.2;
}

.title-icon {
  font-size: 36px;
  color: rgba(255, 255, 255, 0.9);
}

.page-subtitle {
  font-size: 16px;
  color: rgba(255, 255, 255, 0.8);
  margin: 0 0 2px 48px;
  line-height: 1.3;
}

.resource-tip-inline {
  font-size: 13px;
  color: #888888;
  margin: 0 0 0 48px;
  line-height: 1.3;
}

.control-panel {
  background: rgba(255, 255, 255, 0.95);
  backdrop-filter: blur(20px);
  padding: 20px;
  border-radius: 16px;
  border: 1px solid rgba(0, 0, 0, 0.06);
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.08);
}

.control-row {
  display: flex;
  align-items: flex-end;
  gap: 20px;
}

.control-group {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.control-label {
  font-size: 14px;
  color: #666666;
  font-weight: 500;
}

.model-select {
  width: 200px;
}

.threshold-control {
  display: flex;
  align-items: center;
  gap: 8px;
}

.confidence-input {
  width: 120px;
}

.threshold-display {
  background: rgba(0, 0, 0, 0.04);
  backdrop-filter: blur(12px);
  padding: 4px 8px;
  border-radius: 6px;
  border: 1px solid rgba(0, 0, 0, 0.08);
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06);
}

.threshold-value {
  font-size: 12px;
  font-weight: 600;
  color: #555555;
}

.framerate-control {
  display: flex;
  align-items: center;
  gap: 8px;
}

.framerate-input {
  width: 120px;
}

.framerate-display {
  background: rgba(0, 0, 0, 0.04);
  backdrop-filter: blur(12px);
  padding: 4px 8px;
  border-radius: 6px;
  border: 1px solid rgba(0, 0, 0, 0.08);
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06);
}

.framerate-value {
  font-size: 12px;
  font-weight: 600;
  color: #555555;
}

.quick-controls {
  margin-left: 16px;
}

.detection-button {
  height: 44px;
  padding: 0 24px;
  font-size: 16px;
  font-weight: 500;
  border-radius: 8px;
}

.detection-button .button-icon {
  margin-right: 8px;
  font-size: 16px;
  display: inline-flex;
  align-items: center;
  vertical-align: middle;
}

/* 主内容区域 */
.main-content {
  max-width: 1400px;
  margin: 0 auto;
  padding: 32px 24px;
  display: grid;
  grid-template-columns: 1fr 350px;
  gap: 32px;
  min-height: calc(100vh - 200px);
}

/* 视频区域 */
.video-section {
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.video-display-container {
  background: white;
  border-radius: 16px;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.1);
  overflow: hidden;
  flex: 1;
}

.video-header {
  padding: 20px 24px;
  background: linear-gradient(135deg, #ffffff 0%, #fafafa 100%);
  border-bottom: 1px solid #e5e5e5;
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.video-title {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
  color: #333333;
  display: flex;
  align-items: center;
  gap: 8px;
}

.video-status .el-tag {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
}

.video-status .el-tag .status-icon {
  margin-right: 4px;
  font-size: 14px;
  display: inline-flex;
  align-items: center;
  vertical-align: middle;
}

.video-display {
  position: relative;
  background: #000;
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 480px;
}

.result-image {
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
  border-radius: 0;
}

.video-placeholder {
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 480px;
}

.placeholder-content {
  text-align: center;
  color: rgba(255, 255, 255, 0.7);
}

.placeholder-icon {
  font-size: 80px;
  margin-bottom: 16px;
  color: rgba(255, 255, 255, 0.5);
}

.placeholder-content h4 {
  font-size: 24px;
  margin: 0 0 8px 0;
  color: rgba(255, 255, 255, 0.8);
}

.placeholder-content p {
  font-size: 16px;
  margin: 0;
  color: rgba(255, 255, 255, 0.6);
}

/* 统计卡片 */
.stats-cards {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
}

.stat-card {
  background: white;
  border-radius: 12px;
  padding: 20px;
  display: flex;
  align-items: center;
  gap: 16px;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.08);
  transition: all 0.3s ease;
}

.stat-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.12);
}

.stat-icon {
  width: 48px;
  height: 48px;
  border-radius: 12px;
  background: linear-gradient(135deg, rgba(0, 0, 0, 0.04) 0%, rgba(0, 0, 0, 0.08) 100%);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 24px;
}

.stat-content {
  flex: 1;
}

.stat-value {
  font-size: 20px;
  font-weight: 700;
  color: #333333;
  margin-bottom: 4px;
  word-break: break-all;
}

.session-id-display {
  font-size: 16px;
  font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
  cursor: help;
}

.fps-display {
  transition: color 0.3s ease;
}

.fps-good {
  color: #22c55e !important;
}

.fps-medium {
  color: #f59e0b !important;
}

.fps-low {
  color: #ef4444 !important;
}

.session-card:hover .session-id-display {
  color: #666666;
}

.stat-label {
  font-size: 14px;
  color: #666666;
  font-weight: 500;
}

/* 侧边栏 */
.sidebar {
  display: flex;
  flex-direction: column;
}

.sidebar-card {
  background: white;
  border-radius: 16px;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.1);
  height: 100%;
  display: flex;
  flex-direction: column;
  min-height: calc(100vh - 200px);
  max-height: calc(100vh - 200px);
}

.sidebar-header {
  padding: 24px;
  border-bottom: 1px solid #f1f3f4;
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.sidebar-title {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
  color: #495057;
  display: flex;
  align-items: center;
  gap: 8px;
}

.detections-list {
  flex: 1;
  padding: 16px 24px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.detection-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px;
  margin-bottom: 8px;
  background: #f8f9fa;
  border-radius: 12px;
  transition: all 0.3s ease;
  border: 1px solid transparent;
  flex-shrink: 0;
}

.detection-item:last-child {
  margin-bottom: 0;
}

.detection-item:hover {
  background: #e9ecef;
  border-color: #dee2e6;
  transform: translateX(4px);
}

.detection-badge {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  color: white;
  font-weight: 700;
  font-size: 12px;
  flex-shrink: 0;
}

.detection-details {
  flex: 1;
  min-width: 0;
}

.detection-name {
  font-size: 14px;
  font-weight: 600;
  margin-bottom: 4px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.detection-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: #6c757d;
}

.confidence {
  font-weight: 600;
  color: #28a745;
}

.separator {
  color: #dee2e6;
}

.timestamp {
  white-space: nowrap;
}

.no-detections {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  flex: 1;
  text-align: center;
  padding: 24px;
  color: #6c757d;
  margin: auto 0;
}

.empty-icon {
  font-size: 48px;
  color: #dee2e6;
  margin-bottom: 16px;
}

.no-detections p {
  font-size: 16px;
  font-weight: 500;
  margin: 0 0 8px 0;
}

.no-detections small {
  font-size: 14px;
  color: #adb5bd;
}

/* 响应式设计 */
@media (max-width: 1200px) {
  .main-content {
    grid-template-columns: 1fr 320px;
    gap: 24px;
  }
}

@media (max-width: 1024px) {
  .main-content {
    grid-template-columns: 1fr;
    padding: 24px 16px;
  }
  
  .sidebar {
    max-height: 400px;
  }
  
  .stats-cards {
    grid-template-columns: repeat(2, 1fr);
  }
}

@media (max-width: 768px) {
  .page-header {
    padding: 24px 16px;
  }
  
  .header-content {
    flex-direction: column;
    align-items: stretch;
    gap: 24px;
  }
  
  .control-row {
    flex-direction: column;
    align-items: stretch;
    gap: 16px;
  }
  
  .control-group {
    align-items: stretch;
  }
  
  .model-select,
  .threshold-control,
  .framerate-control {
    width: 100%;
  }
  
  .confidence-input,
  .framerate-input {
    flex: 1;
  }
  
  .main-content {
    padding: 16px;
    gap: 16px;
  }
  
  .page-title {
    font-size: 24px;
  }
  
  .stats-cards {
    grid-template-columns: 1fr;
  }
  
  .quick-controls {
    margin-left: 0;
    margin-top: 8px;
  }
}
</style>
