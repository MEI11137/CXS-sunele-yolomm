// MD5: b1dffafc8d880a67c11f018c9759c31e
/*
 * 版权所有 ©  2025 羊羊小栈 （GJQ）
 *
 * 作者：羊羊小栈
 * 时间：2025-09-06 23:44:27
 *
 * 本系统为原创作品，禁止二次销售！禁止将系统对应的视频、文档进行二次发布！
 * 违者需立即停止侵权行为，并按照【羊羊小栈系统版权声明及保护条款】中规定数额进行赔偿，并承担相应法律责任。
 */

export interface WebSocketMessage {
  type: 'fire_detected' | 'camera_status' | 'frame' | 'start_stream' | 'stop_stream' | 'check_camera' | 'stream_stopped'
  data: {
    wsId?: string      // 添加 wsId 字段
    cameraId: number
    rtspUrl?: string   // RTSP地址
    frame?: string     // base64编码的图片
    status?: number    // 摄像头状态
    has_fire?: boolean
    has_smoke?: boolean
    fire_confidence?: number
    smoke_confidence?: number
    fire_bbox?: number[]
    smoke_bbox?: number[]
    level?: number
    location?: string
    [key: string]: any
  }
}

// WebSocket服务器URL
const WS_URL = import.meta.env.VITE_ALGO_WS_URL || `ws://${window.location.hostname}:5000/ws`

class WebSocketClient {
  private ws: WebSocket | null = null
  private url: string
  private reconnectAttempts = 0
  private maxReconnectAttempts = 5
  private reconnectTimeout = 3000
  private messageHandlers: ((message: WebSocketMessage) => void)[] = []
  private isActiveClose = false
  private reconnectTimer: number | null = null
  private messageQueue: any[] = []  // 消息队列
  private isConnecting = false  // 连接状态标记

  constructor(url: string) {
    this.url = url
    console.log('WebSocket URL:', url)
  }

  // 连接WebSocket
  connect() {
    // 如果已经连接，不要重复连接
    if (this.ws?.readyState === WebSocket.OPEN) {
      console.log('WebSocket已经连接')
      this.processMessageQueue()  // 处理可能存在的消息队列
      return
    }

    // 如果正在连接，等待连接完成
    if (this.ws?.readyState === WebSocket.CONNECTING || this.isConnecting) {
      console.log('WebSocket正在连接中')
      return
    }

    // 如果存在旧的连接，先清理
    this.cleanup()

    // 重置状态
    this.isActiveClose = false
    this.isConnecting = true
    console.log('正在连接WebSocket:', this.url)

    try {
      this.ws = new WebSocket(this.url)

      this.ws.onopen = () => {
        console.log('WebSocket连接成功')
        this.reconnectAttempts = 0
        this.clearReconnectTimer()
        this.isConnecting = false
        this.processMessageQueue()  // 连接成功后处理消息队列
      }

      this.ws.onmessage = (event) => {
        try {
          const message: WebSocketMessage = JSON.parse(event.data)
          this.messageHandlers.forEach(handler => handler(message))
        } catch (error) {
          console.error('WebSocket消息解析失败:', error)
        }
      }

      this.ws.onclose = (event) => {
        console.log('WebSocket连接关闭:', {
          code: event.code,
          reason: event.reason,
          wasClean: event.wasClean,
          isActiveClose: this.isActiveClose
        })

        this.isConnecting = false

        // 只有在非主动关闭且不是正常关闭的情况下才重连
        if (!this.isActiveClose && !event.wasClean) {
          this.scheduleReconnect()
        } else {
          console.log('WebSocket正常关闭，不进行重连')
          this.cleanup()
        }
      }

      this.ws.onerror = (event) => {
        this.isConnecting = false
        // 只在开发环境下打印详细错误
        if (import.meta.env.DEV) {
          console.error('WebSocket错误:', event)
        }
        // 生产环境下打印简化的错误信息
        console.error('WebSocket连接出错')
      }
    } catch (error) {
      this.isConnecting = false
      console.error('创建WebSocket连接失败:', error)
      this.scheduleReconnect()
    }
  }

  // 处理消息队列
  private processMessageQueue() {
    while (this.messageQueue.length > 0) {
      const message = this.messageQueue.shift()
      this.sendImmediate(message)
    }
  }

  // 立即发送消息
  private sendImmediate(message: any) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      console.error('WebSocket未连接，消息发送失败')
      return
    }

    try {
      this.ws.send(JSON.stringify(message))
    } catch (error) {
      console.error('发送消息失败:', error)
    }
  }

  // 发送消息
  send(message: any) {
    // 如果WebSocket未连接，先连接
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      console.log('WebSocket未连接，将消息加入队列并尝试连接')
      this.messageQueue.push(message)
      this.connect()
      return
    }

    this.sendImmediate(message)
  }

  // 清理资源
  private cleanup() {
    if (this.ws) {
      // 移除所有事件监听器
      this.ws.onopen = null
      this.ws.onclose = null
      this.ws.onmessage = null
      this.ws.onerror = null

      // 如果连接还在打开状态，尝试正常关闭
      if (this.ws.readyState === WebSocket.OPEN) {
        try {
          this.ws.close(1000, 'Normal closure')
        } catch (error) {
          console.error('关闭WebSocket连接失败:', error)
        }
      }

      this.ws = null
    }

    this.clearReconnectTimer()
  }

  // 清除重连定时器
  private clearReconnectTimer() {
    if (this.reconnectTimer) {
      window.clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
  }

  // 安排重连
  private scheduleReconnect() {
    if (this.reconnectAttempts >= this.maxReconnectAttempts) {
      console.error('WebSocket重连次数超过最大限制')
      this.cleanup()
      return
    }

    this.reconnectAttempts++
    console.log('WebSocket计划第' + this.reconnectAttempts + '次重连')

    this.clearReconnectTimer()
    this.reconnectTimer = window.setTimeout(() => {
      console.log('执行重连...')
      this.connect()
    }, this.reconnectTimeout)
  }

  // 添加消息处理器
  addMessageHandler(handler: (message: WebSocketMessage) => void) {
    this.messageHandlers.push(handler)
  }

  // 移除消息处理器
  removeMessageHandler(handler: (message: WebSocketMessage) => void) {
    const index = this.messageHandlers.indexOf(handler)
    if (index !== -1) {
      this.messageHandlers.splice(index, 1)
    }
  }

  // 关闭连接
  close() {
    console.log('主动关闭WebSocket连接')
    this.isActiveClose = true
    this.cleanup()
  }

  // 检查WebSocket是否已连接
  isConnected(): boolean {
    return this.ws !== null && this.ws.readyState === WebSocket.OPEN
  }
}

// 创建WebSocket客户端实例
const wsClient = new WebSocketClient(WS_URL)

export default wsClient 