<!-- MD5: 6bb641058e5190df46f894db66ac71de -->
<!--
版权所有©2025 羊羊小栈(GJQ) | 作者: 羊羊小栈 | 时间: 2025-09-06 23:44:27

本系统为原创作品,禁止二次销售!禁止将系统对应的视频、文档进行二次发布!
违者需立即停止侵权行为,并按照【羊羊小栈系统版权声明及保护条款】中规定数额进行赔偿,并承担相应法律责任。
-->

<template>
  <div class="dataset-management">
    <div class="header">
      <h2>数据集管理</h2>
      <el-button type="primary" @click="showUploadDialog = true">
        <el-icon><Upload /></el-icon>
        上传数据集
      </el-button>
    </div>

    <!-- 搜索和过滤 -->
    <div class="search-bar">
      <div class="search-controls">
        <el-input
          v-model="searchText"
          placeholder="搜索数据集名称或描述"
          style="width: 300px"
          clearable
          @keyup.enter="loadDatasets"
        >
          <template #prefix>
            <el-icon><Search /></el-icon>
          </template>
        </el-input>
        <el-button type="primary" @click="loadDatasets" :loading="loading">
          <el-icon><Search /></el-icon>
          搜索
        </el-button>
        <el-button @click="clearSearch" :disabled="loading">
          <el-icon><RefreshRight /></el-icon>
          清空
        </el-button>
      </div>
    </div>

    <!-- 数据集列表 -->
    <div class="dataset-list">
      <el-table 
        :data="datasets" 
        v-loading="loading"
        empty-text="暂无数据集"
      >
        <el-table-column prop="name" label="数据集名称" width="180" align="center">
          <template #default="{ row }">
            <div class="dataset-name">
              <el-icon><DataLine /></el-icon>
              <span>{{ row.name }}</span>
            </div>
          </template>
        </el-table-column>
        
        <el-table-column prop="description" label="描述" min-width="120" align="center">
          <template #default="{ row }">
            <span class="description">{{ row.description || '暂无描述' }}</span>
          </template>
        </el-table-column>

        <el-table-column label="模态" width="130" align="center">
          <template #default="{ row }">
            <el-tag :type="row.dataset_mode === 'multimodal' ? 'primary' : 'info'">
              {{ row.dataset_mode === 'multimodal' ? `RGB + ${row.secondary_modality || 'X'}` : 'RGB 单模态' }}
            </el-tag>
          </template>
        </el-table-column>
        
        <el-table-column label="数据集统计" min-width="80" align="center">
          <template #default="{ row }">
            <div class="structure-info">
              <div class="info-item">
                <span class="label">训练:</span>
                <span class="value">{{ row.structure_info.train_images }}张</span>
              </div>
              <div class="info-item">
                <span class="label">验证:</span>
                <span class="value">{{ row.structure_info.valid_images }}张</span>
              </div>
              <div class="info-item" v-if="row.structure_info.test_images > 0">
                <span class="label">测试:</span>
                <span class="value">{{ row.structure_info.test_images }}张</span>
              </div>
            </div>
          </template>
        </el-table-column>
        
        <el-table-column prop="file_size" label="文件大小" width="100" align="center">
          <template #default="{ row }">
            {{ formatFileSize(row.file_size) }}
          </template>
        </el-table-column>
        
        <el-table-column prop="status" label="状态" width="100" align="center">
          <template #default="{ row }">
            <el-tag :type="getStatusType(row.status)">
              {{ getStatusText(row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        
        <el-table-column label="操作" width="220" align="center" fixed="right">
          <template #default="{ row }">
            <el-button size="small" @click="viewDataset(row)">
              <el-icon><View /></el-icon>
              查看
            </el-button>
            <el-button size="small" @click="editDataset(row)">
              <el-icon><Edit /></el-icon>
              编辑
            </el-button>
            <el-dropdown @command="handleCommand">
              <el-button size="small" text>
                更多<el-icon><ArrowDown /></el-icon>
              </el-button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item :command="{ action: 'download', row }">
                    <el-icon><Download /></el-icon>
                    下载
                  </el-dropdown-item>
                  <el-dropdown-item :command="{ action: 'feedback', row }">
                    生成反馈训练版本
                  </el-dropdown-item>
                  <el-dropdown-item :command="{ action: 'delete', row }" divided>
                    <el-icon><Delete /></el-icon>
                    删除
                  </el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
          </template>
        </el-table-column>
      </el-table>

      <!-- 分页 -->
      <div class="pagination">
        <el-pagination
          v-model:current-page="currentPage"
          v-model:page-size="pageSize"
          :page-sizes="[10, 20, 50]"
          :total="total"
          layout="total, sizes, prev, pager, next, jumper"
          @current-change="loadDatasets"
          @size-change="loadDatasets"
        />
      </div>
    </div>

    <!-- 上传对话框 -->
    <el-dialog
      v-model="showUploadDialog"
      title="上传数据集"
      width="760px"
      @close="resetUploadForm"
    >
      <el-form :model="uploadForm" :rules="uploadRules" ref="uploadFormRef" label-width="100px">
        <el-form-item label="数据集名称" prop="name">
          <el-input v-model="uploadForm.name" placeholder="请输入数据集名称" />
        </el-form-item>
        
        <el-form-item label="描述" prop="description">
          <el-input
            v-model="uploadForm.description"
            type="textarea"
            :rows="3"
            placeholder="请输入数据集描述（可选）"
          />
        </el-form-item>

        <el-form-item label="数据集类型" prop="dataset_mode">
          <el-radio-group v-model="uploadForm.dataset_mode">
            <el-radio-button value="single">RGB 单模态</el-radio-button>
            <el-radio-button value="multimodal">RGB + X 多模态</el-radio-button>
          </el-radio-group>
        </el-form-item>

        <template v-if="uploadForm.dataset_mode === 'multimodal'">
          <el-form-item label="X 模态">
            <el-input
              v-model="uploadForm.secondary_modality"
              placeholder="例如 edge、ir、depth；ZIP 无 data.yaml 时必填"
            />
          </el-form-item>
          <el-form-item label="类别名称">
            <el-input
              v-model="uploadForm.class_names"
              type="textarea"
              :rows="2"
              placeholder="ZIP 无 data.yaml 时必填，用逗号分隔，例如 hotspot, crack"
            />
          </el-form-item>
        </template>
        
        <el-form-item label="数据集文件" prop="file">
          <el-upload
            ref="uploadRef"
            :auto-upload="false"
            :limit="1"
            accept=".zip"
            :on-change="handleFileChange"
            :on-remove="handleFileRemove"
          >
            <template #trigger>
              <el-button type="primary">选择ZIP文件</el-button>
            </template>
            <template #tip>
              <div class="el-upload__tip">
                只能上传ZIP格式文件，且需包含标准的YOLO数据集结构
              </div>
            </template>
          </el-upload>
        </el-form-item>
        
        <el-alert
          title="数据集格式要求"
          type="info"
          :closable="false"
          show-icon
        >
          <template #default>
            <div v-if="uploadForm.dataset_mode === 'single'" class="format-example">
              <strong>RGB 单模态固定结构</strong>
              <pre>data.yaml
images/train
images/val
images/test     # 可选
labels/train
labels/val
labels/test     # 可选</pre>
              <pre>path: C:/your/dataset
train: ./images/train
val: ./images/val
test: ./images/test
names: [crack, dirt]</pre>
            </div>
            <div v-else class="format-example">
              <strong>RGB + X 多模态固定结构（X 名称不限）</strong>
              <pre>data.yaml
images/train | val | test
images_X/train | val | test
labels/train | val | test</pre>
              <pre>path: C:/your/dataset
train: ./images/train
val: ./images/val
modalities:
  rgb: C:/your/dataset/images
  edge: C:/your/dataset/images_edge
modality_used: [rgb, edge]</pre>
              <div class="format-note">X 可为 edge、ir、depth 等；RGB 与 X 必须按相对路径和文件名一一配对。</div>
              <div class="format-note">兼容 modalities 与旧版 modality 写法；ZIP 无 data.yaml 时，系统会根据上方 X 模态和类别名称自动生成。</div>
            </div>
          </template>
        </el-alert>
      </el-form>
      
      <template #footer>
        <span class="dialog-footer">
          <el-button @click="showUploadDialog = false">取消</el-button>
          <el-button type="primary" :loading="uploading" @click="submitUpload">
            {{ uploading ? '上传中...' : '确认上传' }}
          </el-button>
        </span>
      </template>
    </el-dialog>

    <!-- 编辑对话框 -->
    <el-dialog
      v-model="showEditDialog"
      title="编辑数据集"
      width="500px"
    >
      <el-form :model="editForm" :rules="editRules" ref="editFormRef" label-width="100px">
        <el-form-item label="数据集名称" prop="name">
          <el-input v-model="editForm.name" placeholder="请输入数据集名称" />
        </el-form-item>
        
        <el-form-item label="描述" prop="description">
          <el-input
            v-model="editForm.description"
            type="textarea"
            :rows="3"
            placeholder="请输入数据集描述（可选）"
          />
        </el-form-item>
      </el-form>
      
      <template #footer>
        <span class="dialog-footer">
          <el-button @click="showEditDialog = false">取消</el-button>
          <el-button type="primary" :loading="updating" @click="submitEdit">
            {{ updating ? '保存中...' : '保存' }}
          </el-button>
        </span>
      </template>
    </el-dialog>

    <!-- 详情对话框 -->
    <el-dialog
      v-model="showDetailDialog"
      title="数据集详情"
      width="700px"
    >
      <div v-if="currentDataset" class="dataset-detail">
        <el-descriptions :column="2" border>
          <el-descriptions-item label="数据集名称">{{ currentDataset.name }}</el-descriptions-item>
          <el-descriptions-item label="文件大小">{{ formatFileSize(currentDataset.file_size) }}</el-descriptions-item>
          <el-descriptions-item label="状态">
            <el-tag :type="getStatusType(currentDataset.status)">
              {{ getStatusText(currentDataset.status) }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="上传时间">{{ formatDateTime(currentDataset.upload_time) }}</el-descriptions-item>
          <el-descriptions-item label="模态类型">
            {{ currentDataset.dataset_mode === 'multimodal' ? `RGB + ${currentDataset.secondary_modality || 'X'}` : 'RGB 单模态' }}
          </el-descriptions-item>
          <el-descriptions-item label="描述" :span="2">
            {{ currentDataset.description || '暂无描述' }}
          </el-descriptions-item>
        </el-descriptions>
        
        <h4>数据集统计信息</h4>
        <el-row :gutter="20">
          <el-col :span="8">
            <el-card class="stats-card">
              <div class="stats-item">
                <div class="stats-value">{{ currentDataset.structure_info.train_images }}</div>
                <div class="stats-label">训练图片</div>
              </div>
            </el-card>
          </el-col>
          <el-col :span="8">
            <el-card class="stats-card">
              <div class="stats-item">
                <div class="stats-value">{{ currentDataset.structure_info.valid_images }}</div>
                <div class="stats-label">验证图片</div>
              </div>
            </el-card>
          </el-col>
          <el-col :span="8">
            <el-card class="stats-card">
              <div class="stats-item">
                <div class="stats-value">{{ currentDataset.structure_info.test_images }}</div>
                <div class="stats-label">测试图片</div>
              </div>
            </el-card>
          </el-col>
        </el-row>
      </div>
    </el-dialog>
  </div>
</template>

<!--
版权所有 © 2025 yangyangxiaozhan (GJQ)
-->

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { FormInstance, UploadFile, UploadInstance } from 'element-plus'
import { 
  Upload, Search, DataLine, View, Edit, ArrowDown, 
  Download, Delete, RefreshRight
} from '@element-plus/icons-vue'
import { datasetApi, type Dataset } from '@/api/dataset'
import { formatFileSize, formatDateTime } from '@/utils/format'

// 响应式数据
const datasets = ref<Dataset[]>([])
const loading = ref(false)
const total = ref(0)
const currentPage = ref(1)
const pageSize = ref(10)
const searchText = ref('')

// 上传相关
const showUploadDialog = ref(false)
const uploading = ref(false)
const uploadFormRef = ref<FormInstance>()
const uploadRef = ref<UploadInstance>()
const uploadForm = reactive({
  name: '',
  description: '',
  dataset_mode: 'single' as 'single' | 'multimodal',
  secondary_modality: '',
  class_names: '',
  file: null as File | null
})

const uploadRules = {
  name: [
    { required: true, message: '请输入数据集名称', trigger: 'blur' },
    { min: 1, max: 50, message: '长度在 1 到 50 个字符', trigger: 'blur' }
  ],
  dataset_mode: [
    { required: true, message: '请选择数据集类型', trigger: 'change' }
  ],
  file: [
    { required: true, message: '请选择数据集文件', trigger: 'change' }
  ]
}

// 编辑相关
const showEditDialog = ref(false)
const updating = ref(false)
const editFormRef = ref<FormInstance>()
const editForm = reactive({
  id: '',
  name: '',
  description: ''
})

const editRules = {
  name: [
    { required: true, message: '请输入数据集名称', trigger: 'blur' },
    { min: 1, max: 50, message: '长度在 1 到 50 个字符', trigger: 'blur' }
  ]
}

// 详情相关
const showDetailDialog = ref(false)
const currentDataset = ref<Dataset | null>(null)

// 加载数据集列表
const loadDatasets = async () => {
  loading.value = true
  try {
    const response = await datasetApi.list({
      page: currentPage.value,
      page_size: pageSize.value,
      search: searchText.value
    })
    datasets.value = response.datasets
    total.value = response.total
  } catch (error) {
    ElMessage.error('加载数据集列表失败')
  } finally {
    loading.value = false
  }
}

// 清空搜索
const clearSearch = async () => {
  searchText.value = ''
  currentPage.value = 1
  await loadDatasets()
}

// 文件选择处理
const handleFileChange = (file: UploadFile) => {
  if (file.raw) {
    uploadForm.file = file.raw
  }
}

const handleFileRemove = () => {
  uploadForm.file = null
}

// 提交上传
const submitUpload = async () => {
  if (!uploadFormRef.value) return
  
  await uploadFormRef.value.validate(async (valid) => {
    if (valid && uploadForm.file) {
      uploading.value = true
      try {
        const formData = new FormData()
        formData.append('file', uploadForm.file)
        formData.append('name', uploadForm.name)
        formData.append('description', uploadForm.description)
        formData.append('dataset_mode', uploadForm.dataset_mode)
        if (uploadForm.dataset_mode === 'multimodal') {
          formData.append('secondary_modality', uploadForm.secondary_modality.trim())
          formData.append('class_names', uploadForm.class_names.trim())
        }
        
        await datasetApi.upload(formData)
        ElMessage.success('数据集上传成功')
        showUploadDialog.value = false
        resetUploadForm()
        loadDatasets()
      } catch (error: any) {
        ElMessage.error(error.response?.data?.msg || '数据集上传失败')
      } finally {
        uploading.value = false
      }
    }
  })
}

// 重置上传表单
const resetUploadForm = () => {
  uploadForm.name = ''
  uploadForm.description = ''
  uploadForm.dataset_mode = 'single'
  uploadForm.secondary_modality = ''
  uploadForm.class_names = ''
  uploadForm.file = null
  uploadFormRef.value?.resetFields()
  uploadRef.value?.clearFiles()
}

// 查看数据集详情
const viewDataset = async (dataset: any) => {
  try {
    const detail = await datasetApi.get(dataset.id)
    currentDataset.value = detail
    showDetailDialog.value = true
  } catch (error) {
    ElMessage.error('获取数据集详情失败')
  }
}

// 编辑数据集
const editDataset = (dataset: any) => {
  editForm.id = dataset.id
  editForm.name = dataset.name
  editForm.description = dataset.description || ''
  showEditDialog.value = true
}

// 提交编辑
const submitEdit = async () => {
  if (!editFormRef.value) return
  
  await editFormRef.value.validate(async (valid) => {
    if (valid) {
      updating.value = true
      try {
        await datasetApi.update(editForm.id, {
          name: editForm.name,
          description: editForm.description
        })
        ElMessage.success('数据集信息更新成功')
        showEditDialog.value = false
        loadDatasets()
      } catch (error) {
        ElMessage.error('数据集信息更新失败')
      } finally {
        updating.value = false
      }
    }
  })
}

// 下载数据集
const downloadDataset = async (dataset: any) => {
  try {
    const blob = await datasetApi.download(dataset.id)
    const url = window.URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `${dataset.name}.zip`
    document.body.appendChild(link)
    link.click()
    document.body.removeChild(link)
    window.URL.revokeObjectURL(url)
    ElMessage.success('下载开始')
  } catch (error) {
    ElMessage.error('下载失败')
  }
}

// 删除数据集
const deleteDataset = async (dataset: any) => {
  try {
    await ElMessageBox.confirm(
      `确定要删除数据集 "${dataset.name}" 吗？删除后无法恢复。`,
      '确认删除',
      {
        confirmButtonText: '确定',
        cancelButtonText: '取消',
        type: 'warning'
      }
    )
    
    await datasetApi.delete(dataset.id)
    ElMessage.success('数据集删除成功')
    loadDatasets()
  } catch (error) {
    if (error !== 'cancel') {
      ElMessage.error('数据集删除失败')
    }
  }
}

// 获取状态文本
const getStatusText = (status: number) => {
  const statusMap = {
    0: '待验证',
    1: '验证通过',
    2: '验证失败'
  }
  return statusMap[status as keyof typeof statusMap] || '未知状态'
}

// 获取状态类型
const getStatusType = (status: number): 'warning' | 'success' | 'danger' | 'info' => {
  const typeMap: Record<number, 'warning' | 'success' | 'danger'> = {
    0: 'warning',
    1: 'success',
    2: 'danger'
  }
  return typeMap[status] || 'info'
}


// 处理下拉菜单命令
const handleCommand = (command: { action: string; row: Dataset }) => {
  switch (command.action) {
    case 'download':
      downloadDataset(command.row)
      break
    case 'feedback':
      createFeedbackVersion(command.row)
      break
    case 'delete':
      deleteDataset(command.row)
      break
  }
}

const createFeedbackVersion = async (dataset: Dataset) => {
  try {
    const result = await datasetApi.createFeedbackVersion(dataset.id)
    ElMessage.success(`已生成 ${result.name}，新数据集 ID：${result.dataset_id}`)
    await loadDatasets()
  } catch (error: any) {
    ElMessage.error(error.response?.data?.msg || '生成反馈训练版本失败')
  }
}

onMounted(() => {
  loadDatasets()
})
</script>

<style lang="scss" scoped>
.dataset-management {
  padding: 20px;

  .header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 20px;
    
    h2 {
      margin: 0;
    }
  }

  .search-bar {
    margin-bottom: 20px;

    .search-controls {
      display: flex;
      align-items: center;
      gap: 12px;
    }
  }

  .dataset-list {
    .dataset-name {
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
    }

    .description {
      color: #666;
      font-size: 12px;
      line-height: 1.4;
      text-align: center;
    }

    .structure-info {
      display: flex;
      flex-direction: column;
      gap: 4px;
      text-align: center;

      .info-item {
        display: flex;
        justify-content: center;
        font-size: 12px;

        .label {
          color: #666;
          margin-right: 4px;
        }

        .value {
          font-weight: 500;
        }
      }
    }

    .pagination {
      margin-top: 20px;
      text-align: right;
    }

    /* 确保表格内容居中对齐 */
    :deep(.el-table .el-table__cell) {
      text-align: center;
    }

    :deep(.el-table .cell) {
      text-align: center;
    }
  }

  .format-requirements {
    margin: 8px 0;
    padding-left: 20px;
    color: #666;
    font-size: 12px;
    
    li {
      margin: 4px 0;
    }
  }

  .format-example {
    margin-top: 8px;

    pre {
      margin: 8px 0;
      padding: 10px 12px;
      overflow-x: auto;
      border-radius: 6px;
      background: #f5f7fa;
      color: #303133;
      font-size: 12px;
      line-height: 1.5;
    }

    .format-note {
      color: #606266;
      font-size: 12px;
    }
  }

  .dataset-detail {
    h4 {
      margin: 20px 0 10px 0;
    }

    .stats-card {
      text-align: center;
      margin-bottom: 10px;

      .stats-item {
        .stats-value {
          font-size: 24px;
          font-weight: bold;
          color: #409eff;
        }

        .stats-label {
          color: #666;
          font-size: 12px;
        }
      }
    }
  }
}
</style>
