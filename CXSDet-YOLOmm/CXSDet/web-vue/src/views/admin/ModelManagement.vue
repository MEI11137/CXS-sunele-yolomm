<!-- MD5: 934232351f05d407979e7fc08d3ddf2c -->
<!--
版权所有 © 2025 羊羊小栈 (GJQ)

作者: 羊羊小栈
时间: 2025-09-06 23:44:27

本系统为原创作品，禁止二次销售！禁止将系统对应的视频、文档进行二次发布！
违者需立即停止侵权行为，并按照【羊羊小栈系统版权声明及保护条款】中规定数额进行赔偿，并承担相应法律责任。
-->

<template>
  <div class="model-management">
    <div class="header">
      <h2>模型管理</h2>
      <div class="header-actions">
        <el-button @click="handleRefresh" :loading="refreshLoading" :disabled="loading">
          <el-icon><Refresh /></el-icon>
          刷新列表
        </el-button>
        <el-button type="success" @click="showImportDialog = true">
          <el-icon><Upload /></el-icon>
          导入模型权重
        </el-button>
        <el-button type="primary" @click="showCreateDialog = true">
          <el-icon><Plus /></el-icon>
          创建模型
        </el-button>
      </div>
    </div>

    <div class="search-bar">
      <div class="search-controls">
        <el-input
          v-model="searchQuery"
          placeholder="搜索模型名称或描述"
          style="width: 300px"
          @keyup.enter="performSearch"
          clearable
        >
          <template #prefix>
            <el-icon><Search /></el-icon>
          </template>
        </el-input>
        <el-button type="primary" @click="performSearch" :loading="loading">
          <el-icon><Search /></el-icon>
          搜索
        </el-button>
        <el-button @click="clearSearch" :disabled="loading">
          <el-icon><RefreshRight /></el-icon>
          清空
        </el-button>
      </div>
    </div>

    <el-table
      :data="modelList"
      v-loading="loading"
      style="width: 100%"
      border
      table-layout="fixed"
    >
      <el-table-column prop="name" label="模型名称" width="150" align="center" />
      <el-table-column prop="description" label="描述" min-width="200" align="center" show-overflow-tooltip />
      <el-table-column prop="dataset_name" label="训练数据集" width="120" align="center" />
      <el-table-column label="模型模态" width="120" align="center">
        <template #default="scope">
          <el-tag :type="scope.row.model_mode === 'multimodal' ? 'primary' : 'info'">
            {{ scope.row.model_mode === 'multimodal' ? `RGB + ${scope.row.secondary_modality || 'X'}` : 'RGB' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="版本" width="90" align="center">
        <template #default="scope">v{{ scope.row.version || 1 }}</template>
      </el-table-column>
      <el-table-column prop="base_model" label="基础模型" width="120" align="center" />
      <el-table-column label="训练参数" width="120" align="center">
        <template #default="scope">
          <div v-if="parseTrainResult(scope.row.train_result).source === 'imported'" class="training-params">
            <el-tag type="info" size="small">外部导入</el-tag>
          </div>
          <div v-else class="training-params">
            <div>迭代: {{ scope.row.epochs }}</div>
            <div>批次: {{ scope.row.batch_size }}</div>
          </div>
        </template>
      </el-table-column>
      <el-table-column prop="status" label="状态" width="100" align="center">
        <template #default="scope">
          <el-tag :type="getStatusType(scope.row.status)">
            {{ getStatusText(scope.row.status) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="训练结果" min-width="180" align="center">
        <template #default="scope">
          <div v-if="(scope.row.status === 1 || scope.row.status === 3) && scope.row.train_result" class="training-results">
            <div v-if="parseTrainResult(scope.row.train_result).source === 'imported'">
              <el-text type="success">权重已验证</el-text>
            </div>
            <div v-else-if="parseTrainResult(scope.row.train_result).results_dict">
              <div>精确度: {{ formatMetricValue(parseTrainResult(scope.row.train_result).results_dict['metrics/precision(B)']) }}</div>
              <div>召回率: {{ formatMetricValue(parseTrainResult(scope.row.train_result).results_dict['metrics/recall(B)']) }}</div>
              <div>mAP@0.5: {{ formatMetricValue(parseTrainResult(scope.row.train_result).results_dict['metrics/mAP50(B)']) }}</div>
              <div>适应度: {{ formatMetricValue(parseTrainResult(scope.row.train_result).results_dict['fitness']) }}</div>
            </div>
            <div v-else>
              <div>最终适应度: {{ parseTrainResult(scope.row.train_result).best_fitness?.toFixed(4) || 'N/A' }}</div>
            </div>
          </div>
          <div v-else-if="scope.row.status === 2 && scope.row.train_result">
            <el-tooltip :content="parseTrainResult(scope.row.train_result).error || '训练失败'" placement="top">
              <el-text type="danger">查看错误</el-text>
            </el-tooltip>
          </div>
          <div v-else>-</div>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="290" align="center" fixed="right">
        <template #default="scope">
          <el-button size="small" @click="viewModel(scope.row)" link>查看</el-button>
          <el-button size="small" @click="editModel(scope.row)" link>编辑</el-button>
          <el-button
            v-if="scope.row.status === 1 || scope.row.status === 3"
            size="small"
            type="primary"
            @click="openIncremental(scope.row)"
            link
          >增量训练</el-button>
          <el-button
            v-if="scope.row.parent_model_id && (scope.row.status === 1 || scope.row.status === 3)"
            size="small"
            @click="showComparison(scope.row)"
            link
          >父子对比</el-button>
          <el-button 
            v-if="scope.row.status === 1" 
            size="small" 
            type="success" 
            @click="publishModel(scope.row.id)"
            link
          >
            发布
          </el-button>
          <el-button 
            v-if="scope.row.status === 3" 
            size="small" 
            type="warning" 
            @click="unpublishModel(scope.row.id)"
            link
          >
            取消发布
          </el-button>
          <el-button 
            size="small" 
            type="danger" 
            @click="deleteModel(scope.row.id, scope.row.name)"
            link
          >
            删除
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-pagination
      v-model:current-page="currentPage"
      v-model:page-size="pageSize"
      :page-sizes="[10, 20, 50, 100]"
      :total="total"
      layout="total, sizes, prev, pager, next, jumper"
      @size-change="handleSizeChange"
      @current-change="handleCurrentChange"
      style="margin-top: 20px; text-align: right"
    />

    <!-- 导入模型权重对话框 -->
    <el-dialog v-model="showImportDialog" title="导入模型权重" width="600px" @close="resetImportForm">
      <el-alert
        title="请选择权重对应的数据集。系统会自动继承该数据集的 RGB 或 RGB+X 模态配置，并在导入前验证权重能否被当前检测运行时加载。"
        type="info"
        :closable="false"
        show-icon
        style="margin-bottom: 18px"
      />
      <el-form
        ref="importFormRef"
        :model="importForm"
        :rules="importRules"
        label-width="120px"
      >
        <el-form-item label="模型名称" prop="name">
          <el-input v-model="importForm.name" placeholder="请输入模型名称" />
        </el-form-item>
        <el-form-item label="关联数据集" prop="dataset_id">
          <el-select v-model="importForm.dataset_id" placeholder="选择权重训练时使用的数据集" style="width: 100%">
            <el-option
              v-for="dataset in datasetOptions"
              :key="dataset.id"
              :label="formatDatasetOption(dataset)"
              :value="dataset.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="权重文件" required>
          <el-upload
            ref="importUploadRef"
            class="weight-upload"
            action="#"
            accept=".pt"
            :auto-upload="false"
            :limit="1"
            :on-change="handleWeightChange"
            :on-remove="handleWeightRemove"
          >
            <el-button type="primary" plain>选择 .pt 权重</el-button>
            <template #tip>
              <div class="el-upload__tip">仅支持由当前 YOLO/YOLOMM 运行时可加载的 .pt 文件</div>
            </template>
          </el-upload>
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="importForm.description" type="textarea" :rows="3" placeholder="可选：填写训练来源、版本或用途" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showImportDialog = false">取消</el-button>
        <el-button type="primary" :loading="importLoading" @click="handleImport">
          验证并导入
        </el-button>
      </template>
    </el-dialog>

    <!-- 创建模型对话框 -->
    <el-dialog v-model="showCreateDialog" title="创建模型" width="600px" @close="resetCreateForm">
      <el-form
        ref="createFormRef"
        :model="createForm"
        :rules="createRules"
        label-width="120px"
      >
        <el-form-item label="模型名称" prop="name">
          <el-input v-model="createForm.name" placeholder="请输入模型名称" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input
            v-model="createForm.description"
            type="textarea"
            placeholder="请输入模型描述"
            :rows="3"
          />
        </el-form-item>
        <el-form-item label="训练数据集" prop="dataset_id">
          <el-select v-model="createForm.dataset_id" placeholder="选择数据集" style="width: 100%">
            <el-option
              v-for="dataset in datasetOptions"
              :key="dataset.id"
              :label="dataset.name"
              :value="dataset.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="基础模型" prop="base_model">
          <el-select v-model="createForm.base_model" placeholder="选择基础模型" style="width: 100%">
            <el-option
              v-for="model in baseModelOptions"
              :key="model"
              :label="model"
              :value="model"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="迭代次数" prop="epochs">
          <el-input-number v-model="createForm.epochs" :min="1" :max="1000" :step="1" />
        </el-form-item>
        <el-form-item label="批次大小" prop="batch_size">
          <el-input-number v-model="createForm.batch_size" :min="1" :max="128" :step="1" />
        </el-form-item>
      </el-form>
      <template #footer>
        <span class="dialog-footer">
          <el-button @click="showCreateDialog = false">取消</el-button>
          <el-button type="primary" @click="handleCreate" :loading="createLoading">
            创建并开始训练
          </el-button>
        </span>
      </template>
    </el-dialog>

    <el-dialog v-model="showIncrementalDialog" title="从当前权重增量训练" width="560px">
      <el-alert
        title="该操作会创建独立子版本；父模型和现有已发布权重不会被覆盖。训练完成后仍需人工对比并发布。"
        type="info"
        :closable="false"
        show-icon
      />
      <el-form :model="incrementalForm" label-width="140px" style="margin-top: 18px">
        <el-form-item label="反馈数据集 ID">
          <el-input v-model="incrementalForm.feedback_dataset_id" placeholder="填写已导出的反馈数据集版本 ID" />
        </el-form-item>
        <el-form-item label="训练轮数">
          <el-input-number v-model="incrementalForm.epochs" :min="1" :max="1000" />
        </el-form-item>
        <el-form-item label="批大小">
          <el-input-number v-model="incrementalForm.batch_size" :min="1" :max="128" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showIncrementalDialog = false">取消</el-button>
        <el-button type="primary" :loading="incrementalLoading" @click="startIncremental">创建子版本并训练</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="showComparisonDialog" title="父子模型指标对比" width="720px">
      <el-table v-if="comparison" :data="comparisonRows" border>
        <el-table-column prop="name" label="指标" />
        <el-table-column prop="parent" label="父模型" />
        <el-table-column prop="child" label="子模型" />
        <el-table-column prop="delta" label="变化" />
        <el-table-column prop="status" label="数据状态" />
      </el-table>
      <el-alert
        v-if="comparison?.per_class.status === 'unknown'"
        title="逐类别指标未由训练器提供，保持 unknown，不会按 0 计算。"
        type="warning"
        :closable="false"
        style="margin-top: 12px"
      />
    </el-dialog>

    <!-- 编辑模型对话框 -->
    <el-dialog v-model="showEditDialog" title="编辑模型" width="500px" @close="resetEditForm">
      <el-form
        ref="editFormRef"
        :model="editForm"
        :rules="editRules"
        label-width="120px"
      >
        <el-form-item label="模型名称" prop="name">
          <el-input v-model="editForm.name" placeholder="请输入模型名称" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input
            v-model="editForm.description"
            type="textarea"
            placeholder="请输入模型描述"
            :rows="3"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <span class="dialog-footer">
          <el-button @click="showEditDialog = false">取消</el-button>
          <el-button type="primary" @click="handleEdit" :loading="editLoading">
            保存
          </el-button>
        </span>
      </template>
    </el-dialog>

    <!-- 查看模型对话框 -->
    <el-dialog 
      v-model="showViewDialog" 
      title="模型详情" 
      width="1400px"
      :close-on-click-modal="false"
      class="model-detail-dialog"
    >
      <div v-if="currentModel" class="model-detail">
        <!-- 模型基本信息 -->
        <div class="detail-section">
          <div class="section-header">
            <el-icon class="section-icon"><InfoFilled /></el-icon>
            <h3 class="section-title">基本信息</h3>
          </div>
          <div class="info-grid">
            <div class="info-item">
              <div class="info-label">模型名称</div>
              <div class="info-value">{{ currentModel.name }}</div>
            </div>
            <div class="info-item">
              <div class="info-label">状态</div>
              <div class="info-value">
                <el-tag :type="getStatusType(currentModel.status)" size="large">
                  {{ getStatusText(currentModel.status) }}
                </el-tag>
              </div>
            </div>
            <div class="info-item">
              <div class="info-label">训练数据集</div>
              <div class="info-value">{{ currentModel.dataset_name }}</div>
            </div>
            <div class="info-item">
              <div class="info-label">基础模型</div>
              <div class="info-value">{{ currentModel.base_model }}</div>
            </div>
            <div class="info-item">
              <div class="info-label">模态</div>
              <div class="info-value">
                {{ currentModel.model_mode === 'multimodal' ? `RGB + ${currentModel.secondary_modality || 'X'}` : 'RGB 单模态' }}
              </div>
            </div>
            <div class="info-item">
              <div class="info-label">模型版本</div>
              <div class="info-value">v{{ currentModel.version || 1 }}</div>
            </div>
            <div class="info-item">
              <div class="info-label">父模型</div>
              <div class="info-value">{{ currentModel.parent_model_id || '-' }}</div>
            </div>
            <div class="info-item">
              <div class="info-label">迭代次数</div>
              <div class="info-value">{{ currentModel.epochs }}</div>
            </div>
            <div class="info-item">
              <div class="info-label">批次大小</div>
              <div class="info-value">{{ currentModel.batch_size }}</div>
            </div>
            <div class="info-item">
              <div class="info-label">创建时间</div>
              <div class="info-value">{{ formatDateTime(currentModel.create_time) }}</div>
            </div>
            <div class="info-item">
              <div class="info-label">开始训练时间</div>
              <div class="info-value">{{ currentModel.start_time ? formatDateTime(currentModel.start_time) : '-' }}</div>
            </div>
            <div class="info-item">
              <div class="info-label">完成时间</div>
              <div class="info-value">{{ currentModel.complete_time ? formatDateTime(currentModel.complete_time) : '-' }}</div>
            </div>
          </div>
          <div v-if="currentModel.description" class="description-section">
            <div class="info-label">描述</div>
            <div class="info-value description-text">{{ currentModel.description }}</div>
          </div>
        </div>

        <!-- 训练结果 -->
        <div v-if="currentModel.train_result" class="detail-section">
          <div class="section-header">
            <el-icon class="section-icon"><Trophy /></el-icon>
            <h3 class="section-title">训练结果</h3>
          </div>
          
          <div v-if="currentModel.status === 1 || currentModel.status === 3">
            <div v-if="parseTrainResult(currentModel.train_result).results_dict">
              <!-- 核心指标卡片 -->
              <div class="metrics-grid">
                <div class="metric-card precision">
                  <div class="metric-icon">🎯</div>
                  <div class="metric-content">
                    <div class="metric-label">精确度</div>
                    <div class="metric-value">{{ formatMetricValue(parseTrainResult(currentModel.train_result).results_dict['metrics/precision(B)'], 4) }}</div>
                    <div class="metric-desc">Precision</div>
                  </div>
                </div>
                <div class="metric-card recall">
                  <div class="metric-icon">🔍</div>
                  <div class="metric-content">
                    <div class="metric-label">召回率</div>
                    <div class="metric-value">{{ formatMetricValue(parseTrainResult(currentModel.train_result).results_dict['metrics/recall(B)'], 4) }}</div>
                    <div class="metric-desc">Recall</div>
                  </div>
                </div>
                <div class="metric-card map50">
                  <div class="metric-icon">📊</div>
                  <div class="metric-content">
                    <div class="metric-label">mAP@0.5</div>
                    <div class="metric-value">{{ formatMetricValue(parseTrainResult(currentModel.train_result).results_dict['metrics/mAP50(B)'], 4) }}</div>
                    <div class="metric-desc">Mean Average Precision</div>
                  </div>
                </div>
                <div class="metric-card map50-95">
                  <div class="metric-icon">📈</div>
                  <div class="metric-content">
                    <div class="metric-label">mAP@0.5:0.95</div>
                    <div class="metric-value">{{ formatMetricValue(parseTrainResult(currentModel.train_result).results_dict['metrics/mAP50-95(B)'], 4) }}</div>
                    <div class="metric-desc">mAP (IoU 0.5-0.95)</div>
                  </div>
                </div>
                <div class="metric-card fitness">
                  <div class="metric-icon">⭐</div>
                  <div class="metric-content">
                    <div class="metric-label">适应度</div>
                    <div class="metric-value highlight">{{ formatMetricValue(parseTrainResult(currentModel.train_result).results_dict['fitness'], 4) }}</div>
                    <div class="metric-desc">Fitness Score</div>
                  </div>
                </div>
              </div>
            </div>
            <div v-else class="simple-metrics">
              <div class="simple-metric-item">
                <span class="simple-label">最终适应度:</span>
                <span class="simple-value">{{ parseTrainResult(currentModel.train_result).best_fitness?.toFixed(6) || 'N/A' }}</span>
              </div>
              <div class="simple-metric-item">
                <span class="simple-label">完成轮数:</span>
                <span class="simple-value">{{ parseTrainResult(currentModel.train_result).final_epoch || 'N/A' }}</span>
              </div>
            </div>
            
            <!-- 训练指标图表 -->
            <div v-if="parseTrainResult(currentModel.train_result).training_metrics && parseTrainResult(currentModel.train_result).training_metrics.length > 0" class="chart-section">
              <div class="section-header">
                <el-icon class="section-icon"><DataAnalysis /></el-icon>
                <h3 class="section-title">训练过程指标</h3>
              </div>
              <div class="chart-container">
                <TrainingChart 
                  :data="parseTrainResult(currentModel.train_result).training_metrics" 
                  title=""
                  height="400px"
                />
              </div>
            </div>
          </div>
          <div v-else-if="currentModel.status === 2" class="error-section">
            <el-alert
              title="训练失败"
              type="error"
              :description="parseTrainResult(currentModel.train_result).error || '未知错误'"
              show-icon
              :closable="false"
            />
          </div>
        </div>

        <!-- 模型文件下载 -->
        <div v-if="currentModel.model_file_url && (currentModel.status === 1 || currentModel.status === 3)" class="detail-section">
          <div class="section-header">
            <el-icon class="section-icon"><Download /></el-icon>
            <h3 class="section-title">模型文件</h3>
          </div>
          <div class="download-section">
            <div class="download-info">
              <p>训练完成的模型文件已准备就绪，您可以下载用于部署或进一步分析。</p>
            </div>
            <el-button type="primary" size="large" @click="downloadModel(currentModel.model_file_url)">
              <el-icon><Download /></el-icon>
              下载模型文件 (.pt)
            </el-button>
          </div>
        </div>
      </div>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, onMounted, reactive, watch, nextTick } from 'vue'
import { ElMessage, ElMessageBox, ElForm, type UploadFile, type UploadInstance } from 'element-plus'
import { Plus, Search, Download, Refresh, InfoFilled, Trophy, DataAnalysis, RefreshRight, Upload } from '@element-plus/icons-vue'
import {
  modelApi,
  type Model,
  type CreateModelRequest,
  type ModelComparison,
  type UpdateModelRequest
} from '@/api/model'
import { datasetApi, type Dataset } from '@/api/dataset'
import TrainingChart from '@/components/TrainingChart.vue'

// 响应式数据
const loading = ref(false)
const refreshLoading = ref(false)
const modelList = ref<Model[]>([])
const total = ref(0)
const currentPage = ref(1)
const pageSize = ref(10)
const searchQuery = ref('')

// 对话框显示状态
const showCreateDialog = ref(false)
const showImportDialog = ref(false)
const showEditDialog = ref(false)
const showViewDialog = ref(false)
const showIncrementalDialog = ref(false)
const showComparisonDialog = ref(false)
const incrementalLoading = ref(false)
const incrementalParent = ref<Model | null>(null)
const comparison = ref<ModelComparison | null>(null)
const incrementalForm = reactive({ feedback_dataset_id: '', epochs: 10, batch_size: 4 })

const importForm = reactive({
  name: '',
  description: '',
  dataset_id: ''
})
const importWeightFile = ref<File | null>(null)

// 表单数据
const createForm = reactive<CreateModelRequest>({
  name: '',
  description: '',
  dataset_id: '',
  base_model: 'yolo11n.pt',
  epochs: 50,
  batch_size: 8
})

const editForm = reactive<UpdateModelRequest>({
  name: '',
  description: ''
})

// 表单引用
const createFormRef = ref<InstanceType<typeof ElForm>>()
const importFormRef = ref<InstanceType<typeof ElForm>>()
const importUploadRef = ref<UploadInstance>()
const editFormRef = ref<InstanceType<typeof ElForm>>()

// 加载状态
const createLoading = ref(false)
const importLoading = ref(false)
const editLoading = ref(false)

// 当前操作的模型
const currentModel = ref<Model | null>(null)
const currentEditId = ref('')

const comparisonRows = computed(() => {
  if (!comparison.value) return []
  return Object.entries(comparison.value.metrics).map(([name, metric]) => ({
    name,
    parent: metric.parent ?? 'unknown',
    child: metric.child ?? 'unknown',
    delta: metric.delta === null ? 'unknown' : metric.delta.toFixed(6),
    status: metric.status
  }))
})

const openIncremental = (model: any) => {
  incrementalParent.value = model
  incrementalForm.feedback_dataset_id = ''
  incrementalForm.epochs = Math.min(model.epochs || 10, 50)
  incrementalForm.batch_size = model.batch_size || 4
  showIncrementalDialog.value = true
}

const startIncremental = async () => {
  if (!incrementalParent.value || !incrementalForm.feedback_dataset_id.trim()) {
    ElMessage.warning('请输入反馈数据集 ID')
    return
  }
  try {
    incrementalLoading.value = true
    await modelApi.startIncremental(incrementalParent.value.id, incrementalForm)
    ElMessage.success('子模型已创建，增量训练开始')
    showIncrementalDialog.value = false
    fetchModels()
  } catch (error: any) {
    ElMessage.error(error.response?.data?.msg || '增量训练启动失败')
  } finally {
    incrementalLoading.value = false
  }
}

const showComparison = async (model: any) => {
  if (!model.parent_model_id) return
  try {
    comparison.value = await modelApi.compare(model.parent_model_id, model.id)
    showComparisonDialog.value = true
  } catch (error: any) {
    ElMessage.error(error.response?.data?.msg || '指标对比失败')
  }
}

// 数据集选项
const datasetOptions = ref<Dataset[]>([])
const baseModelOptions = ref<string[]>([])

// 表单验证规则
const createRules = {
  name: [
    { required: true, message: '请输入模型名称', trigger: 'blur' }
  ],
  dataset_id: [
    { required: true, message: '请选择训练数据集', trigger: 'change' }
  ],
  base_model: [
    { required: true, message: '请选择基础模型', trigger: 'change' }
  ],
  epochs: [
    { required: true, message: '请输入迭代次数', trigger: 'blur' }
  ],
  batch_size: [
    { required: true, message: '请输入批次大小', trigger: 'blur' }
  ]
}

const importRules = {
  name: [
    { required: true, message: '请输入模型名称', trigger: 'blur' }
  ],
  dataset_id: [
    { required: true, message: '请选择关联数据集', trigger: 'change' }
  ]
}

const editRules = {
  name: [
    { required: true, message: '请输入模型名称', trigger: 'blur' }
  ]
}

// 获取模型列表
const fetchModels = async () => {
  try {
    loading.value = true
    const response = await modelApi.list({
      page: currentPage.value,
      page_size: pageSize.value,
      search: searchQuery.value
    })
    modelList.value = response.models
    total.value = response.total
  } catch (error) {
    ElMessage.error('获取模型列表失败')
    console.error('Error fetching models:', error)
  } finally {
    loading.value = false
  }
}

// 刷新列表
const handleRefresh = async () => {
  try {
    refreshLoading.value = true
    // 重置到第一页
    currentPage.value = 1
    // 重新获取模型列表
    await fetchModels()
    ElMessage.success('列表刷新成功')
  } catch (error) {
    ElMessage.error('刷新列表失败')
    console.error('Error refreshing models:', error)
  } finally {
    refreshLoading.value = false
  }
}

// 获取数据集选项
const fetchDatasetOptions = async () => {
  try {
    const response = await datasetApi.list({ page_size: 1000 })
    datasetOptions.value = response.datasets.filter(dataset => dataset.status === 1) // 只显示验证通过的数据集
  } catch (error) {
    console.error('Error fetching dataset options:', error)
  }
}

// 获取基础模型选项
const fetchBaseModelOptions = async () => {
  try {
    const response = await modelApi.getBaseModels()
    baseModelOptions.value = response.base_models
  } catch (error) {
    console.error('Error fetching base model options:', error)
  }
}

const formatDatasetOption = (dataset: Dataset) => {
  const modality = dataset.dataset_mode === 'multimodal'
    ? `RGB + ${dataset.secondary_modality || 'X'}`
    : 'RGB'
  return `${dataset.name}（${modality}）`
}

// 状态相关方法
const getStatusText = (status: number): string => {
  const statusMap: Record<number, string> = {
    0: '训练中',
    1: '训练完成',
    2: '训练失败',
    3: '已发布'
  }
  return statusMap[status] || '未知'
}

const getStatusType = (status: number) => {
  const typeMap: Record<number, 'warning' | 'success' | 'danger' | 'info'> = {
    0: 'warning',
    1: 'success',
    2: 'danger',
    3: 'info'
  }
  return typeMap[status] || 'info'
}

// 解析训练结果
const parseTrainResult = (trainResult: string | object) => {
  if (typeof trainResult === 'string') {
    try {
      return JSON.parse(trainResult)
    } catch {
      return {}
    }
  }
  return trainResult || {}
}

// 格式化时间
const formatDateTime = (dateTime: string): string => {
  return new Date(dateTime).toLocaleString('zh-CN')
}

// 格式化指标值，处理-1的默认值
const formatMetricValue = (value: number | undefined, precision: number = 3): string => {
  if (value === undefined || value === null || value === -1) {
    return 'N/A'
  }
  return value.toFixed(precision)
}

// 搜索处理
let searchTimeout: NodeJS.Timeout | null = null
const handleSearch = () => {
  if (searchTimeout) {
    clearTimeout(searchTimeout)
  }
  searchTimeout = setTimeout(() => {
    currentPage.value = 1
    fetchModels()
  }, 500)
}

// 立即搜索
const performSearch = () => {
  currentPage.value = 1
  fetchModels()
}

// 清空搜索
const clearSearch = () => {
  searchQuery.value = ''
  currentPage.value = 1
  fetchModels()
}

// 分页处理
const handleSizeChange = (size: number) => {
  pageSize.value = size
  currentPage.value = 1
  fetchModels()
}

const handleCurrentChange = (page: number) => {
  currentPage.value = page
  fetchModels()
}

// 创建模型
const handleCreate = async () => {
  if (!createFormRef.value) return
  
  const valid = await createFormRef.value.validate().catch(() => false)
  if (!valid) return

  try {
    createLoading.value = true
    await modelApi.create(createForm)
    ElMessage.success('模型创建成功，开始训练')
    showCreateDialog.value = false
    resetCreateForm()
    fetchModels()
  } catch (error: any) {
    ElMessage.error(error.response?.data?.message || '创建模型失败')
  } finally {
    createLoading.value = false
  }
}

const handleWeightChange = (uploadFile: UploadFile) => {
  const raw = uploadFile.raw
  if (!raw || !raw.name.toLowerCase().endsWith('.pt')) {
    importWeightFile.value = null
    importUploadRef.value?.clearFiles()
    ElMessage.warning('请选择 .pt 模型权重文件')
    return
  }
  importWeightFile.value = raw
}

const handleWeightRemove = () => {
  importWeightFile.value = null
}

const handleImport = async () => {
  if (!importFormRef.value) return
  const valid = await importFormRef.value.validate().catch(() => false)
  if (!valid) return
  if (!importWeightFile.value) {
    ElMessage.warning('请选择 .pt 模型权重文件')
    return
  }

  const formData = new FormData()
  formData.append('name', importForm.name.trim())
  formData.append('description', importForm.description.trim())
  formData.append('dataset_id', importForm.dataset_id)
  formData.append('weight', importWeightFile.value)
  try {
    importLoading.value = true
    await modelApi.importWeight(formData)
    ElMessage.success('模型权重已验证并导入，请在列表中确认后发布')
    showImportDialog.value = false
    resetImportForm()
    await fetchModels()
  } catch (error: any) {
    ElMessage.error(error.response?.data?.msg || error.message || '模型权重导入失败')
  } finally {
    importLoading.value = false
  }
}

const resetImportForm = () => {
  Object.assign(importForm, {
    name: '',
    description: '',
    dataset_id: ''
  })
  importWeightFile.value = null
  importUploadRef.value?.clearFiles()
  importFormRef.value?.clearValidate()
}

// 重置创建表单
const resetCreateForm = () => {
  Object.assign(createForm, {
    name: '',
    description: '',
    dataset_id: '',
    base_model: 'yolo11n.pt',
    epochs: 50,
    batch_size: 16
  })
  createFormRef.value?.clearValidate()
}

// 编辑模型
const editModel = (model: any) => {
  currentEditId.value = model.id
  editForm.name = model.name
  editForm.description = model.description || ''
  showEditDialog.value = true
}

const handleEdit = async () => {
  if (!editFormRef.value) return
  
  const valid = await editFormRef.value.validate().catch(() => false)
  if (!valid) return

  try {
    editLoading.value = true
    await modelApi.update(currentEditId.value, editForm)
    ElMessage.success('更新成功')
    showEditDialog.value = false
    resetEditForm()
    fetchModels()
  } catch (error: any) {
    ElMessage.error(error.response?.data?.message || '更新失败')
  } finally {
    editLoading.value = false
  }
}

// 重置编辑表单
const resetEditForm = () => {
  Object.assign(editForm, {
    name: '',
    description: ''
  })
  editFormRef.value?.clearValidate()
}

// 查看模型
const viewModel = async (model: any) => {
  try {
    const response = await modelApi.get(model.id)
    currentModel.value = response
    showViewDialog.value = true
  } catch (error) {
    ElMessage.error('获取模型详情失败')
  }
}


// 发布模型
const publishModel = async (id: string) => {
  try {
    await ElMessageBox.confirm('确定要发布这个模型吗？', '提示', {
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      type: 'warning'
    })
    
    await modelApi.publish(id)
    ElMessage.success('模型发布成功')
    fetchModels()
  } catch (error: any) {
    if (error !== 'cancel') {
      ElMessage.error(error.response?.data?.message || '发布失败')
    }
  }
}

// 取消发布模型
const unpublishModel = async (id: string) => {
  try {
    await ElMessageBox.confirm('确定要取消发布这个模型吗？', '提示', {
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      type: 'warning'
    })
    
    await modelApi.unpublish(id)
    ElMessage.success('取消发布成功')
    fetchModels()
  } catch (error: any) {
    if (error !== 'cancel') {
      ElMessage.error(error.response?.data?.message || '取消发布失败')
    }
  }
}

// 删除模型
const deleteModel = async (id: string, name: string) => {
  try {
    await ElMessageBox.confirm(`确定要删除模型"${name}"吗？这个操作不能撤销。`, '警告', {
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      type: 'warning'
    })
    
    await modelApi.delete(id)
    ElMessage.success('删除成功')
    fetchModels()
  } catch (error: any) {
    if (error !== 'cancel') {
      ElMessage.error(error.response?.data?.message || '删除失败')
    }
  }
}

// 下载模型
const downloadModel = (url: string) => {
  const link = document.createElement('a')
  link.href = url
  link.download = 'model.pt'
  document.body.appendChild(link)
  link.click()
  document.body.removeChild(link)
}

// 监听对话框显示状态，确保图表正确渲染
watch(showViewDialog, (newVal) => {
  if (newVal) {
    // 对话框打开时，延迟触发图表resize
    nextTick(() => {
      setTimeout(() => {
        window.dispatchEvent(new Event('resize'))
      }, 300)
    })
  }
})

// 初始化
onMounted(() => {
  fetchModels()
  fetchDatasetOptions()
  fetchBaseModelOptions()
})
</script>

<style scoped>
.model-management {
  padding: 20px;
}

.header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 20px;
}

.header h2 {
  margin: 0;
  font-size: 24px;
  color: #303133;
}

.header-actions {
  display: flex;
  gap: 12px;
  align-items: center;
}

.search-bar {
  margin-bottom: 20px;

  .search-controls {
    display: flex;
    align-items: center;
    gap: 12px;
  }
}

.dialog-footer button:first-child {
  margin-right: 10px;
}

/* 模型详情对话框样式 */
.model-detail-dialog .el-dialog__body {
  padding: 20px 24px;
}

.model-detail {
  max-height: 70vh;
  overflow-y: auto;
}

/* 分区样式 */
.detail-section {
  margin-bottom: 32px;
  background: #fafafa;
  border-radius: 12px;
  padding: 24px;
  border: 1px solid #f0f0f0;
}

.detail-section:last-child {
  margin-bottom: 0;
}

/* 分区头部 */
.section-header {
  display: flex;
  align-items: center;
  margin-bottom: 20px;
  padding-bottom: 12px;
  border-bottom: 2px solid #e4e7ed;
}

.section-icon {
  font-size: 20px;
  color: #409eff;
  margin-right: 8px;
}

.section-title {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
  color: #303133;
}

/* 信息网格 */
.info-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 16px;
  margin-bottom: 16px;
}

.info-item {
  display: flex;
  flex-direction: column;
  padding: 16px;
  background: white;
  border-radius: 8px;
  border: 1px solid #e4e7ed;
  transition: all 0.3s ease;
}

.info-item:hover {
  border-color: #409eff;
  box-shadow: 0 2px 8px rgba(64, 158, 255, 0.1);
}

.info-label {
  font-size: 13px;
  color: #909399;
  margin-bottom: 6px;
  font-weight: 500;
}

.info-value {
  font-size: 15px;
  color: #303133;
  font-weight: 500;
}

.description-section {
  margin-top: 16px;
  padding: 16px;
  background: white;
  border-radius: 8px;
  border: 1px solid #e4e7ed;
}

.description-text {
  line-height: 1.6;
  color: #606266;
}

/* 指标卡片网格 */
.metrics-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 16px;
  margin-bottom: 24px;
}

.metric-card {
  display: flex;
  align-items: center;
  padding: 20px;
  background: white;
  border-radius: 12px;
  border: 1px solid #e4e7ed;
  transition: all 0.3s ease;
  position: relative;
  overflow: hidden;
}

.metric-card::before {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 3px;
  background: linear-gradient(90deg, #409eff, #67c23a);
}

.metric-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 8px 25px rgba(0, 0, 0, 0.1);
}

.metric-card.precision::before { background: linear-gradient(90deg, #f56c6c, #ff9f9f); }
.metric-card.recall::before { background: linear-gradient(90deg, #e6a23c, #ffd666); }
.metric-card.map50::before { background: linear-gradient(90deg, #409eff, #79bbff); }
.metric-card.map50-95::before { background: linear-gradient(90deg, #67c23a, #95d475); }
.metric-card.fitness::before { background: linear-gradient(90deg, #909399, #b1b3b8); }

.metric-icon {
  font-size: 24px;
  margin-right: 16px;
  flex-shrink: 0;
}

.metric-content {
  flex: 1;
}

.metric-label {
  font-size: 14px;
  color: #606266;
  margin-bottom: 4px;
  font-weight: 500;
}

.metric-value {
  font-size: 24px;
  font-weight: bold;
  color: #303133;
  margin-bottom: 2px;
}

.metric-value.highlight {
  color: #409eff;
  font-size: 26px;
}

.metric-desc {
  font-size: 12px;
  color: #909399;
  font-style: italic;
}

/* 简单指标 */
.simple-metrics {
  display: flex;
  gap: 32px;
  padding: 20px;
  background: white;
  border-radius: 8px;
  border: 1px solid #e4e7ed;
}

.simple-metric-item {
  display: flex;
  flex-direction: column;
  align-items: center;
}

.simple-label {
  font-size: 14px;
  color: #606266;
  margin-bottom: 8px;
}

.simple-value {
  font-size: 18px;
  font-weight: bold;
  color: #303133;
}

/* 图表区域 */
.chart-section {
  margin-top: 24px;
  padding-top: 24px;
  border-top: 1px solid #e4e7ed;
}

.chart-container {
  background: white;
  border-radius: 8px;
  padding: 20px;
  border: 1px solid #e4e7ed;
}

/* 错误区域 */
.error-section {
  padding: 20px;
  background: white;
  border-radius: 8px;
  border: 1px solid #e4e7ed;
}

/* 下载区域 */
.download-section {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 20px;
  background: white;
  border-radius: 8px;
  border: 1px solid #e4e7ed;
}

.download-info p {
  margin: 0;
  color: #606266;
  line-height: 1.6;
}

/* 表格内容样式 */
.training-params {
  text-align: center;
  font-size: 12px;
}

.training-results {
  text-align: center;
  font-size: 12px;
}

/* 响应式设计 */
@media (max-width: 768px) {
  .info-grid {
    grid-template-columns: 1fr;
  }
  
  .metrics-grid {
    grid-template-columns: 1fr;
  }
  
  .simple-metrics {
    flex-direction: column;
    gap: 16px;
  }
  
  .download-section {
    flex-direction: column;
    gap: 16px;
    text-align: center;
  }
}
</style>
