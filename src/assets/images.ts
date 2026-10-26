// 演示用真实风电叶片照片素材（Unsplash 免费商用许可）
// 说明：这些是真实的叶片/风机照片，作为界面演示占位素材；
// 接入真实后端后，由后端返回实际的巡检影像 URL 替换。
// 素材许可见 README.md 的「素材与许可」章节。

export const IMAGES = {
  // 首页左侧海上风机场景
  scene: '/images/turbine-sea.jpg',
  // 首页风机阵列 / 风机近景
  windfarm: '/images/windfarm-aerial.jpg',
  // 叶片近景（巡检影像、复核原图、缩略图共用）
  blade: '/images/blade-closeup.jpg',
  // 风力发电机近景（备用）
  turbine: '/images/scene-windfarm.jpg',
}

// 按风机编号轮换不同叶片影像，避免所有图都一样
export function bladeImageFor(turbineId: string): string {
  const n = parseInt(turbineId.replace(/\D/g, '') || '0', 10)
  const pool = [IMAGES.blade, IMAGES.turbine, IMAGES.windfarm, IMAGES.scene]
  return pool[n % pool.length]
}
