// 演示用真实海上风电照片素材（Wikimedia Commons，CC BY-SA 3.0）
// 说明：这些是真实的海上风电场照片，作为界面演示占位素材；
// 接入真实后端后，由后端返回实际的巡检影像 URL 替换。
// 素材来源与许可见 README.md 的「素材与许可」章节。

export const IMAGES = {
  // 首页左侧「海上风机场景」：爱尔兰海 Barrow 海上风场（特色图片）
  scene: '/images/offshore-barrow.jpg',
  // 首页风机阵列背景：Kent 海岸海上风场
  windfarm: '/images/offshore-kent.jpg',
  // 叶片近景（巡检影像、复核原图、缩略图共用）：Barrow 原图裁切
  blade: '/images/blade-near-1.jpg',
  // 备用风机近景：Burbo 海上风场、北海 Nordsee One
  turbine: '/images/offshore-burbo.jpg',
  // 备用场景
  altScene: '/images/offshore-nordsee.jpg',
}

// 按风机编号轮换不同的海上风电影像，避免所有图都一样
export function bladeImageFor(turbineId: string): string {
  const n = parseInt(turbineId.replace(/\D/g, '') || '0', 10)
  const pool = [IMAGES.blade, '/images/blade-near-2.jpg', '/images/blade-near-3.jpg', IMAGES.turbine, IMAGES.scene, IMAGES.windfarm]
  return pool[n % pool.length]
}
