<!-- MD5: b6af176195db398951285303136ec433 -->
<!-- 个人中心页面 -->
<!--
版权所有 © 2025 羊羊小栈 (GJQ) | 作者：羊羊小栈 | 时间：2025-09-06 23:44:27

本系统为原创作品，禁止二次销售！禁止将系统对应的视频、文档进行二次发布！
违者需立即停止侵权行为，并按照【羊羊小栈系统版权声明及保护条款】中规定数额进行赔偿，并承担相应法律责任。
-->

<template>
  <div class="profile">
    <el-row :gutter="20">
      <!-- 个人信息卡片 -->
      <el-col :span="8">
        <el-card class="profile-card" shadow="hover">
          <template #header>
            <div class="card-header">
              <span class="header-title">个人信息</span>
              <el-button
                type="primary"
                link
                @click="handleEdit"
              >
                <el-icon><Edit /></el-icon>
                编辑资料
              </el-button>
            </div>
          </template>
          <div class="profile-info">
            <div class="avatar-wrapper">
              <el-avatar
                :size="100"
                :src="userInfo?.avatarUrl"
              />
              <el-upload
                class="avatar-uploader"
                :auto-upload="false"
                :show-file-list="false"
                :on-change="handleAvatarChange"
                :before-upload="beforeAvatarUpload"
              >
                <el-button
                  type="primary"
                  link
                  class="change-avatar"
                >
                  <el-icon><Camera /></el-icon>
                  更换头像
                </el-button>
              </el-upload>
            </div>
            <div class="info-list">
              <div class="info-item">
                <span class="label">用户名：</span>
                <span class="value">{{ userInfo?.username }}</span>
              </div>
              <div class="info-item">
                <span class="label">真实姓名：</span>
                <span class="value">{{ userInfo?.realName }}</span>
              </div>
              <div class="info-item">
                <span class="label">手机号：</span>
                <span class="value">{{ userInfo?.phone }}</span>
              </div>
              <div class="info-item">
                <span class="label">邮箱：</span>
                <span class="value">{{ userInfo?.email }}</span>
              </div>
              <div class="info-item">
                <span class="label">角色：</span>
                <el-tag :type="userInfo?.role === 1 ? 'danger' : 'info'" size="small">
                  {{ userInfo?.role === 1 ? '管理员' : '普通用户' }}
                </el-tag>
              </div>
              <div class="info-item">
                <span class="label">状态：</span>
                <el-tag :type="userInfo?.status === 1 ? 'success' : 'danger'" size="small">
                  {{ userInfo?.status === 1 ? '正常' : '禁用' }}
                </el-tag>
              </div>
            </div>
          </div>
        </el-card>
      </el-col>

      <!-- 修改密码卡片 -->
      <el-col :span="8">
        <el-card class="password-card" shadow="hover">
          <template #header>
            <div class="card-header">
              <span class="header-title">修改密码</span>
              <el-icon><Lock /></el-icon>
            </div>
          </template>
          <el-form
            ref="passwordFormRef"
            :model="passwordForm"
            :rules="passwordRules"
            label-width="100px"
            status-icon
          >
            <el-form-item label="原密码" prop="oldPassword">
              <el-input
                v-model="passwordForm.oldPassword"
                type="password"
                show-password
                placeholder="请输入原密码"
              />
            </el-form-item>
            <el-form-item label="新密码" prop="newPassword">
              <el-input
                v-model="passwordForm.newPassword"
                type="password"
                show-password
                placeholder="请输入新密码"
              />
            </el-form-item>
            <el-form-item label="确认密码" prop="confirmPassword">
              <el-input
                v-model="passwordForm.confirmPassword"
                type="password"
                show-password
                placeholder="请确认新密码"
              />
            </el-form-item>
            <el-form-item>
              <el-button
                type="primary"
                :loading="passwordLoading"
                @click="handleChangePassword"
              >
                <el-icon><Check /></el-icon>
                修改密码
              </el-button>
            </el-form-item>
          </el-form>
        </el-card>
      </el-col>
    </el-row>

    <!-- 编辑个人信息对话框 -->
    <el-dialog
      v-model="editDialogVisible"
      title="编辑个人信息"
      width="500px"
      destroy-on-close
    >
      <el-form
        ref="editFormRef"
        :model="editForm"
        :rules="editRules"
        label-width="100px"
        status-icon
      >
        <el-form-item label="真实姓名" prop="realName">
          <el-input
            v-model="editForm.realName"
            placeholder="请输入真实姓名"
          />
        </el-form-item>
        <el-form-item label="手机号" prop="phone">
          <el-input
            v-model="editForm.phone"
            placeholder="请输入手机号"
          />
        </el-form-item>
        <el-form-item label="邮箱" prop="email">
          <el-input
            v-model="editForm.email"
            placeholder="请输入邮箱"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <span class="dialog-footer">
          <el-button @click="editDialogVisible = false">取消</el-button>
          <el-button
            type="primary"
            :loading="editLoading"
            @click="handleSaveEdit"
          >
            确定
          </el-button>
        </span>
      </template>
    </el-dialog>
  </div>
</template>

<!--
版权所有 © 2025 yangyangxiaozhan (GJQ)
-->

<script setup lang="ts">
import { ref, computed } from 'vue'
import type { FormInstance, UploadProps, FormItemRule } from 'element-plus'
import { ElMessage } from 'element-plus'
import { useUserStore } from '@/stores/user'
import { updateUser } from '@/api/user'
import { fileRequest } from '@/api/file_request'
import type { UpdateUserParams } from '@/types/user'
import { Edit, Camera, Lock, Check } from '@element-plus/icons-vue'

const userStore = useUserStore()

// 用户信息
const userInfo = computed(() => userStore.userInfo)

// 修改密码表单
const passwordForm = ref({
  oldPassword: '',
  newPassword: '',
  confirmPassword: ''
})

// 修改密码表单校验规则
const validatePass2 = (rule: any, value: string, callback: any) => {
  if (value === '') {
    callback(new Error('请再次输入密码'))
  } else if (value !== passwordForm.value.newPassword) {
    callback(new Error('两次输入密码不一致!'))
  } else {
    callback()
  }
}

const passwordRules = {
  oldPassword: [
    { required: true, message: '请输入原密码', trigger: 'blur' },
    { min: 6, max: 20, message: '密码长度应在6-20个字符之间', trigger: 'blur' }
  ],
  newPassword: [
    { required: true, message: '请输入新密码', trigger: 'blur' },
    { min: 6, max: 20, message: '密码长度应在6-20个字符之间', trigger: 'blur' }
  ],
  confirmPassword: [
    { required: true, message: '请再次输入密码', trigger: 'blur' },
    { validator: validatePass2, trigger: 'blur' }
  ]
}

const passwordLoading = ref(false)
const passwordFormRef = ref<FormInstance>()

// 处理修改密码
const handleChangePassword = async () => {
  if (!passwordFormRef.value) return

  try {
    await passwordFormRef.value.validate()
    passwordLoading.value = true

    await userStore.changePassword({
      id: userInfo.value?.id as number,
      oldPassword: passwordForm.value.oldPassword,
      newPassword: passwordForm.value.newPassword
    })

    ElMessage.success('密码修改成功')
    passwordForm.value = {
      oldPassword: '',
      newPassword: '',
      confirmPassword: ''
    }
  } catch (error) {
    console.error('修改密码失败:', error)
  } finally {
    passwordLoading.value = false
  }
}

// 编辑表单
const editForm = ref({
  realName: '',
  phone: '',
  email: ''
})

// 编辑表单校验规则
const editRules = {
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
    { pattern: /^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$/, message: '请输入正确的邮箱地址', trigger: 'blur' }
  ]
} satisfies Record<string, FormItemRule[]>

const editDialogVisible = ref(false)
const editLoading = ref(false)
const editFormRef = ref<FormInstance>()

// 处理编辑
const handleEdit = () => {
  editForm.value = {
    realName: userInfo.value?.realName || '',
    phone: userInfo.value?.phone || '',
    email: userInfo.value?.email || ''
  }
  editDialogVisible.value = true
}

// 处理保存编辑
const handleSaveEdit = async () => {
  if (!editFormRef.value) return

  try {
    await editFormRef.value.validate()
    editLoading.value = true

    await updateUser({
      id: userInfo.value?.id as number,
      realName: editForm.value.realName,
      phone: editForm.value.phone,
      email: editForm.value.email,
      role: userInfo.value?.role as number,
      status: userInfo.value?.status as number
    })

    // 更新本地用户信息
    const storedUserInfo = localStorage.getItem('userInfo')
    if (storedUserInfo) {
      const data = JSON.parse(storedUserInfo)
      data.userInfo = {
        ...data.userInfo,
        realName: editForm.value.realName,
        phone: editForm.value.phone,
        email: editForm.value.email
      }
      localStorage.setItem('userInfo', JSON.stringify(data))
      userStore.initUserInfo()
    }

    ElMessage.success('个人信息修改成功')
    editDialogVisible.value = false
  } catch (error) {
    console.error('修改个人信息失败:', error)
  } finally {
    editLoading.value = false
  }
}

// 处理头像上传前的验证
const beforeAvatarUpload: UploadProps['beforeUpload'] = (file) => {
  const isJPG = file.type === 'image/jpeg'
  const isPNG = file.type === 'image/png'
  const isLt2M = file.size / 1024 / 1024 < 2

  if (!isJPG && !isPNG) {
    ElMessage.error('头像只能是 JPG 或 PNG 格式!')
    return false
  }
  if (!isLt2M) {
    ElMessage.error('头像大小不能超过 2MB!')
    return false
  }
  return true
}

// 处理头像文件改变
const handleAvatarChange: UploadProps['onChange'] = async (uploadFile) => {
  if (!uploadFile.raw) return
  
  try {
    // 上传文件到文件服务
    const { bucket, objectKey } = await fileRequest.upload('avatars', uploadFile.raw)
    
    // 更新用户头像
    const updateParams: UpdateUserParams = {
      id: userInfo.value?.id as number,
      avatarBucket: bucket,
      avatarObjectKey: objectKey
    }
    await updateUser(updateParams)
    
    // 更新本地用户信息
    const storedUserInfo = localStorage.getItem('userInfo')
    if (storedUserInfo) {
      const data = JSON.parse(storedUserInfo)
      data.userInfo = {
        ...data.userInfo,
        avatarBucket: bucket,
        avatarObjectKey: objectKey,
        avatarUrl: fileRequest.getFileUrl(bucket, objectKey)
      }
      localStorage.setItem('userInfo', JSON.stringify(data))
      userStore.initUserInfo()
    }
    
    ElMessage.success('头像更新成功')
  } catch (error) {
    console.error('头像上传失败:', error)
    ElMessage.error('头像上传失败')
  }
}
</script>

<style scoped>
.profile {
  padding: 20px;
  min-height: calc(100vh - 140px);
  background-color: #f5f7fa;
}

.profile-card,
.password-card {
  height: 100%;
  transition: all 0.3s;
}

.profile-card:hover,
.password-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1);
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.header-title {
  font-size: 16px;
  font-weight: bold;
  display: flex;
  align-items: center;
  gap: 8px;
}

.profile-info {
  display: flex;
  flex-direction: column;
  align-items: center;
}

.avatar-wrapper {
  position: relative;
  margin-bottom: 30px;
  text-align: center;
}

.change-avatar {
  margin-top: 16px;
  display: flex;
  align-items: center;
  gap: 4px;
}

.info-list {
  width: 100%;
}

.info-item {
  display: flex;
  margin-bottom: 20px;
  padding: 0 20px;
  line-height: 24px;
}

.info-item .label {
  width: 80px;
  color: #606266;
  font-weight: 500;
}

.info-item .value {
  color: #303133;
  flex: 1;
}

.dialog-footer {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}

:deep(.el-form-item__content) {
  flex-wrap: nowrap;
}

:deep(.el-input__wrapper) {
  box-shadow: 0 0 0 1px #dcdfe6 inset;
}

:deep(.el-input__wrapper:hover) {
  box-shadow: 0 0 0 1px #409eff inset;
}

:deep(.el-form--label-top .el-form-item__label) {
  margin-bottom: 8px;
}

:deep(.el-button--primary) {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
}
</style> 