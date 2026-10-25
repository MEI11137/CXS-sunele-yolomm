// MD5: 6cbdb7eb7fc449bd979f25b94cee6394
/*
 * 版权所有©2025羊羊小栈（GJQ）
 *
 * 作者：羊羊小栈
 * 时间：2025-09-06 23:44:27
 *
 * 本系统为原创作品，禁止二次销售！禁止将系统对应的视频、文档进行二次发布！
 * 违者需立即停止侵权行为，并按照【羊羊小栈系统版权声明及保护条款】中规定数额进行赔偿，并承担相应法律责任。
 */

import { request } from './request'
import type { PublishedModel } from './detection'

export interface VideoDatasetFormOptions {
  enabled: boolean
  name: string
  description: string
  mode: 'single' | 'multimodal'
  frameInterval: number
  annotationModelId: string
  confidence: number
  secondaryModality: string
  secondaryTimeOffset: number
  secondaryVideo: File | null
}

export function getCompatibleAnnotationModels(
  models: PublishedModel[],
  mode: 'single' | 'multimodal',
  secondaryModality: string
): PublishedModel[] {
  return models.filter((model) =>
    model.model_mode === mode &&
    (mode === 'single' || model.secondary_modality === secondaryModality)
  )
}

export function getDefaultAnnotationModelId(
  detectionModel: PublishedModel | undefined,
  mode: 'single' | 'multimodal',
  secondaryModality: string
): string {
  if (!detectionModel || detectionModel.model_mode !== mode) return ''
  if (mode === 'multimodal' && detectionModel.secondary_modality !== secondaryModality) return ''
  return detectionModel.id
}

export function appendVideoDatasetFields(formData: FormData, options: VideoDatasetFormOptions): void {
  if (!options.enabled) return
  formData.append('generate_dataset', 'true')
  formData.append('dataset_name', options.name.trim())
  formData.append('dataset_description', options.description.trim())
  formData.append('dataset_mode', options.mode)
  formData.append('frame_interval', String(options.frameInterval))
  formData.append('annotation_model_id', options.annotationModelId)
  formData.append('annotation_confidence', String(options.confidence))
  if (options.mode === 'multimodal') {
    formData.append('secondary_modality', options.secondaryModality)
    formData.append('secondary_time_offset', String(options.secondaryTimeOffset))
    if (options.secondaryVideo) {
      formData.append('secondary_video', options.secondaryVideo)
    }
  }
}

// 视频识别相关类型定义
export interface VideoDetectionProgress {
  status: number
  total_frames?: number              // 视频总帧数
  processed_frames?: number          // 当前读取的帧数
  target_frames?: number             // 需要处理的总帧数
  actual_processed_frames?: number   // 实际已处理的帧数
  progress: number                   // 读取进度百分比
  processing_progress?: number       // 处理进度百分比
  frame_rate?: number               // 设置的帧率
  start_time?: string
  complete_time?: string
  dataset_generation_status?: 'not_requested' | 'pending' | 'generating' | 'completed' | 'failed'
  dataset_draft_id?: string | null
  dataset_statistics?: {
    extracted?: number
    paired?: number
    missing_secondary?: number
    max_pairing_error_ms?: number | null
    error?: string
  }
}

export interface VideoDetectionRecord {
  id: string
  user_id: number
  user_name?: string // 管理员可见
  model_id: string
  model_name: string
  original_video_url: string
  result_video_url?: string
  detection_result: string
  confidence_threshold: number
  frame_rate: number
  total_frames?: number
  processed_frames: number
  target_frames?: number                // 需要处理的总帧数
  actual_processed_frames?: number      // 实际已处理的帧数
  create_time: string
  start_time?: string
  complete_time?: string
  status: number
  title: string
  description?: string
  dataset_generation_status?: VideoDetectionProgress['dataset_generation_status']
  dataset_draft_id?: string | null
  dataset_statistics?: VideoDetectionProgress['dataset_statistics']
}

export interface VideoDetectionRecordsResponse {
  records: VideoDetectionRecord[]
  total: number
  page: number
  page_size: number
}

export interface CreateVideoDetectionRequest {
  title: string
  description?: string
  model_id: string
  confidence_threshold: number
  frame_rate: number
  video: File
  generate_dataset?: boolean
  dataset_name?: string
  dataset_description?: string
  dataset_mode?: 'single' | 'multimodal'
  frame_interval?: number
  annotation_model_id?: string
  annotation_confidence?: number
  secondary_modality?: string
  secondary_time_offset?: number
  secondary_video?: File
}

export interface UpdateVideoDetectionRequest {
  title: string
  description?: string
}

// 视频识别状态常量
export const VIDEO_DETECTION_STATUS = {
  PENDING: 0,     // 等待处理
  PROCESSING: 1,  // 处理中
  COMPLETED: 2,   // 处理完成
  FAILED: 3       // 处理失败
} as const

// 获取状态文本
export const getVideoStatusText = (status: number): string => {
  const statusMap: Record<number, string> = {
    0: '等待处理',
    1: '处理中',
    2: '处理完成',
    3: '处理失败'
  }
  return statusMap[status] || '未知状态'
}

// 获取状态颜色类型
export const getVideoStatusType = (status: number) => {
  const typeMap: Record<number, 'warning' | 'success' | 'danger' | 'info'> = {
    0: 'info',
    1: 'warning', 
    2: 'success',
    3: 'danger'
  }
  return typeMap[status] || 'info'
}

// 视频识别API
export const videoDetectionApi = {
  // 提交视频识别任务
  detectVideo(formData: FormData) {
    return request.post<{ id: string; dataset_draft_id?: string | null; message: string }>('/video-detection/detect', formData, {
      headers: {
        'Content-Type': 'multipart/form-data'
      }
    })
  },

  // 获取处理进度
  getProgress(id: string) {
    return request.get<VideoDetectionProgress>(`/video-detection/progress/${id}`)
  },

  // 获取视频识别记录列表
  getRecords(params?: {
    page?: number
    page_size?: number
    search?: string
  }) {
    return request.get<VideoDetectionRecordsResponse>('/video-detection/records', { params })
  },

  // 获取视频识别记录详情
  getRecord(id: string) {
    return request.get<VideoDetectionRecord>(`/video-detection/records/${id}`)
  },

  // 更新视频识别记录
  updateRecord(id: string, data: UpdateVideoDetectionRequest) {
    return request.put<null>(`/video-detection/records/${id}`, data)
  },

  // 删除视频识别记录
  deleteRecord(id: string) {
    return request.delete<null>(`/video-detection/records/${id}`)
  }
}
