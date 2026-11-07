<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import AssistantPanel from '@/components/AssistantPanel.vue'
import NewProjectDialog from '@/components/NewProjectDialog.vue'
import { useWorkspaceStore } from '@/stores/workspace'

/**
 * 项目入口 = 项目主对话（与 v1.6 一致）。
 *
 * * 没有 projectId（`/projects`）：只给一句“从左侧选项目或新建”，不再铺一屏项目管理卡片；
 * * 有 projectId：直接进对话。已有会话就打开最近一条，没有会话就停在**空输入区**——
 *   发送时才会建会话（刷新不会凭空造出一堆会话）。
 * * 目录、要求、改名、删除这类低频动作在顶部“项目详情”抽屉里。
 */
const route = useRoute()
const router = useRouter()
const workspace = useWorkspaceStore()

const projectId = computed(() => String(route.params.projectId ?? ''))
const project = computed(() => (projectId.value ? workspace.projectById(projectId.value) : null))
const sessions = computed(() =>
  projectId.value ? (workspace.sessionsByProject[projectId.value] ?? []) : [],
)
const createOpen = ref(false)

watch(
  [project, sessions],
  () => {
    if (!projectId.value || route.query.details === '1') return
    const first = sessions.value[0]
    if (!first) return
    // replace：否则“项目页 → 会话页”在历史里堆一层，点返回又立刻被送回来。
    void router.replace({
      name: 'session',
      params: { projectId: projectId.value, sessionId: first.id },
    })
  },
  { immediate: true, flush: 'post' },
)
</script>

<template>
  <!-- 没有选中项目：只说明下一步，不重复列出左侧已有的项目 -->
  <section v-if="!projectId" class="welcome">
    <div class="welcome-inner">
      <div class="welcome-symbol" aria-hidden="true">✳</div>
      <h1>海巡智检</h1>
      <p class="welcome-lede">
        从左侧选择项目开始，或者新建一个。说明要检查什么，助手会帮你找文件、跑检测；
        最终标注仍由你确认。
      </p>
      <div class="welcome-actions">
        <button class="primary" type="button" @click="createOpen = true">新建项目</button>
        <RouterLink class="button ghost" :to="{ name: 'settings' }">检查模型连接</RouterLink>
      </div>
      <ul class="welcome-steps">
        <li>
          <span class="step-index">1</span>
          <span><strong>建项目</strong>给这一次巡检起个名字。</span>
        </li>
        <li>
          <span class="step-index">2</span>
          <span><strong>绑目录</strong>在“项目详情”里选服务器上已授权的文件夹。</span>
        </li>
        <li>
          <span class="step-index">3</span>
          <span><strong>开始对话</strong>说清楚要查哪些图、多少置信度。</span>
        </li>
      </ul>
    </div>
    <NewProjectDialog :open="createOpen" @close="createOpen = false" />
  </section>

  <section v-else-if="!project" class="welcome">
    <div class="welcome-inner">
      <div class="welcome-symbol" aria-hidden="true">✳</div>
      <h1>项目不可用</h1>
      <p class="welcome-lede">这个项目不存在，或不属于当前账号；也可能刚被移入回收站。</p>
      <div class="welcome-actions">
        <RouterLink class="button ghost" :to="{ name: 'projects' }">回到项目入口</RouterLink>
        <RouterLink class="button ghost" :to="{ name: 'trash' }">看看回收站</RouterLink>
      </div>
    </div>
  </section>

  <!-- 项目主对话：没有会话时也保留可发送的空输入区 -->
  <section v-else class="chat-page">
    <AssistantPanel variant="center" />
  </section>
</template>

<style scoped>
.welcome {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100%;
  padding: 48px 24px;
}

.welcome-inner {
  width: min(520px, 100%);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 14px;
  text-align: center;
}

.welcome-symbol {
  margin-bottom: 2px;
  font-size: 26px;
  line-height: 1;
  color: var(--text);
}

.welcome-inner h1 {
  margin: 0;
  font-size: 28px;
  font-weight: 600;
  letter-spacing: -0.5px;
}

.welcome-lede {
  margin: 0;
  max-width: 440px;
  color: var(--text-secondary);
  font-size: 13.5px;
  line-height: 1.85;
}

.welcome-actions {
  display: flex;
  gap: 8px;
  margin-top: 6px;
}

.welcome-steps {
  width: 100%;
  margin: 18px 0 0;
  padding: 14px 0 0;
  border-top: 1px solid var(--border);
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 2px;
  text-align: left;
}

.welcome-steps li {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 7px 0;
  color: var(--text-secondary);
  font-size: 13px;
  line-height: 1.7;
}

.welcome-steps strong {
  display: block;
  margin-bottom: 1px;
  color: var(--text);
  font-size: 13px;
  font-weight: 550;
}

.step-index {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  margin-top: 1px;
  border: 1px solid var(--border);
  border-radius: 50%;
  color: var(--text-secondary);
  font-size: 11px;
}
</style>
