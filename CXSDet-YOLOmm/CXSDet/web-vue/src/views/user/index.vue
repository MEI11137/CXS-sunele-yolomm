<!-- MD5: 58c3486135ab8ce63bf42b5751bd1772 -->
<!--
版权所有 © 2025羊羊小栈 （GJQ）

作者: 羊羊小栈
时间: 2025-09-06 23:44:27

本系统为原创作品,禁止二次销售!禁止将系统对应的视频、文档进行二次发布!
违者需立即停止侵权行为,并按照【羊羊小栈系统版权声明及保护条款】中规定数额进行赔偿,并承担相应法律责任。
-->

<template>
  <div class="user">
    <!-- 工具栏 -->
    <div class="toolbar">
      <div class="left">
        <el-button type="primary" @click="handleCreate">
          <el-icon><Plus /></el-icon>
          新增用户
        </el-button>
      </div>
    </div>

    <!-- 搜索表单 -->
    <el-form :model="queryParams" inline class="search-form">
      <el-form-item label="用户名">
        <el-input v-model="queryParams.username" placeholder="请输入用户名" clearable />
      </el-form-item>
      <el-form-item label="真实姓名">
        <el-input v-model="queryParams.realName" placeholder="请输入真实姓名" clearable />
      </el-form-item>
      <el-form-item label="用户角色">
        <el-select v-model="queryParams.role" placeholder="请选择用户角色" clearable class="form-select">
          <el-option :value="0" label="普通用户" />
          <el-option :value="1" label="管理员" />
        </el-select>
      </el-form-item>
      <el-form-item>
        <el-button type="primary" :loading="loading" @click="handleQuery">
          <el-icon><Search /></el-icon>
          查询
        </el-button>
        <el-button @click="handleReset">
          <el-icon><Refresh /></el-icon>
          重置
        </el-button>
      </el-form-item>
    </el-form>

    <!-- 数据表格 -->
    <el-table v-loading="loading" :data="list" border stripe>
      <el-table-column type="index" label="序号" width="60" align="center" />
      <el-table-column prop="username" label="用户名" min-width="120" show-overflow-tooltip />
      <el-table-column prop="realName" label="真实姓名" min-width="120" show-overflow-tooltip />
      <el-table-column prop="phone" label="手机号码" width="120" show-overflow-tooltip />
      <el-table-column prop="email" label="邮箱" min-width="180" show-overflow-tooltip />
      <el-table-column prop="role" label="用户角色" width="100" align="center">
        <template #default="{ row }">
          <el-tag :type="row.role === 1 ? 'danger' : 'info'">
            {{ row.role === 1 ? '管理员' : '普通用户' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="createTime" label="创建时间" width="180" align="center">
        <template #default="{ row }">
          {{ formatDateTime(row.createTime) }}
        </template>
      </el-table-column>
      <el-table-column label="操作" width="250" align="center" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="handleUpdate(row)">
            <el-icon><Edit /></el-icon>
            编辑
          </el-button>
          <el-button link type="primary" @click="handleResetPassword(row)">
            <el-icon><Key /></el-icon>
            重置密码
          </el-button>
          <el-button link type="danger" @click="handleDelete(row)">
            <el-icon><Delete /></el-icon>
            删除
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <!-- 分页 -->
    <pagination
      v-model:current="queryParams.current"
      v-model:size="queryParams.size"
      :total="total"
      @change="getList"
    />

    <!-- 新增/编辑对话框 -->
    <el-dialog
      v-model="dialogVisible"
      :title="dialogType === 'create' ? '新增用户' : '编辑用户'"
      width="500px"
      @close="resetForm"
    >
      <el-form ref="formRef" :model="form" :rules="rules" label-width="100px">
        <el-form-item label="用户名" prop="username">
          <el-input v-model="form.username" placeholder="请输入用户名" :disabled="dialogType === 'update'" />
        </el-form-item>
        <el-form-item label="真实姓名" prop="realName">
          <el-input v-model="form.realName" placeholder="请输入真实姓名" />
        </el-form-item>
        <el-form-item label="手机号码" prop="phone">
          <el-input v-model="form.phone" placeholder="请输入手机号码" />
        </el-form-item>
        <el-form-item label="邮箱" prop="email">
          <el-input v-model="form.email" placeholder="请输入邮箱" />
        </el-form-item>
        <el-form-item label="用户角色" prop="role">
          <el-select v-model="form.role" placeholder="请选择用户角色">
            <el-option :value="0" label="普通用户" />
            <el-option :value="1" label="管理员" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="dialogType === 'create'" label="密码" prop="password">
          <el-input v-model="form.password" type="password" show-password placeholder="请输入密码" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitLoading" @click="submitForm">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<!--
版权所有 © 2025 羊羊 小栈 (GJQ)
-->

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { FormInstance } from 'element-plus'
import type { UserInfo } from '@/types/user'
import {
  getUserList,
  addUser,
  updateUser,
  deleteUser,
  resetPassword
} from '@/api/user'
import { formatDateTime } from '@/utils/format'
import { validatePhone, validateEmail } from '@/utils/validate'
import Pagination from '@/components/Pagination/index.vue'

// 查询参数
const queryParams = reactive({
  current: 1,
  size: 10,
  username: undefined,
  realName: undefined,
  role: undefined,
})

// 数据列表
const list = ref<UserInfo[]>([])
const total = ref(0)
const loading = ref(false)

// 获取数据列表
const getList = async () => {
  loading.value = true
  try {
    const res = await getUserList(queryParams)
    list.value = res.records
    total.value = res.total
  } catch (error) {
    console.error(error)
  } finally {
    loading.value = false
  }
}

// 查询
const handleQuery = () => {
  queryParams.current = 1
  getList()
}

// 重置
const handleReset = () => {
  Object.assign(queryParams, {
    current: 1,
    size: 10,
    username: undefined,
    realName: undefined,
    role: undefined,
  })
  getList()
}

// 表单
const formRef = ref<FormInstance>()
const dialogVisible = ref(false)
const dialogType = ref<'create' | 'update'>('create')
const submitLoading = ref(false)

const form = reactive({
  id: 0,
  username: '',
  password: '',
  realName: '',
  phone: '',
  email: '',
  role: 0,
})

const rules = {
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    { min: 3, max: 20, message: '用户名长度应在3-20个字符之间', trigger: 'blur' }
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 6, max: 20, message: '密码长度应在6-20个字符之间', trigger: 'blur' }
  ],
  realName: [
    { required: true, message: '请输入真实姓名', trigger: 'blur' }
  ],
  phone: [
    { required: true, message: '请输入手机号码', trigger: 'blur' },
    { validator: (rule: any, value: string, callback: any) => {
      if (!validatePhone(value)) {
        callback(new Error('请输入正确的手机号码'))
      } else {
        callback()
      }
    }, trigger: 'blur' }
  ],
  email: [
    { required: true, message: '请输入邮箱', trigger: 'blur' },
    { validator: (rule: any, value: string, callback: any) => {
      if (!validateEmail(value)) {
        callback(new Error('请输入正确的邮箱'))
      } else {
        callback()
      }
    }, trigger: 'blur' }
  ],
  role: [
    { required: true, message: '请选择用户角色', trigger: 'change' }
  ]
}

// 新增
const handleCreate = () => {
  dialogType.value = 'create'
  dialogVisible.value = true
}

// 编辑
const handleUpdate = (row: any) => {
  dialogType.value = 'update'
  Object.assign(form, row)
  dialogVisible.value = true
}

// 重置密码
const handleResetPassword = (row: any) => {
  ElMessageBox.confirm('确认重置该用户的密码？', '警告', {
    type: 'warning',
  })
    .then(async () => {
      await resetPassword(row.id)
      ElMessage.success('重置成功')
    })
    .catch(() => {})
}

// 删除
const handleDelete = (row: any) => {
  ElMessageBox.confirm('确认删除该用户？删除后无法恢复！', '警告', {
    type: 'warning',
  })
    .then(async () => {
      await deleteUser(row.id)
      ElMessage.success('删除成功')
      getList()
    })
    .catch(() => {})
}

// 重置表单
const resetForm = () => {
  if (formRef.value) {
    formRef.value.resetFields()
  }
  Object.assign(form, {
    id: 0,
    username: '',
    password: '',
    realName: '',
    phone: '',
    email: '',
    role: 0,
  })
}

// 提交表单
const submitForm = async () => {
  if (!formRef.value) return
  await formRef.value.validate()
  submitLoading.value = true
  try {
    if (dialogType.value === 'create') {
      await addUser(form)
      ElMessage.success('新增成功')
    } else {
      await updateUser(form)
      ElMessage.success('更新成功')
    }
    dialogVisible.value = false
    getList()
  } catch (error) {
    console.error(error)
  } finally {
    submitLoading.value = false
  }
}

onMounted(() => {
  getList()
})
</script>

<style lang="scss" scoped>
.user {
  padding: 20px;

  .toolbar {
    margin-bottom: 20px;
    display: flex;
    justify-content: space-between;
    align-items: center;

    .left {
      display: flex;
      gap: 10px;
    }
  }

  .search-form {
    margin-bottom: 20px;
    
    :deep(.el-form-item) {
      margin-bottom: 18px;
      margin-right: 18px;
      
      .el-input {
        width: 200px;
      }
      
      .form-select {
        width: 200px;
      }
    }
  }
}
</style> 
