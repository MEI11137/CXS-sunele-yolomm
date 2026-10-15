<!-- MD5: f89ccb077904acb7e2e573ea807be104 -->
<!-- 注册页面 -->
<!--
版权所有 ©2025 羊羊小栈(GJQ) | 作者：羊羊小栈 | 时间：2025-09-06 23:44:27

本系统为原创作品，禁止二次销售！禁止将系统对应的视频、文档进行二次发布！
违者需立即停止侵权行为，并按照【羊羊小栈系统版权声明及保护条款】中规定数额进行赔偿，并承担相应法律责任。
-->

<template>
  <div class="register-container">
    <!-- 左侧背景和介绍区域 -->
    <div class="left-section">
      <div class="background-overlay"></div>
      <div class="content-wrapper">
        <div class="logo-section">
          <h1 class="system-title">光伏板缺陷检测系统</h1>
        </div>
        <div class="intro-section">
          <p class="intro-description">
            基于深度学习的光伏板缺陷检测系统，支持实时检测、批量处理、智能分析等功能。
            提供高精度、高效率的缺陷识别服务，助力光伏电站运维效率提升。
          </p>
          <div class="feature-list">
            <div class="feature-item">
              <el-icon class="feature-icon"><Camera /></el-icon>
              <span>实时检测</span>
            </div>
            <div class="feature-item">
              <el-icon class="feature-icon"><DataAnalysis /></el-icon>
              <span>智能分析</span>
            </div>
            <div class="feature-item">
              <el-icon class="feature-icon"><TrendCharts /></el-icon>
              <span>数据统计</span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 右侧注册区域 -->
    <div class="right-section">
      <div class="register-box">
        <div class="register-header">
          <img src="@/assets/images/logo.png" alt="Logo" class="header-logo" />
          <h2 class="register-title">用户注册</h2>
          <p class="register-subtitle">创建您的光伏板缺陷检测系统账户</p>
        </div>
        
        <el-form
          ref="registerFormRef"
          :model="registerForm"
          :rules="registerRules"
          label-width="80px"
          size="large"
          class="register-form"
        >
          <el-form-item label="用户名" prop="username">
            <el-input
              v-model="registerForm.username"
              placeholder="请输入用户名"
              class="register-input"
            />
          </el-form-item>
          <el-form-item label="密码" prop="password">
            <el-input
              v-model="registerForm.password"
              type="password"
              placeholder="请输入密码"
              show-password
              class="register-input"
            />
          </el-form-item>
          <el-form-item label="确认密码" prop="confirmPassword">
            <el-input
              v-model="registerForm.confirmPassword"
              type="password"
              placeholder="请确认密码"
              show-password
              class="register-input"
            />
          </el-form-item>
          <el-form-item label="真实姓名" prop="realName">
            <el-input
              v-model="registerForm.realName"
              placeholder="请输入真实姓名"
              class="register-input"
            />
          </el-form-item>
          <el-form-item label="手机号" prop="phone">
            <el-input
              v-model="registerForm.phone"
              placeholder="请输入手机号"
              class="register-input"
            />
          </el-form-item>
          <el-form-item label="邮箱" prop="email">
            <el-input
              v-model="registerForm.email"
              placeholder="请输入邮箱"
              class="register-input"
            />
          </el-form-item>
          <el-form-item>
            <el-button
              type="primary"
              :loading="loading"
              class="register-button"
              @click="handleRegister"
            >
              注册
            </el-button>
          </el-form-item>
        </el-form>
        
        <div class="register-options">
          <span class="has-account">已有账号？</span>
          <router-link to="/login" class="login-link">
            立即登录
          </router-link>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import type { FormInstance } from 'element-plus'
import { Camera, DataAnalysis, TrendCharts } from '@element-plus/icons-vue'
import { useUserStore } from '@/stores/user'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

const userStore = useUserStore()
const router = useRouter()

// 注册表单
const registerForm = ref({
  username: '',
  password: '',
  confirmPassword: '',
  realName: '',
  phone: '',
  email: ''
})

// 表单校验规则
const validatePass2 = (rule: any, value: string, callback: any) => {
  if (value === '') {
    callback(new Error('请再次输入密码'))
  } else if (value !== registerForm.value.password) {
    callback(new Error('两次输入密码不一致!'))
  } else {
    callback()
  }
}

const registerRules = {
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    { min: 3, max: 20, message: '用户名长度应在3-20个字符之间', trigger: 'blur' }
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 6, max: 20, message: '密码长度应在6-20个字符之间', trigger: 'blur' }
  ],
  confirmPassword: [
    { required: true, message: '请再次输入密码', trigger: 'blur' },
    { validator: validatePass2, trigger: 'blur' }
  ],
  realName: [
    { required: true, message: '请输入真实姓名', trigger: 'blur' },
    { min: 2, max: 20, message: '真实姓名长度应在2-20个字符之间', trigger: 'blur' }
  ],
  phone: [
    { required: true, message: '请输入手机号', trigger: 'blur' },
    { pattern: /^1[3-9]\d{9}$/, message: '请输入正确的手机号', trigger: 'blur' }
  ],
  email: [
    { required: true, message: '请输入邮箱', trigger: 'blur' },
    { type: 'email' as const, message: '请输入正确的邮箱地址', trigger: 'blur' }
  ]
}

const loading = ref(false)
const registerFormRef = ref<FormInstance>()

// 处理注册
const handleRegister = async () => {
  if (!registerFormRef.value) return

  try {
    await registerFormRef.value.validate()
    loading.value = true

    const { confirmPassword, ...registerData } = registerForm.value
    await userStore.register(registerData)

    ElMessage.success('注册成功，请登录')
    router.push('/login')
  } catch (error) {
    console.error('注册失败:', error)
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.register-container {
  height: 100vh;
  display: flex;
  overflow: hidden;
}

/* 左侧区域 */
.left-section {
  flex: 1;
  position: relative;
  background: url('@/assets/images/login-bg.jpg') no-repeat center center;
  background-size: cover;
  display: flex;
  align-items: center;
  justify-content: center;
}

.background-overlay {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background: rgba(0, 0, 0, 0.2);
}

.content-wrapper {
  position: relative;
  z-index: 2;
  text-align: center;
  color: white;
  max-width: 500px;
  padding: 40px;
}

.logo-section {
  margin-bottom: 40px;
  text-align: center;
}

.system-title {
  font-size: 36px;
  font-weight: 700;
  margin: 0;
  text-shadow: 2px 2px 4px rgba(0, 0, 0, 0.5);
}

.intro-section {
  text-align: left;
}

.intro-title {
  font-size: 24px;
  font-weight: 600;
  margin: 0 0 20px 0;
  text-shadow: 1px 1px 2px rgba(0, 0, 0, 0.5);
}

.intro-description {
  font-size: 16px;
  line-height: 1.6;
  margin: 0 0 30px 0;
  opacity: 0.9;
  text-shadow: 1px 1px 2px rgba(0, 0, 0, 0.3);
}

.feature-list {
  display: flex;
  flex-direction: column;
  gap: 15px;
}

.feature-item {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 16px;
  font-weight: 500;
}

.feature-icon {
  font-size: 20px;
  color: #409EFF;
}

/* 右侧区域 */
.right-section {
  width: 480px;
  background: #ffffff;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 40px;
  overflow-y: auto;
}

.register-box {
  width: 100%;
  max-width: 500px;
}

.register-header {
  text-align: center;
  margin-bottom: 30px;
}

.header-logo {
  width: 60px;
  height: 60px;
  margin-bottom: 20px;
}

.register-title {
  font-size: 28px;
  font-weight: 600;
  color: #303133;
  margin: 0 0 8px 0;
}

.register-subtitle {
  font-size: 16px;
  color: #909399;
  margin: 0;
}

.register-form {
  margin-bottom: 30px;
}

.register-input {
  height: 45px;
}

.register-input :deep(.el-input__wrapper) {
  border-radius: 8px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
}

.register-input :deep(.el-input__wrapper:hover) {
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
}

.register-form :deep(.el-form-item__label) {
  font-weight: 500;
  color: #606266;
}

.register-button {
  width: 100%;
  height: 50px;
  font-size: 16px;
  font-weight: 600;
  border-radius: 8px;
  background: linear-gradient(135deg, #409EFF 0%, #66b1ff 100%);
  border: none;
  box-shadow: 0 4px 12px rgba(64, 158, 255, 0.3);
}

.register-button:hover {
  background: linear-gradient(135deg, #66b1ff 0%, #409EFF 100%);
  box-shadow: 0 6px 16px rgba(64, 158, 255, 0.4);
  transform: translateY(-2px);
}

.register-options {
  text-align: center;
  font-size: 14px;
}

.has-account {
  color: #909399;
  margin-right: 8px;
}

.login-link {
  color: #409EFF;
  text-decoration: none;
  font-weight: 500;
}

.login-link:hover {
  color: #66b1ff;
  text-decoration: underline;
}

/* 响应式设计 */
@media (max-width: 1200px) {
  .left-section {
    flex: 0.8;
  }
  
  .right-section {
    width: 450px;
  }
}

@media (max-width: 768px) {
  .register-container {
    flex-direction: column;
  }
  
  .left-section {
    flex: none;
    height: 300px;
  }
  
  .right-section {
    width: 100%;
    flex: 1;
    padding: 20px;
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
  
  .register-form :deep(.el-form-item__label) {
    text-align: left;
  }
}
</style> 