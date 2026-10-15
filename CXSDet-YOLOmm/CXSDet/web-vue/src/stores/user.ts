// MD5: d6947b3dcd241e12a5439b3f46c11557
/*
 * 版权所有 © 2025羊羊小栈 （GJQ）
 *
 * 作者: 羊羊小栈
 * 时间: 2025-09-06 23:44:27
 *
 * 本系统为原创作品,禁止二次销售!禁止将系统对应的视频、文档进行二次发布!
 * 违者需立即停止侵权行为,并按照【羊羊小栈系统版权声明及保护条款】中规定数额进行赔偿,并承担相应法律责任。
 */

import { defineStore } from 'pinia'
import { ref } from 'vue'
import type { UserInfo, LoginResponse } from '@/types/user'
import { login as userLogin, logout as userLogout, updatePassword as updateUserPassword, register as userRegister } from '@/api/user'
import { ElMessage } from 'element-plus'
import router from '@/router'

export const useUserStore = defineStore('user', () => {
  // 用户信息
  const userInfo = ref<UserInfo | null>(null)
  // token
  const token = ref<string | null>(null)

  // 从localStorage中恢复用户信息
  const initUserInfo = () => {
    const storedUserInfo = localStorage.getItem('userInfo')
    if (storedUserInfo) {
      try {
        const data = JSON.parse(storedUserInfo)
        userInfo.value = data.userInfo
        token.value = data.token
      } catch (error) {
        console.error('解析用户信息失败:', error)
        localStorage.removeItem('userInfo')
      }
    }
  }

  // 登录
  const login = async (username: string, password: string) => {
    try {
      const res = await userLogin(username, password)
      if (!res || !res.userInfo) {
        throw new Error('登录失败，请稍后重试')
      }
      userInfo.value = res.userInfo
      token.value = res.token
      // 存储用户信息和token到localStorage
      localStorage.setItem('userInfo', JSON.stringify({
        userInfo: res.userInfo,
        token: res.token
      }))
      ElMessage.success('登录成功')
      // 根据角色跳转到不同页面
      if (res.userInfo.role === 1) {
        router.push('/admin')
      } else {
        router.push('/dashboard')
      }
    } catch (error) {
      ElMessage.error('登录失败')
      throw error
    }
  }

  // 退出登录
  const logout = async (sendRequest: boolean = true) => {
    try {
      // 仅在需要时发送请求
      if (sendRequest) {
        try {
          await userLogout()
        } catch (error) {
          console.error('退出请求失败，但会继续清除本地登录状态')
        }
      }
      
      // 无论请求是否成功，都清除本地状态
      userInfo.value = null
      token.value = null
      // 清除localStorage中的用户信息
      localStorage.removeItem('userInfo')
      
      if (sendRequest) {
        ElMessage.success('退出成功')
      }
      
      router.push('/login')
    } catch (error) {
      // 即使出错也确保本地状态被清除
      userInfo.value = null
      token.value = null
      localStorage.removeItem('userInfo')
      router.push('/login')
      
      if (sendRequest) {
        ElMessage.error('退出失败')
        throw error
      }
    }
  }

  // 判断是否登录
  const isLoggedIn = () => {
    try {
      const storedUserInfo = localStorage.getItem('userInfo')
      if (!storedUserInfo) return false
      
      const data = JSON.parse(storedUserInfo)
      return !!(data.token && data.userInfo)
    } catch (error) {
      console.error('解析用户信息失败:', error)
      localStorage.removeItem('userInfo')
      return false
    }
  }

  // 判断是否是管理员
  const isAdmin = () => {
    try {
      const storedUserInfo = localStorage.getItem('userInfo')
      if (!storedUserInfo) return false
      
      const data = JSON.parse(storedUserInfo)
      return data.userInfo?.role === 1
    } catch (error) {
      console.error('解析用户信息失败:', error)
      localStorage.removeItem('userInfo')
      return false
    }
  }

  // 修改密码
  const changePassword = async (data: { id: number; oldPassword: string; newPassword: string }) => {
    await updateUserPassword(data)
  }

  // 注册
  const register = async (data: { username: string; password: string; realName: string; phone: string; email: string }) => {
    return userRegister(data)
  }

  return {
    userInfo,
    token,
    initUserInfo,
    login,
    logout,
    isLoggedIn,
    isAdmin,
    changePassword,
    register
  }
}) 