<script setup lang="ts">
defineProps<{ name: string; kind: 'project' | 'session'; busy: boolean; error: string }>()
const emit = defineEmits<{ confirm: []; cancel: [] }>()
</script>
<template>
  <Teleport to="body"><div class="overlay" @click.self="!busy && emit('cancel')"><section class="dialog delete-dialog" role="alertdialog" aria-modal="true" aria-labelledby="delete-title" aria-describedby="delete-impact">
    <header class="dialog-header"><h3 id="delete-title" class="dialog-title">确认删除{{ kind === 'project' ? '项目' : '会话' }}？</h3></header>
    <div class="dialog-body"><strong class="target">{{ name }}</strong><p id="delete-impact">{{ kind === 'project' ? '项目及其会话将移入回收站，可恢复。数据集、模型和本机文件夹会保留，不会删除磁盘文件。' : '这段会话将从项目历史中移入回收站，可恢复。项目文件和业务数据不受影响。' }}</p><p v-if="error" class="error" role="alert">{{ error }}</p></div>
    <footer class="dialog-footer"><button class="btn" :disabled="busy" @click="emit('cancel')">取消</button><button class="btn danger" :disabled="busy" @click="emit('confirm')">{{ busy ? '处理中…' : '确认移入回收站' }}</button></footer>
  </section></div></Teleport>
</template>
<style scoped>
.delete-dialog { width: min(460px,calc(100vw - 32px)); }
.target { display: block; overflow-wrap: anywhere; }
.dialog-body p { font-size: 13px; line-height: 1.8; color: var(--text-secondary); }
.danger { background: #b44239; color: white; border-color: #b44239; }
.dialog-body .error { color: #b44239; }
</style>
