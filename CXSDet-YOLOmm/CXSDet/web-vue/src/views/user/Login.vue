<!-- MD5: ac2e62ec2daf024dc29ddbe5677a5082 -->
<!-- 登录页面 -->
<!--
版权所有 ©  2025  羊羊小栈 (GJQ)

作者：羊羊小栈
时间：2025-09-06 23:44:27

本系统为原创作品，禁止二次销售！禁止将系统对应的视频、文档进行二次发布！
违者需立即停止侵权行为，并按照【羊羊小栈系统版权声明及保护条款】中规定数额进行赔偿，并承担相应法律责任。
-->

<template>
  <div class="login-container">
    <!-- 左侧背景和介绍区域 -->
    <div class="left-section">
      <div class="background-overlay"></div>
      <div class="content-wrapper">
        <div class="logo-section">
          <h1 class="system-title">海上风电场检测系统</h1>
        </div>
        <div class="intro-section">
          <p class="intro-description">
            面向海上风电场的巡检影像检测系统，支持图片、视频与实时检测，
            并由人工确认结果回流训练，持续迭代模型。
          </p>
          <div class="feature-list">
            <div class="feature-item">
              <el-icon class="feature-icon"><Camera /></el-icon>
              <span>影像检测</span>
            </div>
            <div class="feature-item">
              <el-icon class="feature-icon"><DataAnalysis /></el-icon>
              <span>AI 辅助核查</span>
            </div>
            <div class="feature-item">
              <el-icon class="feature-icon"><User /></el-icon>
              <span>人工确认</span>
            </div>
            <div class="feature-item">
              <el-icon class="feature-icon"><TrendCharts /></el-icon>
              <span>样本回流后迭代模型</span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 右侧登录区域 -->
    <div class="right-section">
      <div class="login-box">
        <div class="login-header">
          <img src="@/assets/images/logo.png" alt="Logo" class="header-logo" />
          <h2 class="login-title">用户登录</h2>
          <p class="login-subtitle">欢迎使用海上风电场检测系统</p>
        </div>
        
        <el-form
          ref="loginFormRef"
          :model="loginForm"
          :rules="loginRules"
          label-width="0"
          size="large"
          class="login-form"
        >
          <el-form-item prop="username">
            <el-input
              v-model="loginForm.username"
              placeholder="请输入用户名"
              prefix-icon="User"
              class="login-input"
            />
          </el-form-item>
          <el-form-item prop="password">
            <el-input
              v-model="loginForm.password"
              type="password"
              placeholder="请输入密码"
              prefix-icon="Lock"
              show-password
              @keyup.enter="handleLogin"
              class="login-input"
            />
          </el-form-item>
          <el-form-item>
            <el-button
              type="primary"
              :loading="loading"
              class="login-button"
              @click="handleLogin"
            >
              登录
            </el-button>
          </el-form-item>
        </el-form>
        
        <div class="login-options">
          <span class="no-account">还没有账号？</span>
          <router-link to="/register" class="register-link">
            立即注册
          </router-link>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import type { FormInstance } from 'element-plus'
import { Camera, DataAnalysis, TrendCharts, User } from '@element-plus/icons-vue'
import { useUserStore } from '@/stores/user'
import { useRoute, useRouter } from 'vue-router'

const userStore = useUserStore()
const route = useRoute()
const router = useRouter()

// 登录表单
const loginForm = ref({
  username: '',
  password: ''
})

// 表单校验规则
const loginRules = {
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    { min: 3, max: 20, message: '用户名长度应在3-20个字符之间', trigger: 'blur' }
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 6, max: 20, message: '密码长度应在6-20个字符之间', trigger: 'blur' }
  ]
}

const loading = ref(false)
const loginFormRef = ref<FormInstance>()

// 处理登录
const handleLogin = async () => {
  if (!loginFormRef.value) return

  try {
    await loginFormRef.value.validate()
    loading.value = true

    await userStore.login(loginForm.value.username, loginForm.value.password)

    // 如果有重定向地址，则跳转到重定向地址
    const redirect = route.query.redirect as string
    router.replace(redirect || '/')
  } catch (error) {
    console.error('登录失败:', error)
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-container {
  height: 100vh;
  display: flex;
  overflow: hidden;
}

/* 左侧区域 */
.left-section {
  flex: 1;
  position: relative;
  background-color: var(--hx-ink);
  background-image: linear-gradient(135deg, #0c2029 0%, #123642 55%, #0f4a4d 100%);
  display: flex;
  align-items: center;
  justify-content: flex-start;
  overflow: hidden;
}

/* 等深线栅格：与主系统一致的结构纹理 */
.left-section::before {
  content: '';
  position: absolute;
  inset: 0;
  background-image: linear-gradient(to right, rgba(255, 255, 255, 0.05) 1px, transparent 1px),
    linear-gradient(to bottom, rgba(255, 255, 255, 0.05) 1px, transparent 1px);
  background-size: 28px 28px;
  pointer-events: none;
}

/* 一道巡检扫描线 */
.left-section::after {
  content: '';
  position: absolute;
  left: 0;
  right: 0;
  top: 38%;
  height: 1px;
  background: linear-gradient(90deg, transparent, rgba(99, 194, 189, 0.55), transparent);
  pointer-events: none;
}

.background-overlay {
  position: absolute;
  inset: 0;
  background: radial-gradient(circle at 78% 18%, rgba(63, 168, 164, 0.18), transparent 58%);
}

.content-wrapper {
  position: relative;
  z-index: 2;
  text-align: left;
  color: #e8f1f0;
  max-width: 560px;
  padding: 0 64px;
}

.logo-section {
  margin-bottom: 34px;
}

.system-title {
  font-size: 34px;
  font-weight: 600;
  letter-spacing: 0.06em;
  margin: 0;
  color: #f2f8f7;
  padding-left: 16px;
  border-left: 3px solid #3fa8a4;
  line-height: 1.25;
}

.intro-section {
  text-align: left;
}

.intro-title {
  font-size: 22px;
  font-weight: 600;
  margin: 0 0 18px 0;
}

.intro-description {
  font-size: 14px;
  line-height: 1.85;
  margin: 0 0 30px 0;
  max-width: 460px;
  color: #a9c2c6;
}

.feature-list {
  display: flex;
  flex-wrap: wrap;
  gap: 10px 26px;
  padding-top: 20px;
  border-top: 1px solid rgba(255, 255, 255, 0.12);
}

.feature-item {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: #cfe0de;
}

.feature-icon {
  color: #63c2bd;
  font-size: 14px;
}

.feature-icon {
  font-size: 20px;
  color: #409EFF;
}

/* 右侧区域 */
.right-section {
  width: 420px;
  background: #ffffff;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 40px;
}

.login-box {
  width: 100%;
  max-width: 400px;
}

.login-header {
  text-align: center;
  margin-bottom: 40px;
}

.header-logo {
  width: 60px;
  height: 60px;
  margin-bottom: 20px;
}

.login-title {
  font-size: 28px;
  font-weight: 600;
  color: #303133;
  margin: 0 0 8px 0;
}

.login-subtitle {
  font-size: 16px;
  color: #909399;
  margin: 0;
}

.login-form {
  margin-bottom: 30px;
}

.login-input {
  height: 50px;
}

.login-input :deep(.el-input__wrapper) {
  border-radius: 2px;
  box-shadow: 0 0 0 1px var(--hx-line) inset;
}

.login-input :deep(.el-input__wrapper:hover) {
  box-shadow: 0 0 0 1px var(--hx-sea-line) inset;
}

.login-input :deep(.el-input__wrapper.is-focus) {
  box-shadow: 0 0 0 1px var(--hx-sea) inset;
}

.login-button {
  width: 100%;
  height: 46px;
  font-size: 15px;
  font-weight: 500;
  letter-spacing: 0.12em;
  border-radius: 2px;
  background: var(--hx-sea);
  border: none;
  box-shadow: none;
}

.login-button:hover {
  background: var(--hx-sea-hover);
  box-shadow: none;
  transform: translateY(-2px);
}

.login-options {
  text-align: center;
  font-size: 14px;
}

.no-account {
  color: #909399;
  margin-right: 8px;
}

.register-link {
  color: #409EFF;
  text-decoration: none;
  font-weight: 500;
}

.register-link:hover {
  color: #66b1ff;
  text-decoration: underline;
}

/* 响应式设计 */
@media (max-width: 1200px) {
  .left-section {
    flex: 0.8;
  }
  
  .right-section {
    width: 400px;
  }
}

@media (max-width: 768px) {
  .login-container {
    flex-direction: column;
  }
  
  .left-section {
    flex: none;
    height: 300px;
  }
  
  .right-section {
    width: 100%;
    flex: 1;
  }
  
  .content-wrapper {
    padding: 20px;
  }
  
  .system-title {
    font-size: 24px;
  }
  
  .intro-title {
    font-size: 20px;
  }
  
  .intro-description {
    font-size: 14px;
  }
}
</style> 