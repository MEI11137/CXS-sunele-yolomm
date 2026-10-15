<!-- MD5: 5c21a63b9a3c28af3971c86f37a56b71 -->
<!-- 仪表盘页面 -->
<!--
版权所有©2025 羊羊小栈(GJQ)

作者:羊羊小栈
时间:2025-09-06 23:44:27

本系统为原创作品,禁止二次销售!禁止将系统对应的视频、文档进行二次发布!
违者需立即停止侵权行为,并按照【羊羊小栈系统版权声明及保护条款】中规定数额进行赔偿,并承担相应法律责任。
-->

<template>
  <div class="dashboard">
    <!-- 欢迎区域 -->
    <div class="welcome-section">
      <div class="welcome-content">
        <div class="welcome-text">
          <h1 class="welcome-title">欢迎回来，{{ userInfo?.realName || userInfo?.username || '用户' }}！</h1>
          <p class="welcome-subtitle">{{ currentDate }}</p>
        </div>
        <div class="welcome-avatar">
          <el-avatar :size="80" :src="userInfo?.avatarUrl" class="user-avatar">
            {{ userInfo?.realName?.charAt(0) || userInfo?.username?.charAt(0) || 'U' }}
          </el-avatar>
        </div>
      </div>
    </div>



    <!-- 主要内容区域 -->
    <div class="main-content">
      <div class="content-grid">
        <!-- 快捷操作区域 -->
        <div class="quick-actions">
          <div class="section-header">
            <h2 class="section-title">
              <el-icon><MagicStick /></el-icon>
              快捷操作
            </h2>
          </div>
          <div class="actions-grid">
            <div class="action-card" @click="navigateTo('/detection')">
              <div class="action-icon">
                <el-icon><Camera /></el-icon>
              </div>
              <h3 class="action-title">图片检测</h3>
              <p class="action-desc">上传光伏板图片进行缺陷检测</p>
            </div>
            
            <div class="action-card" @click="navigateTo('/video-detection')">
              <div class="action-icon">
                <el-icon><VideoPlay /></el-icon>
              </div>
              <h3 class="action-title">视频检测</h3>
              <p class="action-desc">处理视频流进行实时检测</p>
            </div>
            
            <div class="action-card" @click="navigateTo('/realtime-detection')">
              <div class="action-icon">
                <el-icon><Monitor /></el-icon>
              </div>
              <h3 class="action-title">实时检测</h3>
              <p class="action-desc">连接摄像头进行实时监控</p>
            </div>
            
            <div class="action-card" @click="navigateTo('/detection-records')">
              <div class="action-icon">
                <el-icon><Picture /></el-icon>
              </div>
              <h3 class="action-title">图片记录</h3>
              <p class="action-desc">查看图片检测历史记录</p>
            </div>
            
            <div class="action-card" @click="navigateTo('/video-detection-records')">
              <div class="action-icon">
                <el-icon><Film /></el-icon>
              </div>
              <h3 class="action-title">视频记录</h3>
              <p class="action-desc">查看视频检测历史记录</p>
            </div>
            
            <div class="action-card" @click="navigateTo('/realtime-detection-records')">
              <div class="action-icon">
                <el-icon><Monitor /></el-icon>
              </div>
              <h3 class="action-title">摄像头记录</h3>
              <p class="action-desc">查看实时检测历史记录</p>
            </div>
          </div>
        </div>

        <!-- 最近活动区域 -->
        <div class="recent-activities">
          <div class="section-header">
            <h2 class="section-title">
              <el-icon><Clock /></el-icon>
              最近活动
            </h2>
          </div>
          <div class="activities-list">
            <div v-if="recentActivities.length === 0" class="no-activities">
              <el-icon class="empty-icon"><Calendar /></el-icon>
              <p>暂无最近活动</p>
              <small>开始使用系统后，您的活动将在此显示</small>
            </div>
            <div v-else class="activity-item" v-for="activity in recentActivities" :key="activity.id">
              <div class="activity-icon" :class="activity.type">
                <el-icon><component :is="activity.icon" /></el-icon>
              </div>
              <div class="activity-content">
                <h4 class="activity-title">{{ activity.title }}</h4>
                <p class="activity-desc">{{ activity.description }}</p>
                <span class="activity-time">{{ activity.time }}</span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 系统信息区域 -->
    <div class="system-info">
      <div class="info-card">
        <div class="info-header">
          <h3 class="info-title">
            <el-icon><InfoFilled /></el-icon>
            系统信息
          </h3>
        </div>
        <div class="info-content">
          <div class="info-item">
            <span class="info-label">系统版本：</span>
            <span class="info-value">v1.0.0</span>
          </div>
          <div class="info-item">
            <span class="info-label">最后更新：</span>
            <span class="info-value">{{ systemInfo.lastUpdate }}</span>
          </div>
          <div class="info-item">
            <span class="info-label">在线状态：</span>
            <el-tag type="success" size="small">正常运行</el-tag>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { useRouter } from 'vue-router'
import { useUserStore } from '@/stores/user'
import { 
  Camera, VideoPlay, Monitor, List, MagicStick, 
  Setting, TrendCharts, Clock, Calendar, InfoFilled,
  Picture, Film
} from '@element-plus/icons-vue'

const router = useRouter()
const userStore = useUserStore()

// 用户信息
const userInfo = computed(() => userStore.userInfo)

// 当前日期
const currentDate = new Date().toLocaleDateString('zh-CN', {
  year: 'numeric',
  month: 'long',
  day: 'numeric',
  weekday: 'long'
})



// 最近活动
const recentActivities = ref([
  {
    id: 1,
    type: 'detection',
    icon: 'Camera',
    title: '完成图片检测',
    description: '成功检测光伏板图片，发现2个缺陷',
    time: '2小时前'
  },
  {
    id: 2,
    type: 'video',
    icon: 'VideoPlay',
    title: '开始视频检测',
    description: '启动视频流检测任务',
    time: '5小时前'
  },
  {
    id: 3,
    type: 'realtime',
    icon: 'Monitor',
    title: '实时检测会话',
    description: '建立实时检测连接',
    time: '1天前'
  }
])

// 系统信息
const systemInfo = ref({
  lastUpdate: '2025-09-03'
})

// 页面跳转
const navigateTo = (path: string) => {
  router.push(path)
}

// 组件挂载时加载数据
onMounted(async () => {
  // 可以在这里加载其他需要的数据
})
</script>

<style scoped>
.dashboard {
  padding: 24px;
  background-color: #f5f7fa;
  min-height: 100vh;
}

/* 欢迎区域 */
.welcome-section {
  background: linear-gradient(135deg, #ffffff 0%, #f8f9fa 100%);
  border-radius: 16px;
  padding: 32px;
  margin-bottom: 24px;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.05);
}

.welcome-content {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.welcome-title {
  font-size: 28px;
  font-weight: 600;
  color: #303133;
  margin: 0 0 8px 0;
}

.welcome-subtitle {
  font-size: 16px;
  color: #909399;
  margin: 0;
}

.user-avatar {
  background: linear-gradient(135deg, #409EFF 0%, #66b1ff 100%);
  color: white;
  font-size: 32px;
  font-weight: 600;
}



/* 主要内容区域 */
.main-content {
  margin-bottom: 24px;
}

.content-grid {
  display: grid;
  grid-template-columns: 2fr 1fr;
  gap: 24px;
}

/* 快捷操作区域 */
.quick-actions {
  background: white;
  border-radius: 16px;
  padding: 24px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06);
}

.section-header {
  margin-bottom: 24px;
}

.section-title {
  font-size: 20px;
  font-weight: 600;
  color: #303133;
  margin: 0;
  display: flex;
  align-items: center;
  gap: 8px;
}

.actions-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 16px;
}

.action-card {
  background: #f8f9fa;
  border-radius: 12px;
  padding: 20px;
  text-align: center;
  cursor: pointer;
  transition: all 0.3s ease;
  border: 2px solid transparent;
}

.action-card:hover {
  background: white;
  border-color: #409EFF;
  transform: translateY(-2px);
  box-shadow: 0 4px 12px rgba(64, 158, 255, 0.15);
}

.action-icon {
  width: 50px;
  height: 50px;
  background: linear-gradient(135deg, #409EFF 0%, #66b1ff 100%);
  border-radius: 12px;
  display: flex;
  align-items: center;
  justify-content: center;
  margin: 0 auto 16px;
  font-size: 20px;
  color: white;
}

/* 为不同的记录类型设置不同的图标颜色 */
.action-card:nth-child(4) .action-icon {
  background: linear-gradient(135deg, #67C23A 0%, #85ce61 100%);
}

.action-card:nth-child(5) .action-icon {
  background: linear-gradient(135deg, #E6A23C 0%, #ebb563 100%);
}

.action-card:nth-child(6) .action-icon {
  background: linear-gradient(135deg, #9C27B0 0%, #BA68C8 100%);
}

.action-title {
  font-size: 16px;
  font-weight: 600;
  color: #303133;
  margin: 0 0 8px 0;
}

.action-desc {
  font-size: 14px;
  color: #909399;
  margin: 0;
  line-height: 1.4;
}

/* 最近活动区域 */
.recent-activities {
  background: white;
  border-radius: 16px;
  padding: 24px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06);
}

.activities-list {
  max-height: 400px;
  overflow-y: auto;
}

.no-activities {
  text-align: center;
  padding: 40px 20px;
  color: #909399;
}

.empty-icon {
  font-size: 48px;
  margin-bottom: 16px;
  opacity: 0.5;
}

.activity-item {
  display: flex;
  align-items: flex-start;
  gap: 16px;
  padding: 16px 0;
  border-bottom: 1px solid #f0f0f0;
}

.activity-item:last-child {
  border-bottom: none;
}

.activity-icon {
  width: 40px;
  height: 40px;
  border-radius: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 16px;
  color: white;
  flex-shrink: 0;
}

.activity-icon.detection {
  background: #409EFF;
}

.activity-icon.video {
  background: #67C23A;
}

.activity-icon.realtime {
  background: #E6A23C;
}

.activity-content {
  flex: 1;
  min-width: 0;
}

.activity-title {
  font-size: 14px;
  font-weight: 600;
  color: #303133;
  margin: 0 0 4px 0;
}

.activity-desc {
  font-size: 13px;
  color: #909399;
  margin: 0 0 8px 0;
  line-height: 1.4;
}

.activity-time {
  font-size: 12px;
  color: #c0c4cc;
}

/* 系统信息区域 */
.system-info {
  margin-bottom: 24px;
}

.info-card {
  background: white;
  border-radius: 16px;
  padding: 24px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06);
}

.info-header {
  margin-bottom: 20px;
}

.info-title {
  font-size: 18px;
  font-weight: 600;
  color: #303133;
  margin: 0;
  display: flex;
  align-items: center;
  gap: 8px;
}

.info-content {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.info-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 0;
  border-bottom: 1px solid #f0f0f0;
}

.info-item:last-child {
  border-bottom: none;
}

.info-label {
  font-size: 14px;
  color: #606266;
}

.info-value {
  font-size: 14px;
  color: #303133;
  font-weight: 500;
}

/* 响应式设计 */
@media (max-width: 1200px) {
  .content-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 768px) {
  .dashboard {
    padding: 16px;
  }
  
  .welcome-content {
    flex-direction: column;
    text-align: center;
    gap: 20px;
  }
  
  .stats-grid {
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  }
  
  .actions-grid {
    grid-template-columns: 1fr;
  }
  
  .welcome-title {
    font-size: 24px;
  }
  
  .stat-number {
    font-size: 28px;
  }
}
</style> 