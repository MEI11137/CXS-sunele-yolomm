// MD5: 51a09b9377d9c49a91c88f2e3f86e1ca
/*
 * 版权所有 © 2025羊羊小栈 （GJQ）
 *
 * 作者: 羊羊小栈
 * 时间: 2025-09-06 23:44:27
 *
 * 本系统为原创作品,禁止二次销售!禁止将系统对应的视频、文档进行二次发布!
 * 违者需立即停止侵权行为,并按照【羊羊小栈系统版权声明及保护条款】中规定数额进行赔偿,并承担相应法律责任。
 */

import { request } from './request'

// 图片识别相关类型定义
export interface DetectionResult {
  bbox: number[]
  confidence: number
  class: number
  class_name: string
}

export interface DetectionResponse {
  record_id: string
  record_saved: boolean
  original_image_url: string
  secondary_image_url?: string
  secondary_image_origin?: 'uploaded' | 'generated'
  result_image_url?: string
  detections: DetectionResult[]
  model_id: string
  model_name: string
  confidence_threshold: number
  detection_count: number
  model_mode: 'single' | 'multimodal'
  secondary_modality?: string
  save_data: {
    model_id: string
    model_name: string
    original_image_bucket: string
    original_image_object_key: string
    result_image_bucket?: string
    result_image_object_key?: string
    detection_result: string
    confidence_threshold: number
    enhanced_image_bucket?: string
    enhanced_image_object_key?: string
    analysis_result?: string
    suggestions?: string
  }
  enhancement_data: {
    result_image_bucket?: string
    result_image_object_key?: string
    detections: DetectionResult[]
  }
}

export interface DetectionRecord {
  id: string
  user_id: number
  user_name?: string // 管理员可见
  model_id: string
  model_name: string
  original_image_url: string
  result_image_url?: string
  detection_result: string
  confidence_threshold: number
  create_time: string
  title: string
  description?: string
  enhanced_image_url?: string
  analysis_result?: string
  suggestions?: string
  secondary_image_url?: string
  secondary_image_origin?: 'uploaded' | 'generated'
  record_status: string
  ai_review_status: string
  ai_review_result?: string
  human_review_status: string
  corrected_annotations?: string
  feedback_dataset_id?: string
  source_dataset_id?: string
  source_dataset_name?: string
  class_names?: string[]
  reviewed_by?: number
  reviewed_at?: string
  review_version: number
}

export interface HumanReviewAnnotation {
  class_name: string
  class_id: number
  bbox: [number, number, number, number]
}

export interface HumanReviewResult {
  record_id: string
  status: 'confirmed' | 'rejected'
  decision: 'confirmed' | 'no_defect' | 'rejected'
  previous_version: number
  new_version: number
  annotations: HumanReviewAnnotation[]
}

export interface FeedbackSampleResult {
  sample_id: string
  record_id: string
  dataset_id: string
  annotation_hash: string
  created: boolean
}

export interface BatchOperationItem {
  record_id: string
  status: 'success' | 'failed'
  error?: string
  result?: AIReviewResult
  dataset_id?: string
  dataset_name?: string
  sample_id?: string
  created?: boolean
}

export interface BatchDatasetGroup {
  dataset_id: string
  dataset_name: string
  record_ids: string[]
}

export interface BatchOperationResult {
  requested: number
  unique: number
  succeeded: number
  failed: number
  items: BatchOperationItem[]
  dataset_groups?: BatchDatasetGroup[]
}

export interface DetectionRecordsResponse {
  records: DetectionRecord[]
  total: number
  page: number
  page_size: number
}

export interface PublishedModel {
  id: string
  name: string
  description?: string
  model_mode: 'single' | 'multimodal'
  primary_modality: string
  secondary_modality?: string
  version: number
}

export interface SaveDetectionRequest {
  title: string
  description?: string
  save_data: {
    model_id: string
    model_name: string
    original_image_bucket: string
    original_image_object_key: string
    result_image_bucket?: string
    result_image_object_key?: string
    detection_result: string
    confidence_threshold: number
    enhanced_image_bucket?: string
    enhanced_image_object_key?: string
    analysis_result?: string
    suggestions?: string
  }
}

export interface UpdateDetectionRequest {
  title: string
  description?: string
}

export interface EnhanceImageRequest {
  bucket: string
  object_key: string
}

export interface EnhanceImageResponse {
  enhanced_image_url: string
  enhanced_image_bucket: string
  enhanced_image_object_key: string
}

export interface AnalyzeDefectsRequest {
  bucket: string
  object_key: string
  detections: DetectionResult[]
}

export interface AnalyzeDefectsResponse {
  analysis_result: string
}

export interface GenerateSuggestionsRequest {
  analysis_result: string
}

export interface GenerateSuggestionsResponse {
  suggestions: string
}

export interface AIReviewResult {
  status: 'consistent' | 'possible_false_positive' | 'possible_missed_detection' | 'needs_human_review'
  summary: string
  possible_false_positives: number[]
  possible_missed_detections: Array<{ class_name: string; bbox: number[]; reason: string }>
  attention_regions: Array<{ bbox: number[]; reason: string }>
}

export interface GenerateSuggestionsRequest {
  analysis_result: string
}

export interface GenerateSuggestionsResponse {
  suggestions: string
}

// 图片识别API
export const detectionApi = {
  // 获取已发布的模型列表
  getPublishedModels() {
    return request.get<{ models: PublishedModel[] }>('/detection/models/published')
  },

  // 图片识别
  detectImage(formData: FormData) {
    return request.post<DetectionResponse>('/detection/detect', formData, {
      headers: {
        'Content-Type': 'multipart/form-data'
      }
    })
  },

  // 保存识别结果
  saveDetection(data: SaveDetectionRequest) {
    return request.post<{ id: string }>('/detection/save', data)
  },

  // 获取识别记录列表
  getRecords(params?: {
    page?: number
    page_size?: number
    search?: string
  }) {
    return request.get<DetectionRecordsResponse>('/detection/records', { params })
  },

  // 获取识别记录详情
  getRecord(id: string) {
    return request.get<DetectionRecord>(`/detection/records/${id}`)
  },

  // 更新识别记录
  updateRecord(id: string, data: UpdateDetectionRequest) {
    return request.put<null>(`/detection/records/${id}`, data)
  },

  // 删除识别记录
  deleteRecord(id: string) {
    return request.delete<null>(`/detection/records/${id}`)
  },

  // 图像增强
  enhanceImage(data: EnhanceImageRequest) {
    return request.post<EnhanceImageResponse>('/detection/enhance-image', data)
  },

  // 缺陷分析
  analyzeDefects(data: AnalyzeDefectsRequest) {
    return request.post<AnalyzeDefectsResponse>('/detection/analyze-defects', data)
  },

  // 生成建议
  generateSuggestions(data: GenerateSuggestionsRequest) {
    return request.post<GenerateSuggestionsResponse>('/detection/generate-suggestions', data)
  },

  reviewRecord(id: string) {
    return request.post<AIReviewResult>(`/detection/records/${id}/ai-review`)
  },

  reviewRecordsBatch(recordIds: string[]) {
    return request.post<BatchOperationResult>('/detection/records/ai-review/batch', {
      record_ids: recordIds
    })
  },

  submitHumanReview(id: string, data: {
    expected_version: number
    decision: 'confirmed' | 'no_defect' | 'rejected'
    annotations: HumanReviewAnnotation[]
  }) {
    return request.post<HumanReviewResult>(`/detection/records/${id}/human-review`, data)
  },

  addToFeedback(id: string, datasetId: string) {
    return request.post<FeedbackSampleResult>(`/detection/records/${id}/feedback`, {
      dataset_id: datasetId
    })
  },

  addRecordsToFeedbackBatch(recordIds: string[]) {
    return request.post<BatchOperationResult>('/detection/records/feedback/batch', {
      record_ids: recordIds
    })
  },

  checkAIHealth() {
    return request.get<{ ok: boolean; model?: string; available: boolean }>('/detection/llm/health')
  }
}
