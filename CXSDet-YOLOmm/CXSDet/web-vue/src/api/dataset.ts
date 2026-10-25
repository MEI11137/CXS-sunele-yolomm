// MD5: 851f4e26b2546e146758c06b496f75db
/*
 * 版权所有 © 2025 羊羊小栈 (GJQ) | 作者：羊羊小栈 | 时间：2025-09-06 23:44:27
 *
 * 本系统为原创作品，禁止二次销售！禁止将系统对应的视频、文档进行二次发布！
 * 违者需立即停止侵权行为，并按照【羊羊小栈系统版权声明及保护条款】中规定数额进行赔偿，并承担相应法律责任。
 */

import { request } from './request'

// 数据集相关类型定义
export interface DatasetStructure {
  total_files: number
  train_images: number
  train_labels: number
  valid_images: number
  valid_labels: number
  test_images: number
  test_labels: number
  split_counts?: Record<'train' | 'val' | 'test', number>
}

export interface Dataset {
  id: string
  name: string
  description?: string
  structure_info: DatasetStructure
  upload_time: string
  file_size: number
  status: number  // 0: 待验证, 1: 验证通过, 2: 验证失败
  zip_file_url?: string
  dataset_mode: 'single' | 'multimodal'
  primary_modality: string
  secondary_modality?: string
}

export interface DraftDatasetListItem {
  id: string
  name: string
  description?: string
  upload_time?: string
  record_kind: 'draft'
  workflow_status: 'generating' | 'draft' | 'failed' | 'finalized'
  dataset_mode: 'single' | 'multimodal'
  primary_modality: string
  secondary_modality?: string
  error_message?: string | null
  metadata?: Record<string, unknown>
  sample_count: number
  split_counts: Record<DraftSampleSplit, number>
}

export type DatasetListItem = Dataset | DraftDatasetListItem

export interface DatasetListResponse<T = Dataset> {
  datasets: T[]
  total: number
  page: number
  page_size: number
  record_kind?: 'formal' | 'draft' | 'all'
  workflow_status?: string | null
}

export type DraftSampleSplit = 'unassigned' | 'train' | 'val' | 'test' | 'excluded'

export interface DatasetDraft {
  id: string
  name: string
  description?: string | null
  creator_id?: number
  source_type: 'video' | 'feedback'
  source_dataset_id?: string | null
  video_detection_record_id?: string | null
  dataset_mode: 'single' | 'multimodal'
  primary_modality: string
  secondary_modality?: string | null
  workflow_status: 'generating' | 'draft' | 'failed' | 'finalized'
  metadata: Record<string, unknown>
  error_message?: string | null
  create_time?: string
  update_time?: string
}

export interface DatasetDraftSample {
  id: string
  draft_id: string
  sample_key: string
  split: DraftSampleSplit
  pairing_state: 'not_required' | 'paired' | 'missing'
  frame_index?: number | null
  primary_timestamp?: number | null
  secondary_frame_index?: number | null
  secondary_timestamp?: number | null
  pairing_error_ms?: number | null
  annotations: Array<Record<string, unknown>>
  rgb_preview_url: string
  secondary_preview_url?: string | null
  label_preview_url: string
}

export interface DatasetDraftSamplesResponse {
  draft: DatasetDraft
  samples: DatasetDraftSample[]
  total: number
  page: number
  page_size: number
}

export interface DraftAssignmentResponse {
  updated: number
}

export interface DraftFinalizationResponse {
  dataset_id: string
  name: string
  description?: string | null
  zip_bucket: string
  zip_object_key: string
  file_size: number
  sample_count: number
  dataset_mode: 'single' | 'multimodal'
  secondary_modality?: string | null
  structure_info: DatasetStructure
}

interface DatasetListParams {
  page?: number
  page_size?: number
  search?: string
  workflow_status?: string
}

function listDatasetRecords(
  params?: DatasetListParams & { record_kind?: 'formal' }
): Promise<DatasetListResponse<Dataset>>
function listDatasetRecords(
  params: DatasetListParams & { record_kind: 'draft' | 'all' }
): Promise<DatasetListResponse<DatasetListItem>>
function listDatasetRecords(params?: DatasetListParams & { record_kind?: 'formal' | 'draft' | 'all' }) {
  return request.get<DatasetListResponse<DatasetListItem>>('/dataset/list', { params })
}

export interface FeedbackDatasetVersion {
  dataset_id: string
  parent_dataset_id: string
  name: string
  sample_count: number
  zip_bucket: string
  zip_object_key: string
}

export interface CreateDatasetRequest {
  name: string
  description?: string
  dataset_mode: 'single' | 'multimodal'
}

export interface UpdateDatasetRequest {
  name: string
  description?: string
}


// 数据集API
export const datasetApi = {
  // 上传数据集
  upload(formData: FormData) {
    return request.post<Dataset>('/dataset/upload', formData, {
      headers: {
        'Content-Type': 'multipart/form-data'
      }
    })
  },

  // 获取数据集列表
  list: listDatasetRecords,

  // 获取数据集详情
  get(id: string) {
    return request.get<Dataset>(`/dataset/${id}`)
  },

  // 更新数据集
  update(id: string, data: UpdateDatasetRequest) {
    return request.put<null>(`/dataset/${id}`, data)
  },

  // 删除数据集
  delete(id: string) {
    return request.delete<null>(`/dataset/${id}`)
  },

  // 下载数据集
  download(id: string) {
    return request.get(`/dataset/${id}/download`, {
      responseType: 'blob'
    })
  },

  createFeedbackVersion(id: string) {
    return request.post<FeedbackDatasetVersion>(`/dataset/${id}/feedback-version`)
  },

  listDraftSamples(id: string, params?: { page?: number; page_size?: number; split?: DraftSampleSplit; pairing_state?: DatasetDraftSample['pairing_state'] }) {
    return request.get<DatasetDraftSamplesResponse>(`/dataset/drafts/${id}/samples`, { params })
  },

  assignDraftSamples(id: string, sampleIds: string[], split: DraftSampleSplit) {
    return request.post<DraftAssignmentResponse>(`/dataset/drafts/${id}/assign`, {
      sample_ids: sampleIds,
      split,
    })
  },

  repairDraftSecondary(id: string, sampleId: string, formData: FormData) {
    return request.post<DraftAssignmentResponse>(
      `/dataset/drafts/${id}/samples/${sampleId}/secondary`,
      formData,
      { headers: { 'Content-Type': 'multipart/form-data' } }
    )
  },

  finalizeDraft(id: string) {
    return request.post<DraftFinalizationResponse>(`/dataset/drafts/${id}/finalize`)
  },

  retryDraft(id: string) {
    return request.post<{ id: string; workflow_status: 'generating' }>(`/dataset/drafts/${id}/retry`)
  },

  draftSamplePreviewUrl(id: string, sampleId: string, kind: 'rgb' | 'secondary' | 'label') {
    return `/dataset/drafts/${id}/samples/${sampleId}/preview/${kind}`
  },

  getDraftSamplePreview(id: string, sampleId: string, kind: 'rgb' | 'secondary' | 'label') {
    return request.get<Blob>(`/dataset/drafts/${id}/samples/${sampleId}/preview/${kind}`, {
      responseType: 'blob'
    })
  }
}
