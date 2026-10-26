<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount } from 'vue'
import { useWorkspaceStore } from '@/stores/workspace'

defineProps<{ workspaceName: string }>()

const workspace = useWorkspaceStore()
const open = ref(false)
const showCreate = ref(false)
const root = ref<HTMLElement | null>(null)

const newWs = ref({ name: '', description: '', requirements: '' })
const creating = ref(false)

function toggle() {
  open.value = !open.value
}

function onOutside(e: MouseEvent) {
  if (root.value && !root.value.contains(e.target as Node)) {
    open.value = false
  }
}

onMounted(() => window.addEventListener('mousedown', onOutside))
onBeforeUnmount(() => window.removeEventListener('mousedown', onOutside))

async function submitCreate() {
  if (!newWs.value.name.trim()) return
  creating.value = true
  try {
    await workspace.create({ ...newWs.value })
    newWs.value = { name: '', description: '', requirements: '' }
    showCreate.value = false
    open.value = false
  } finally {
    creating.value = false
  }
}
</script>

<template>
  <div ref="root" class="ws-switcher">
    <button class="ws-trigger" data-guide="workspace" aria-haspopup="listbox" @click="toggle">
      <span class="ws-icon" aria-hidden="true">
        <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.4">
          <rect x="1.5" y="1.5" width="6" height="6" rx="1" />
          <rect x="8.5" y="1.5" width="6" height="6" rx="1" />
          <rect x="1.5" y="8.5" width="6" height="6" rx="1" />
          <rect x="8.5" y="8.5" width="6" height="6" rx="1" />
        </svg>
      </span>
      <span class="ws-label">工作区</span>
      <span class="ws-name">{{ workspaceName }}</span>
      <span class="ws-caret" aria-hidden="true">▾</span>
    </button>

    <div v-if="open" class="ws-menu panel" role="listbox">
      <div class="ws-menu-title">个人工作区</div>
      <button
        v-for="ws in workspace.workspaces"
        :key="ws.id"
        class="ws-option"
        :class="{ current: ws.id === workspace.currentId }"
        role="option"
        :aria-selected="ws.id === workspace.currentId"
        @click="workspace.setCurrent(ws.id); open = false"
      >
        <span class="ws-opt-name">{{ ws.name }}</span>
        <span v-if="ws.id === workspace.currentId" class="ws-check" aria-hidden="true">✓</span>
      </button>
      <div class="ws-menu-divider"></div>
      <button class="ws-create" @click="showCreate = true; open = false">+ 新建工作区</button>
    </div>

    <div v-if="showCreate" class="overlay" @click.self="showCreate = false">
      <div class="dialog" role="dialog" aria-label="新建工作区">
        <div class="dialog-header">
          <h3 class="dialog-title">新建工作区</h3>
          <button class="btn btn-ghost btn-sm" aria-label="关闭" @click="showCreate = false">✕</button>
        </div>
        <div class="dialog-body">
          <div class="field">
            <label class="field-label" for="ws-name">名称</label>
            <input id="ws-name" v-model="newWs.name" class="input" placeholder="例如：北海风场项目" />
          </div>
          <div class="field">
            <label class="field-label" for="ws-desc">描述</label>
            <textarea id="ws-desc" v-model="newWs.description" class="textarea" rows="2" placeholder="项目说明"></textarea>
          </div>
          <div class="field">
            <label class="field-label" for="ws-req">项目要求</label>
            <textarea id="ws-req" v-model="newWs.requirements" class="textarea" rows="2" placeholder="例如：重点核查前缘侵蚀与表面裂纹"></textarea>
          </div>
        </div>
        <div class="dialog-footer">
          <button class="btn" @click="showCreate = false">取消</button>
          <button class="btn btn-primary" :disabled="!newWs.name.trim() || creating" @click="submitCreate">
            {{ creating ? '创建中…' : '创建' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.ws-switcher {
  position: relative;
}
.ws-trigger {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  height: 34px;
  padding: 0 10px;
  border: 1px solid var(--border-control);
  background: var(--panel-secondary);
  border-radius: var(--radius-sm);
  color: var(--text);
  cursor: pointer;
  font-size: 12px;
}
.ws-icon {
  color: var(--text-secondary);
  display: flex;
}
.ws-label {
  color: var(--text-secondary);
}
.ws-name {
  flex: 1;
  text-align: left;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: 500;
}
.ws-caret {
  color: var(--text-secondary);
  font-size: 10px;
}
.ws-menu {
  position: absolute;
  top: calc(100% + 6px);
  left: 0;
  right: 0;
  z-index: 100;
  box-shadow: var(--shadow-md);
  overflow: hidden;
}
.ws-menu-title {
  font-size: 11px;
  color: var(--text-secondary);
  padding: 10px 12px 6px;
}
.ws-option {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  height: 34px;
  padding: 0 12px;
  border: none;
  background: transparent;
  color: var(--text);
  font-size: 13px;
  cursor: pointer;
  text-align: left;
}
.ws-option:hover {
  background: var(--panel-secondary);
}
.ws-option.current {
  background: var(--bg-selected);
  font-weight: 500;
}
.ws-check {
  color: var(--accent);
}
.ws-menu-divider {
  height: 1px;
  background: var(--border);
  margin: 4px 0;
}
.ws-create {
  width: 100%;
  height: 34px;
  border: none;
  background: transparent;
  color: var(--accent);
  font-size: 13px;
  cursor: pointer;
  text-align: left;
  padding: 0 12px;
}
.ws-create:hover {
  background: var(--panel-secondary);
}
</style>
