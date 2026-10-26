<script setup lang="ts">
import { useGuideStore } from '@/stores/guide'

const guide = useGuideStore()

const version = 'V1（Agent 化改造）'
</script>

<template>
  <div class="page">
    <div class="page-header">
      <h1 class="page-title">资料与手册</h1>
    </div>

    <section class="section">
      <h2 class="section-title">当前版本说明</h2>
      <div class="panel manual-card">
        <h3 class="manual-title">海巡智检 · {{ version }}</h3>
        <p class="manual-desc">
          海上风电叶片巡检工作平台，面向影像巡检、检测计划确认、缺陷人工复核与项目资源管理。
        </p>
        <ul class="manual-list">
          <li><strong>工作区</strong>：影像、会话、任务、记录和统计按项目归属。</li>
          <li><strong>图片巡检</strong>：选择影像、指定检测模型，先核对计划再执行。</li>
          <li><strong>人工复核</strong>：在原图上修正类别、调整检测框，确认无缺陷后提交。</li>
          <li><strong>巡检助手</strong>：右侧全局对话栏，可发起巡检、核对计划、处理复核。</li>
        </ul>
      </div>
    </section>

    <section class="section">
      <h2 class="section-title">使用引导</h2>
      <div class="panel manual-card">
        <p class="manual-desc">首次进入会自动展示五步引导。如需再次查看：</p>
        <button class="btn" @click="guide.replay">重新播放引导</button>
      </div>
    </section>

    <section class="section">
      <h2 class="section-title">手册资料</h2>
      <div class="panel manual-card">
        <div class="empty">
          <div class="empty-title">暂无上传的手册</div>
          <div class="empty-desc">文档解析、索引及引用来源需接入真实解析服务（第三人）后可用。</div>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.page {
  padding: var(--content-pad);
  max-width: 800px;
  margin: 0 auto;
}
.page-header {
  margin-bottom: 20px;
}
.page-title {
  font-size: 24px;
  font-weight: 600;
  margin: 0;
}
.section {
  margin-bottom: 24px;
}
.section-title {
  font-size: 16px;
  font-weight: 600;
  margin: 0 0 12px;
}
.manual-card {
  padding: 20px;
}
.manual-title {
  font-size: 16px;
  font-weight: 600;
  margin: 0 0 12px;
}
.manual-desc {
  font-size: 14px;
  line-height: 1.7;
  color: var(--text-secondary);
  margin: 0 0 12px;
}
.manual-list {
  margin: 0;
  padding-left: 18px;
  font-size: 13px;
  line-height: 1.8;
  color: var(--text);
}
</style>
