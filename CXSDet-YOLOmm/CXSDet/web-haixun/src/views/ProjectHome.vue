<script setup lang="ts">
import { useWorkspaceStore } from '@/stores/workspace'
import { projectPath } from '@/utils/projectRoutes'
import WorkspaceSwitcher from '@/components/WorkspaceSwitcher.vue'
const workspace = useWorkspaceStore()
</script>
<template>
  <section class="project-home">
    <span class="eyebrow">海巡智检 · 项目工作台</span>
    <h1>从一个项目开始</h1>
    <p>选择左侧项目或历史会话，与海巡助手继续工作。数据集、模型、巡检与人工复核都在对应项目内。</p>
    <div class="new-project"><WorkspaceSwitcher /></div>
    <div class="project-cards"><RouterLink v-for="project in workspace.workspaces.filter(p => p.origin !== 'system')" :key="project.id" :to="projectPath(project.id)" class="project-card panel"><strong>{{ project.name }}</strong><span>{{ project.description || '进入项目对话' }}</span><small>{{ project.root_directory || '尚未绑定本机文件夹' }}</small></RouterLink></div>
  </section>
</template>
<style scoped>
.project-home { max-width: 900px; margin: 8vh auto; padding: 32px; }
.eyebrow, .project-home p { color: var(--text-secondary); }
.project-home h1 { margin: 16px 0; font-size: 30px; }
.project-home p { line-height: 1.8; }
.new-project { max-width: 220px; margin: 24px 0; }
.project-cards { display: grid; grid-template-columns: repeat(auto-fit,minmax(220px,1fr)); gap: 14px; }
.project-card { padding: 18px; display: flex; gap: 10px; flex-direction: column; text-decoration: none; color: var(--text); }
.project-card span, .project-card small { color: var(--text-secondary); overflow-wrap: anywhere; }
</style>
