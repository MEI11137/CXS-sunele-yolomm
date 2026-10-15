// MD5: a16d7d944be6def29d81fd01062d51a1
/*
 * 版权所有 © 2025 羊羊小栈 (GJQ)
 *
 * 作者: 羊羊小栈
 * 时间: 2025-09-06 23:44:27
 *
 * 本系统为原创作品,禁止二次销售!禁止将系统对应的视频、文档进行二次发布!
 * 违者需立即停止侵权行为,并按照【羊羊小栈系统版权声明及保护条款】中规定数额进行赔偿,并承担相应法律责任。
 */

import axios from 'axios'
import type { AxiosInstance, AxiosRequestConfig, AxiosResponse } from 'axios'
import { ElMessage } from 'element-plus'

// 创建 axios 实例
const service: AxiosInstance = axios.create({
  baseURL: import.meta.env.VITE_API_URL,
  timeout: 10000,
})

// 请求拦截器
service.interceptors.request.use(
  (config) => {
    // 从localStorage获取token
    const token = localStorage.getItem('token')
    if (token) {
      config.headers.Authorization = `Bearer ${token}`
    }
    return config
  },
  (error) => {
    return Promise.reject(error)
  }
)

// 响应拦截器
service.interceptors.response.use(
  (response: AxiosResponse) => {
    // 如果是二进制数据，直接返回
    if (response.config.responseType === 'blob') {
      return response.data
    }

    const { code, msg, data } = response.data

    if (code === 200) {
      return data
    }

    ElMessage.error(msg || '请求失败')
    return Promise.reject(new Error(msg || '请求失败'))
  },
  (error) => {
    // 处理文件下载失败的情况
    if (error.config?.responseType === 'blob' && error.response?.data) {
      // 尝试读取blob中的错误信息
      return new Promise((_, reject) => {
        const reader = new FileReader()
        reader.onload = () => {
          try {
            const errorData = JSON.parse(reader.result as string)
            ElMessage.error(errorData.msg || '下载失败')
            reject(new Error(errorData.msg || '下载失败'))
          } catch (e) {
            ElMessage.error('下载失败')
            reject(new Error('下载失败'))
          }
        }
        reader.onerror = () => {
          ElMessage.error('下载失败')
          reject(new Error('下载失败'))
        }
        reader.readAsText(error.response.data)
      })
    }
    
    ElMessage.error(error.message || '请求失败')
    return Promise.reject(error)
  }
)

// 封装请求方法
const fileRequest = {
  /**
   * 上传文件
   * @param bucket 存储桶名称
   * @param file 文件对象
   * @param isCache 是否为缓存文件
   * @param config 额外的请求配置
   * @returns { url: string, bucket: string, objectKey: string }
   */
  upload<T = { url: string, bucket: string, objectKey: string }>(bucket: string, file: File, isCache?: boolean, config?: AxiosRequestConfig): Promise<T> {
    const formData = new FormData()
    formData.append('file', file)
    if (isCache) {
      formData.append('is_cache', 'true')
    }
    return service.post(`/file/upload/${bucket}`, formData, {
      headers: {
        'Content-Type': 'multipart/form-data',
      },
      ...config,
    }) as Promise<T>
  },

  /**
   * 获取文件
   * @param bucket 存储桶名称
   * @param objectKey 文件名
   * @param config 额外的请求配置
   * @returns 返回文件内容
   */
  get(bucket: string, objectKey: string, config?: AxiosRequestConfig): Promise<Blob> {
    return service.get(`/file/${bucket}/${objectKey}`, {
      responseType: 'blob',
      ...config,
    })
  },

  /**
   * 删除文件
   * @param bucket 存储桶名称
   * @param objectKey 文件名
   * @param config 额外的请求配置
   * @returns 返回响应数据
   */
  delete<T = any>(bucket: string, objectKey: string, config?: AxiosRequestConfig): Promise<T> {
    return service.delete(`/file/${bucket}/${objectKey}`, config) as Promise<T>
  },

  /**
   * 获取文件URL
   * @param bucket 存储桶名称
   * @param objectKey 文件名
   * @returns 返回文件访问URL
   */
  getFileUrl(bucket: string, objectKey: string): string {
    return `${import.meta.env.VITE_API_URL}/file/${bucket}/${objectKey}`
  }
}

export { fileRequest }
