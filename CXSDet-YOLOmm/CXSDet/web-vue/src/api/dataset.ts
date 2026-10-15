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

export interface DatasetListResponse {
  datasets: Dataset[]
  total: number
  page: number
  page_size: number
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
  list(params?: {
    page?: number
    page_size?: number
    search?: string
  }) {
    return request.get<DatasetListResponse>('/dataset/list', { params })
  },

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
  }
}
