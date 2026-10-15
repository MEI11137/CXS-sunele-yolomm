// MD5: f8b939434ddb221da418ff9083638da4
/*
 * 版权所有 ©2025 羊羊小栈(GJQ) | 作者：羊羊小栈 | 时间：2025-09-06 23:44:27
 *
 * 本系统为原创作品，禁止二次销售！禁止将系统对应的视频、文档进行二次发布！
 * 违者需立即停止侵权行为，并按照【羊羊小栈系统版权声明及保护条款】中规定数额进行赔偿，并承担相应法律责任。
 */

import { request } from './request'

export interface RealtimeDetectionRecord {
  id: string
  user_id: number
  user_name?: string
  model_id: string
  model_name: string
  session_id: string
  track_id?: number
  detection_result: string
  confidence_threshold: number
  detection_time: string
  result_image_url?: string
  bbox?: string
  class_name: string
  confidence: number
  create_time: string
}

export interface DetectionSession {
  session_id: string
  model_name: string
  start_time: string
  end_time: string
  detection_count: number
  class_count: number
  user_id: number
  user_name?: string
}

export interface RealtimeDetectionRequest {
  model_id: string
  session_id: string
  image_data: string
  confidence_threshold?: number
}

export interface RealtimeDetectionResponse {
  result_image: string
  new_detections: Array<{
    id: string
    track_id: number
    class_name: string
    confidence: number
    bbox: {
      x1: number
      y1: number
      x2: number
      y2: number
    }
  }>
  detection_count: number
}

export interface RealtimeDetectionRecordsResponse {
  records: RealtimeDetectionRecord[]
  total: number
  size: number
  current: number
  pages: number
}

export const realtimeDetectionApi = {
  // 实时检测
  detect(data: RealtimeDetectionRequest): Promise<RealtimeDetectionResponse> {
    return request.post('/realtime-detection/detect', data)
  },

  // 获取检测记录列表
  getRecords(params: {
    page?: number
    page_size?: number
    session_id?: string
    model_id?: string
    class_name?: string
  }): Promise<RealtimeDetectionRecordsResponse> {
    return request.get('/realtime-detection/records', { params })
  },

  // 获取检测会话列表
  getSessions(): Promise<{ sessions: DetectionSession[] }> {
    return request.get('/realtime-detection/sessions')
  },

  // 删除检测记录
  deleteRecord(recordId: string): Promise<{ message: string }> {
    return request.delete(`/realtime-detection/records/${recordId}`)
  },

  // 删除检测会话及其所有相关记录
  deleteSession(sessionId: string): Promise<{ 
    message: string
    deleted_records_count: number
    deleted_files_count: number
  }> {
    return request.delete(`/realtime-detection/sessions/${sessionId}`)
  },

  // 重置YOLO追踪器
  resetTracker(modelId: string): Promise<{ message: string }> {
    return request.post('/realtime-detection/reset-tracker', { model_id: modelId })
  }
}