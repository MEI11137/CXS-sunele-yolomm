// MD5: 1837cca38e28ea9c934734ddf52ed45c
/*
 * 版权所有©2025 羊羊小栈(GJQ)
 *
 * 作者:羊羊小栈
 * 时间:2025-09-06 23:44:27
 *
 * 本系统为原创作品,禁止二次销售!禁止将系统对应的视频、文档进行二次发布!
 * 违者需立即停止侵权行为,并按照【羊羊小栈系统版权声明及保护条款】中规定数额进行赔偿,并承担相应法律责任。
 */

import { request } from './request'

// 模型相关类型定义
export interface TrainResult {
  final_epoch: number
  best_fitness: number
  error?: string
  training_metrics?: Array<{[key: string]: number | string}>
}

export interface Model {
  id: string
  name: string
  description?: string
  dataset_id: string
  dataset_name?: string
  base_model: string
  epochs: number
  batch_size: number
  status: number  // 0: 训练中, 1: 训练完成, 2: 训练失败, 3: 已发布
  model_file_url?: string
  train_result?: TrainResult
  create_time: string
  start_time?: string
  complete_time?: string
  model_mode: 'single' | 'multimodal'
  primary_modality: string
  secondary_modality?: string
  model_yaml?: string
  parent_model_id?: string
  version: number
  metrics_json?: string
  publish_status: 'draft' | 'published' | 'archived'
}

export interface ModelListResponse {
  models: Model[]
  total: number
  page: number
  page_size: number
}

export interface CreateModelRequest {
  name: string
  description?: string
  dataset_id: string
  base_model: string
  epochs: number
  batch_size: number
}

export interface UpdateModelRequest {
  name: string
  description?: string
}

export interface BaseModelsResponse {
  base_models: string[]
}

export interface IncrementalModelResponse {
  model_id: string
  parent_model_id: string
  dataset_id: string
  version: number
  model_mode: 'single' | 'multimodal'
  epochs: number
  batch_size: number
}

export interface MetricDifference {
  parent: number | null
  child: number | null
  delta: number | null
  status: 'known' | 'unknown'
}

export interface ModelComparison {
  parent_model_id: string
  child_model_id: string
  metrics: Record<string, MetricDifference>
  per_class: { parent: unknown; child: unknown; status: 'known' | 'unknown' }
}

// YOLO类别信息接口
export interface YoloClassInfo {
  chinese_name: string
  color: string
  description: string
}

export interface YoloClassesConfig {
  [key: string]: YoloClassInfo
}

export interface YoloClassesResponse {
  classes: YoloClassesConfig
}

// 模型API
export const modelApi = {
  // 创建模型并开始训练
  create(data: CreateModelRequest) {
    return request.post<Model>('/model/create', data)
  },

  // 导入外部训练完成的模型权重
  importWeight(formData: FormData) {
    return request.post<Model>('/model/import', formData, {
      headers: {
        'Content-Type': 'multipart/form-data'
      }
    })
  },

  // 获取模型列表
  list(params?: {
    page?: number
    page_size?: number
    search?: string
  }) {
    return request.get<ModelListResponse>('/model/list', { params })
  },

  // 获取模型详情
  get(id: string) {
    return request.get<Model>(`/model/${id}`)
  },

  // 更新模型
  update(id: string, data: UpdateModelRequest) {
    return request.put<null>(`/model/${id}`, data)
  },

  // 删除模型
  delete(id: string) {
    return request.delete<null>(`/model/${id}`)
  },

  // 发布模型
  publish(id: string) {
    return request.post<{ model_id: string; previous_model_id?: string; publish_status: string }>(`/model/${id}/publish`)
  },

  startIncremental(parentId: string, data: {
    feedback_dataset_id: string
    name?: string
    description?: string
    epochs?: number
    batch_size?: number
  }) {
    return request.post<IncrementalModelResponse>(`/model/${parentId}/incremental`, data)
  },

  compare(parentId: string, childId: string) {
    return request.get<ModelComparison>(`/model/${parentId}/compare/${childId}`)
  },

  // 取消发布模型
  unpublish(id: string) {
    return request.post<null>(`/model/${id}/unpublish`)
  },

  // 获取可用的基础模型列表
  getBaseModels() {
    return request.get<BaseModelsResponse>('/model/base-models')
  },

  // 获取YOLO类别配置信息
  getYoloClasses() {
    return request.get<YoloClassesResponse>('/model/classes')
  }
}
