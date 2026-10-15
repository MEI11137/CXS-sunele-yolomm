export const PREFERRED_CAMERA_CONSTRAINTS: MediaStreamConstraints = {
  video: {
    width: { ideal: 640 },
    height: { ideal: 480 },
    facingMode: { ideal: 'environment' }
  },
  audio: false
}

const GENERIC_CAMERA_CONSTRAINTS: MediaStreamConstraints = {
  video: true,
  audio: false
}

const getErrorName = (error: unknown): string => {
  if (typeof error === 'object' && error !== null && 'name' in error) {
    return String((error as { name?: unknown }).name || '')
  }
  return ''
}

export const getCameraErrorMessage = (error: unknown): string => {
  const errorName = getErrorName(error)
  switch (errorName) {
    case 'NotAllowedError':
    case 'PermissionDeniedError':
      return '摄像头权限被拒绝。请点击浏览器地址栏左侧的锁或摄像头图标，将摄像头设为“允许”并刷新页面；同时确认 Windows“隐私和安全性 → 摄像头”已允许桌面应用访问。'
    case 'NotFoundError':
    case 'DevicesNotFoundError':
      return '未检测到可用摄像头，请确认摄像头已连接并在设备管理器中启用。'
    case 'NotReadableError':
    case 'TrackStartError':
    case 'AbortError':
      return '摄像头可能正被其他程序占用。请关闭会议、相机等程序后重试。'
    case 'SecurityError':
      return '当前页面无法安全访问摄像头。请使用 http://localhost:3000 打开系统。'
    case 'OverconstrainedError':
      return '摄像头不支持请求的分辨率，请重新尝试启动。'
    default:
      return `启动摄像头失败${errorName ? `（${errorName}）` : ''}，请检查浏览器和 Windows 摄像头权限。`
  }
}

export const requestCameraStream = async (): Promise<MediaStream> => {
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
    const securityError = new Error('Camera access requires a secure context')
    securityError.name = 'SecurityError'
    throw securityError
  }

  try {
    return await navigator.mediaDevices.getUserMedia(PREFERRED_CAMERA_CONSTRAINTS)
  } catch (error) {
    if (getErrorName(error) === 'OverconstrainedError') {
      return navigator.mediaDevices.getUserMedia(GENERIC_CAMERA_CONSTRAINTS)
    }
    throw error
  }
}
