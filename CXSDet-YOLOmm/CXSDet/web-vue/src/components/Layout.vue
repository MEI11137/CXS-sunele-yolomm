<!-- MD5: f89953667199cd372827404d26711cfa -->
<!-- 基础布局组件 -->
<!--
版权所有 © 2025羊羊小栈 （GJQ）

作者: 羊羊小栈
时间: 2025-09-06 23:44:27

本系统为原创作品,禁止二次销售!禁止将系统对应的视频、文档进行二次发布!
违者需立即停止侵权行为,并按照【羊羊小栈系统版权声明及保护条款】中规定数额进行赔偿,并承担相应法律责任。
-->

<template>
  <el-container class="layout-container">
    <!-- 侧边栏 -->
    <el-aside :width="isCollapse ? '64px' : '200px'" class="aside">
      <div class="logo" :class="{ 'logo-collapse': isCollapse }">
        <img src="@/assets/images/logo.png" alt="logo" />
        <span v-show="!isCollapse">光伏板缺陷检测</span>
      </div>
      <el-menu
        :default-active="activeMenu"
        class="menu"
        :router="true"
        :collapse="isCollapse"
        background-color="#304156"
        text-color="#bfcbd9"
        active-text-color="#409EFF"
      >
        <!-- 普通用户菜单 -->
        <template v-if="!isAdmin">
          <el-menu-item index="/dashboard">
            <el-icon><Monitor /></el-icon>
            <template #title>首页</template>
          </el-menu-item>
          <el-menu-item index="/detection">
            <el-icon><Camera /></el-icon>
            <template #title>图片识别</template>
          </el-menu-item>
          <el-menu-item index="/detection-records">
            <el-icon><Document /></el-icon>
            <template #title>识别记录</template>
          </el-menu-item>
          <el-menu-item index="/video-detection">
            <el-icon><VideoPlay /></el-icon>
            <template #title>视频识别</template>
          </el-menu-item>
          <el-menu-item index="/video-detection-records">
            <el-icon><Film /></el-icon>
            <template #title>视频记录</template>
          </el-menu-item>
          <el-menu-item index="/realtime-detection">
            <el-icon><View /></el-icon>
            <template #title>实时检测</template>
          </el-menu-item>
          <el-menu-item index="/realtime-detection-records">
            <el-icon><Clock /></el-icon>
            <template #title>实时记录</template>
          </el-menu-item>
          <el-menu-item index="/profile">
            <el-icon><User /></el-icon>
            <template #title>个人中心</template>
          </el-menu-item>
        </template>

        <!-- 管理员菜单 -->
        <template v-else>
          <el-menu-item index="/admin">
            <el-icon><Monitor /></el-icon>
            <template #title>控制台</template>
          </el-menu-item>
          <el-menu-item index="/admin/users">
            <el-icon><User /></el-icon>
            <template #title>用户管理</template>
          </el-menu-item>
          <el-menu-item index="/admin/datasets">
            <el-icon><Files /></el-icon>
            <template #title>数据集管理</template>
          </el-menu-item>
          <el-menu-item index="/admin/models">
            <el-icon><Box /></el-icon>
            <template #title>模型管理</template>
          </el-menu-item>
          <el-menu-item index="/detection">
            <el-icon><Camera /></el-icon>
            <template #title>图片识别</template>
          </el-menu-item>
          <el-menu-item index="/detection-records">
            <el-icon><Document /></el-icon>
            <template #title>识别记录</template>
          </el-menu-item>
          <el-menu-item index="/video-detection">
            <el-icon><VideoPlay /></el-icon>
            <template #title>视频识别</template>
          </el-menu-item>
          <el-menu-item index="/video-detection-records">
            <el-icon><Film /></el-icon>
            <template #title>视频记录</template>
          </el-menu-item>
          <el-menu-item index="/realtime-detection">
            <el-icon><View /></el-icon>
            <template #title>实时检测</template>
          </el-menu-item>
          <el-menu-item index="/realtime-detection-records">
            <el-icon><Clock /></el-icon>
            <template #title>实时记录</template>
          </el-menu-item>
        </template>
      </el-menu>
    </el-aside>

    <!-- 主要内容区 -->
    <el-container>
      <!-- 头部 -->
      <el-header class="header">
        <div class="header-left">
          <el-icon
            class="collapse-btn"
            @click="toggleCollapse"
          >
            <Fold v-if="!isCollapse" />
            <Expand v-else />
          </el-icon>
          <el-breadcrumb separator="/">
            <el-breadcrumb-item :to="{ path: '/' }">首页</el-breadcrumb-item>
            <el-breadcrumb-item>{{ route.meta.title }}</el-breadcrumb-item>
          </el-breadcrumb>
        </div>
        <div class="header-right">
          <AlgoHealthCheck class="health-check" />
          <el-dropdown @command="handleCommand">
            <span class="user-info">
              <el-avatar :size="32" :src="userInfo?.avatarUrl" />
              <span>{{ userInfo?.username }}</span>
            </span>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="profile">个人中心</el-dropdown-item>
                <el-dropdown-item command="logout">退出登录</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </el-header>

      <!-- 内容区 -->
      <el-main class="main">
        <router-view v-slot="{ Component }">
          <transition name="fade" mode="out-in">
            <component :is="Component" />
          </transition>
        </router-view>
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useUserStore } from '@/stores/user'
import { Monitor, User, Files, Box, Fold, Expand, Camera, Document, VideoPlay, Film, View, Clock } from '@element-plus/icons-vue'
import AlgoHealthCheck from './AlgoHealthCheck.vue'

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()

// 计算当前激活的菜单项
const activeMenu = computed(() => route.path)

// 用户信息
const userInfo = computed(() => userStore.userInfo)

// 是否是管理员
const isAdmin = computed(() => userStore.isAdmin())

// 侧边栏折叠状态
const isCollapse = ref(false)

// 切换侧边栏折叠状态
const toggleCollapse = () => {
  isCollapse.value = !isCollapse.value
}

// 处理下拉菜单命令
const handleCommand = (command: string) => {
  switch (command) {
    case 'profile':
      router.push('/profile')
      break
    case 'logout':
      userStore.logout()
      break
  }
}
</script>

<style scoped>
.layout-container {
  height: 100vh;
}

.aside {
  background-color: #304156;
  transition: width 0.3s;
  overflow: hidden;
}

.logo {
  height: 60px;
  display: flex;
  align-items: center;
  padding: 0 20px;
  color: #fff;
  transition: all 0.3s;
  overflow: hidden;
  white-space: nowrap;
}

.logo-collapse {
  padding: 0 16px;
}

.logo img {
  width: 32px;
  height: 32px;
  margin-right: 12px;
  flex-shrink: 0;
}

.menu {
  border-right: none;
  background-color: transparent;
}

.menu:not(.el-menu--collapse) {
  width: 200px;
}

:deep(.el-menu--collapse) {
  width: 64px;
}

:deep(.el-menu-item) {
  &.is-active {
    background-color: #263445;
  }

  &:hover {
    background-color: #263445;
  }
}

.header {
  background-color: #fff;
  border-bottom: 1px solid #dcdfe6;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 20px;
}

.header-left {
  display: flex;
  align-items: center;
}

.collapse-btn {
  font-size: 20px;
  cursor: pointer;
  margin-right: 20px;
  transition: transform 0.3s;
}

.header-right {
  display: flex;
  align-items: center;
  gap: 16px;
}

.health-check {
  margin-right: 8px;
}

.user-info {
  display: flex;
  align-items: center;
  cursor: pointer;
}

.user-info span {
  margin-left: 8px;
}

.main {
  background-color: #f0f2f5;
  padding: 20px;
}

/* 路由过渡动画 */
.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.3s ease;
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style> 