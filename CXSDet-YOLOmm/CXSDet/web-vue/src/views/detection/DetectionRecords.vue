<!-- MD5: 238e110d9cfd71b3d6b93f5a7d7256c2 -->
<!--
版权所有 © 2025 羊羊小栈 (GJQ) | 作者：羊羊小栈 | 时间：2025-09-06 23:44:27

本系统为原创作品，禁止二次销售！禁止将系统对应的视频、文档进行二次发布！
违者需立即停止侵权行为，并按照【羊羊小栈系统版权声明及保护条款】中规定数额进行赔偿，并承担相应法律责任。
-->

<template>
  <div class="detection-records">
    <!-- 页面头部 -->
    <div class="page-header">
      <div class="header-content">
        <div class="header-left">
          <h1 class="page-title">图片识别记录</h1>
          <p class="page-subtitle">查看和管理所有图片识别历史记录</p>
        </div>
        <div class="header-right">
          <el-button type="primary" @click="$router.push('/detection')" class="new-btn">
            <el-icon><Plus /></el-icon>
            新建识别
          </el-button>
        </div>
      </div>
    </div>

    <!-- 主要内容区域 -->
    <div class="main-content">
      <!-- 搜索和统计区域 -->
      <el-card class="search-card" shadow="never">
        <div class="search-section">
          <div class="search-left">
            <el-input
              v-model="searchQuery"
              placeholder="搜索标题、描述或模型名称"
              class="search-input"
              @input="handleSearch"
              @keyup.enter="handleSearch"
              clearable
            >
              <template #prefix>
                <el-icon><Search /></el-icon>
              </template>
            </el-input>
            <div class="search-buttons">
              <el-button 
                type="primary" 
                @click="handleSearch"
                :loading="loading"
                class="search-btn"
              >
                <el-icon><Search /></el-icon>
                搜索
              </el-button>
              <el-button 
                @click="handleClear"
                class="clear-btn"
              >
                <el-icon><RefreshLeft /></el-icon>
                清空
              </el-button>
            </div>
          </div>
          <div class="search-right">
            <div class="stats-info">
              <span class="stats-text">共 {{ total }} 条记录</span>
            </div>
          </div>
        </div>
      </el-card>

      <!-- 记录列表 -->
      <el-card class="records-card" shadow="never">
        <div class="batch-toolbar">
          <div>
            <strong>已选择 {{ selectedRecords.length }} 条</strong>
            <span class="batch-toolbar-tip">AI 核查后仍需逐条人工确认，确认后才能回流训练集。</span>
          </div>
          <div class="batch-toolbar-actions">
            <el-button
              type="warning"
              :disabled="selectedRecords.length === 0"
              :loading="batchReviewLoading"
              @click="batchReviewWithAI"
            >批量AI核查</el-button>
            <el-button
              type="success"
              :disabled="selectedRecords.every(record => record.human_review_status !== 'confirmed')"
              :loading="batchFeedbackLoading"
              @click="batchAddToFeedback"
            >批量加入反馈</el-button>
          </div>
        </div>
        <el-alert
          v-if="feedbackDatasetGroups.length"
          type="success"
          :closable="false"
          show-icon
          class="feedback-version-alert"
        >
          <template #title>已回流到原训练数据集的反馈池，请生成不可变的新版本</template>
          <template #default>
            <el-button
              v-for="group in feedbackDatasetGroups"
              :key="group.dataset_id"
              type="primary"
              size="small"
              :loading="versionBuildingId === group.dataset_id"
              @click="createFeedbackVersion(group)"
            >生成新数据集版本：{{ group.dataset_name }}</el-button>
          </template>
        </el-alert>
        <el-table
          :data="recordList"
          v-loading="loading"
          class="records-table"
          border
          stripe
          @selection-change="handleSelectionChange"
        >
          <el-table-column type="selection" width="50" align="center" />
          <el-table-column prop="title" label="标题" width="160" show-overflow-tooltip align="center">
            <template #default="scope">
              <div class="title-cell">
                <span class="title-text">{{ scope.row.title }}</span>
              </div>
            </template>
          </el-table-column>
          
          <el-table-column prop="model_name" label="使用模型" width="180" align="center">
            <template #default="scope">
              <el-tag type="primary" size="small">{{ scope.row.model_name }}</el-tag>
            </template>
          </el-table-column>

          <el-table-column prop="source_dataset_name" label="原训练数据集" width="180" align="center" show-overflow-tooltip>
            <template #default="scope">
              <el-tag v-if="scope.row.source_dataset_name" type="success" size="small">
                {{ scope.row.source_dataset_name }}
              </el-tag>
              <el-tag v-else type="danger" size="small">未关联</el-tag>
            </template>
          </el-table-column>

          <el-table-column label="X 模态" width="120" align="center">
            <template #default="scope">
              <el-tag v-if="scope.row.secondary_image_url" type="warning" size="small">
                {{ scope.row.secondary_image_origin === 'generated' ? '自动生成' : '已上传' }}
              </el-tag>
              <span v-else>-</span>
            </template>
          </el-table-column>

          <el-table-column label="复核状态" width="130" align="center">
            <template #default="scope">
              <el-tag type="info" size="small">{{ scope.row.human_review_status || 'pending' }}</el-tag>
            </template>
          </el-table-column>
          
          <el-table-column v-if="userStore.isAdmin()" prop="user_name" label="用户" width="120" align="center">
            <template #default="scope">
              <el-tag type="info" size="small">{{ scope.row.user_name }}</el-tag>
            </template>
          </el-table-column>
          
          <el-table-column label="检测结果" width="140" align="center">
            <template #default="scope">
              <div class="detection-result">
                <el-tag v-if="getDetectionCount(scope.row) > 0" type="success" size="small">
                  <el-icon><Check /></el-icon>
                  {{ getDetectionCount(scope.row) }} 个对象
                </el-tag>
                <el-tag v-else type="info" size="small">
                  <el-icon><Close /></el-icon>
                  无检测结果
                </el-tag>
              </div>
            </template>
          </el-table-column>
          
          <el-table-column prop="description" label="描述" show-overflow-tooltip>
            <template #default="scope">
              <span class="description-text">{{ scope.row.description || '-' }}</span>
            </template>
          </el-table-column>
          
          <el-table-column label="操作" width="330" align="center" fixed="right">
            <template #default="scope">
              <div class="action-buttons">
                <el-button size="small" @click="viewRecord(scope.row)" class="action-btn view-btn">
                  <el-icon><View /></el-icon>
                  查看
                </el-button>
                <el-button size="small" @click="editRecord(scope.row)" class="action-btn edit-btn">
                  <el-icon><Edit /></el-icon>
                  编辑
                </el-button>
                <el-button
                  size="small"
                  type="warning"
                  :loading="aiReviewLoading === scope.row.id"
                  @click="reviewWithAI(scope.row)"
                >
                  AI复核
                </el-button>
                <el-button 
                  size="small" 
                  type="danger" 
                  @click="deleteRecord(scope.row.id, scope.row.title)"
                  class="action-btn delete-btn"
                >
                  <el-icon><Delete /></el-icon>
                  删除
                </el-button>
              </div>
            </template>
          </el-table-column>
        </el-table>

        <!-- 分页器 -->
        <div class="pagination-wrapper">
          <el-pagination
            v-model:current-page="currentPage"
            v-model:page-size="pageSize"
            :page-sizes="[10, 20, 50, 100]"
            :total="total"
            layout="total, sizes, prev, pager, next, jumper"
            @size-change="handleSizeChange"
            @current-change="handleCurrentChange"
            class="pagination"
          />
        </div>
      </el-card>
    </div>

    <!-- 查看记录对话框 -->
    <el-dialog 
      v-model="showViewDialog" 
      title="识别记录详情" 
      width="90%"
      :close-on-click-modal="false"
      class="view-dialog"
      :style="{ 'max-width': '1400px' }"
    >
      <div v-if="currentRecord" class="record-detail">
        <!-- 基本信息 -->
        <div class="detail-section">
          <div class="section-header">
            <el-icon class="section-icon"><InfoFilled /></el-icon>
            <h3 class="section-title">基本信息</h3>
          </div>
          <div class="info-grid">
            <div class="info-item">
              <div class="info-label">标题</div>
              <div class="info-value info-text-primary">{{ currentRecord.title }}</div>
            </div>

            <div v-if="currentRecord.secondary_image_url" class="image-container">
              <div class="image-header">
                <h4>X 模态图片</h4>
                <el-tag type="warning" size="small">
                  {{ currentRecord.secondary_image_origin === 'generated' ? '自动生成' : '上传' }}
                </el-tag>
              </div>
              <div class="image-wrapper">
                <el-image
                  :src="currentRecord.secondary_image_url"
                  alt="X模态图片"
                  class="result-image"
                  fit="contain"
                  :preview-src-list="getPreviewImages(currentRecord)"
                  :initial-index="1"
                  preview-teleported
                />
              </div>
            </div>
            <div class="info-item">
              <div class="info-label">使用模型</div>
              <div class="info-value info-text-success">{{ currentRecord.model_name }}</div>
            </div>
            <div v-if="userStore.isAdmin() && currentRecord.user_name" class="info-item">
              <div class="info-label">用户</div>
              <div class="info-value info-text-default">{{ currentRecord.user_name }}</div>
            </div>
            <div class="info-item">
              <div class="info-label">置信度阈值</div>
              <div class="info-value info-text-warning">{{ (currentRecord.confidence_threshold * 100).toFixed(0) }}%</div>
            </div>
            <div class="info-item">
              <div class="info-label">检测结果数量</div>
              <div class="info-value" :class="getDetectionCount(currentRecord) > 0 ? 'info-text-success' : 'info-text-info'">
                {{ getDetectionCount(currentRecord) }} 个对象
              </div>
            </div>
            <div class="info-item">
              <div class="info-label">创建时间</div>
              <div class="info-value info-text-time">{{ formatDateTime(currentRecord.create_time) }}</div>
            </div>
          </div>
          <div v-if="currentRecord.description" class="description-section">
            <div class="info-label">描述</div>
            <div class="info-value description-text">{{ currentRecord.description }}</div>
          </div>
        </div>

        <!-- 图片对比 -->
        <div class="detail-section">
          <div class="section-header">
            <el-icon class="section-icon"><Picture /></el-icon>
            <h3 class="section-title">图片对比</h3>
          </div>
          
          <div class="result-images">
            <div class="image-container">
              <div class="image-header">
                <h4>原始图片</h4>
                <el-tag type="info" size="small">输入</el-tag>
              </div>
              <div class="image-wrapper">
                <el-image
                  :src="currentRecord.original_image_url"
                  alt="原始图片"
                  class="result-image"
                  fit="contain"
                  :preview-src-list="getPreviewImages(currentRecord)"
                  :initial-index="0"
                  preview-teleported
                >
                  <template #error>
                    <div class="image-error">
                      <el-icon><Picture /></el-icon>
                      <span>图片加载失败</span>
                    </div>
                  </template>
                </el-image>
              </div>
            </div>
            
            <div v-if="currentRecord.result_image_url" class="image-container">
              <div class="image-header">
                <h4>识别结果图片</h4>
                <el-tag type="success" size="small">输出</el-tag>
              </div>
              <div class="image-wrapper">
                <el-image
                  :src="currentRecord.result_image_url"
                  alt="识别结果"
                  class="result-image"
                  fit="contain"
                  :preview-src-list="getPreviewImages(currentRecord)"
                  :initial-index="currentRecord.secondary_image_url ? 2 : 1"
                  preview-teleported
                >
                  <template #error>
                    <div class="image-error">
                      <el-icon><Picture /></el-icon>
                      <span>图片加载失败</span>
                    </div>
                  </template>
                </el-image>
              </div>
            </div>
            
            <div v-if="currentRecord.enhanced_image_url" class="image-container">
              <div class="image-header">
                <h4>图像增强结果</h4>
                <el-tag type="warning" size="small">增强</el-tag>
              </div>
              <div class="image-wrapper">
                <el-image
                  :src="currentRecord.enhanced_image_url"
                  alt="增强图片"
                  class="result-image"
                  fit="contain"
                  :preview-src-list="getPreviewImages(currentRecord)"
                  :initial-index="2"
                  preview-teleported
                >
                  <template #error>
                    <div class="image-error">
                      <el-icon><Picture /></el-icon>
                      <span>图片加载失败</span>
                    </div>
                  </template>
                </el-image>
              </div>
            </div>
          </div>
        </div>

        <!-- 检测详情 -->
        <div v-if="getDetections(currentRecord).length > 0" class="detail-section">
          <div class="section-header">
            <el-icon class="section-icon"><List /></el-icon>
            <h3 class="section-title">检测详情</h3>
          </div>
          
          <div class="detections-container">
            <el-table :data="getDetections(currentRecord)" border class="detections-table">
              <el-table-column prop="class_name" label="缺陷类别" width="150" align="center">
                <template #default="scope">
                  <el-tag 
                    :color="getClassColor(scope.row.class_name)" 
                    size="small"
                    style="color: white; border: none;"
                  >
                    {{ getClassDisplayName(scope.row.class_name) }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="confidence" label="置信度" width="120" align="center">
                <template #default="scope">
                  <div class="confidence-display">
                    <el-progress 
                      :percentage="scope.row.confidence * 100" 
                      :color="getConfidenceColor(scope.row.confidence)"
                      :show-text="false"
                      class="confidence-progress"
                    />
                    <span class="confidence-text">{{ (scope.row.confidence * 100).toFixed(1) }}%</span>
                  </div>
                </template>
              </el-table-column>
              <el-table-column prop="bbox" label="位置信息" align="center">
                <template #default="scope">
                  <el-tag type="info" size="small">
                    [{{ scope.row.bbox.map((v: number) => Math.round(v)).join(', ') }}]
                  </el-tag>
                </template>
              </el-table-column>
            </el-table>
          </div>
        </div>

        <div class="detail-section human-review-panel">
          <div class="section-header">
            <el-icon class="section-icon"><Edit /></el-icon>
            <h3 class="section-title">人工标注确认</h3>
            <el-tag type="info">版本 {{ currentRecord.review_version || 0 }}</el-tag>
          </div>
          <el-alert
            title="AI 复核只提供建议，不会自动写入真值；以下标注必须由人工确认后才能进入反馈数据集。"
            type="warning"
            :closable="false"
            show-icon
          />
          <div v-if="currentRecord.ai_review_result" class="ai-review-actions">
            <div
              v-if="getCurrentAIReview().possible_false_positives?.length"
              class="ai-suggestion-group false-positive-group"
            >
              <strong>AI 提示的疑似误报</strong>
              <div
                v-for="index in getCurrentAIReview().possible_false_positives"
                :key="`false-${index}`"
                class="ai-suggestion-item"
              >
                <span>
                  标注 #{{ index + 1 }}
                  <template v-if="getDetectionByIndex(index)">
                    （{{ getClassDisplayName(getDetectionByIndex(index).class_name) }}）
                  </template>
                  疑似多余
                </span>
                <el-button type="danger" size="small" plain @click="removeFalsePositive(index)">
                  一键移除 #{{ index + 1 }}
                </el-button>
              </div>
            </div>
            <div
              v-if="getCurrentAIReview().possible_missed_detections?.length"
              class="ai-suggestion-group missed-detection-group"
            >
              <strong>AI 提示的疑似漏检</strong>
              <div
                v-for="(suggestion, index) in getCurrentAIReview().possible_missed_detections"
                :key="`missed-${index}`"
                class="ai-suggestion-item"
              >
                <span>
                  {{ getClassDisplayName(suggestion.class_name) }}：{{ suggestion.reason || '疑似存在未标注缺陷' }}
                </span>
                <el-button type="primary" size="small" plain @click="addMissedDetection(suggestion)">
                  一键加入标注
                </el-button>
              </div>
            </div>
          </div>
          <div v-if="!reviewNoDefect" class="annotation-workspace">
            <div
              ref="annotationCanvasRef"
              class="annotation-canvas"
              :class="{ 'is-drawing': drawMode }"
              @pointerdown="startDrawing"
              @pointermove="updateDrawing"
              @pointerup="finishDrawing"
              @pointercancel="cancelDrawing"
            >
              <img
                :src="currentRecord.original_image_url"
                alt="人工核查原图"
                class="annotation-source-image"
                draggable="false"
                @load="handleReviewImageLoad"
              />
              <button
                v-for="(annotation, index) in reviewAnnotations"
                :key="`annotation-${index}`"
                type="button"
                class="annotation-box"
                :class="{
                  selected: selectedAnnotationIndex === index,
                  'suspected-false-positive': isSuggestedFalsePositive(annotation, index)
                }"
                :style="annotationBoxStyle(annotation.bbox)"
                @pointerdown.stop
                @click.stop="selectedAnnotationIndex = index"
              >
                <span class="annotation-number">#{{ index + 1 }}</span>
              </button>
              <div
                v-if="drawingPreview"
                class="annotation-box drawing-preview"
                :style="annotationBoxStyle(drawingPreview)"
              />
            </div>
            <div class="annotation-help">
              图中编号与下方表格一致。点击“绘制新增框”后，在图片上按住并拖动即可完成标注。
            </div>
          </div>
          <div class="review-toolbar">
            <el-checkbox v-model="reviewNoDefect" @change="handleNoDefectChange">确认无缺陷</el-checkbox>
            <el-button
              size="small"
              :type="drawMode ? 'warning' : 'primary'"
              :disabled="reviewNoDefect"
              @click="toggleDrawMode"
            >
              {{ drawMode ? '取消绘制' : '绘制新增框' }}
            </el-button>
          </div>
          <el-table
            v-if="!reviewNoDefect"
            :data="reviewAnnotations"
            border
            size="small"
            :row-class-name="reviewRowClassName"
            @row-click="selectReviewRow"
          >
            <el-table-column label="序号" width="70" align="center">
              <template #default="scope">
                <strong>#{{ scope.$index + 1 }}</strong>
              </template>
            </el-table-column>
            <el-table-column label="类别" width="150">
              <template #default="scope">
                <el-select v-model="scope.row.class_name" @change="syncClassId(scope.row)">
                  <el-option
                    v-for="(value, key, index) in yoloClasses"
                    :key="key"
                    :label="value.chinese_name"
                    :value="key"
                  >
                    <span>{{ index }} - {{ value.chinese_name }}</span>
                  </el-option>
                </el-select>
              </template>
            </el-table-column>
            <el-table-column label="坐标（由图片标注生成）" min-width="280">
              <template #default="scope">
                <el-tag type="info">{{ formatNormalizedBox(scope.row.bbox) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="80">
              <template #default="scope">
                <el-button type="danger" link @click.stop="removeReviewAnnotation(scope.$index)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
          <div class="review-actions">
            <el-button type="primary" :loading="humanReviewLoading" @click="submitHumanReview">提交人工复核</el-button>
            <span>回流目标：</span>
            <el-tag v-if="currentRecord.source_dataset_name" type="success">
              {{ currentRecord.source_dataset_name }}
            </el-tag>
            <el-tag v-else type="danger">当前模型未关联原训练数据集</el-tag>
            <el-button
              type="success"
              :disabled="currentRecord.human_review_status !== 'confirmed' || !currentRecord.source_dataset_id"
              :loading="feedbackLoading"
              @click="addCurrentToFeedback"
            >
              加入原数据集反馈池
            </el-button>
            <el-button
              v-if="currentRecord.feedback_dataset_id"
              type="primary"
              :loading="versionBuildingId === currentRecord.feedback_dataset_id"
              @click="createFeedbackVersion({
                dataset_id: currentRecord.feedback_dataset_id,
                dataset_name: currentRecord.source_dataset_name || '原训练数据集',
                record_ids: [currentRecord.id]
              })"
            >生成新数据集版本</el-button>
          </div>
        </div>

        <!-- 分析结果显示 -->
        <div v-if="currentRecord.analysis_result" class="detail-section">
          <div class="section-header">
            <el-icon class="section-icon"><ChatDotRound /></el-icon>
            <h3 class="section-title">大模型缺陷分析结果</h3>
            <el-tooltip content="支持 Markdown 格式" placement="top">
              <el-icon class="info-icon"><InfoFilled /></el-icon>
            </el-tooltip>
          </div>
          
          <div class="analysis-container">
            <el-scrollbar max-height="400px">
              <div v-html="formatAnalysisText(currentRecord.analysis_result)" class="analysis-text markdown-content"></div>
            </el-scrollbar>
          </div>
        </div>

        <!-- 建议显示 -->
        <div v-if="currentRecord.ai_review_result" class="detail-section">
          <div class="section-header">
            <el-icon class="section-icon"><ChatDotRound /></el-icon>
            <h3 class="section-title">AI辅助复核（需人工确认）</h3>
          </div>
          <el-alert
            :title="parseAIReview(currentRecord.ai_review_result).summary"
            :type="parseAIReview(currentRecord.ai_review_result).status === 'consistent' ? 'success' : 'warning'"
            :closable="false"
            show-icon
          />
          <div class="ai-review-summary">
            <span v-if="parseAIReview(currentRecord.ai_review_result).possible_false_positives?.length">
              疑似误报：{{ parseAIReview(currentRecord.ai_review_result).possible_false_positives.map((index: number) => `#${index + 1}`).join('、') }}
            </span>
            <span v-if="parseAIReview(currentRecord.ai_review_result).possible_missed_detections?.length">
              疑似漏检：{{ parseAIReview(currentRecord.ai_review_result).possible_missed_detections.length }} 处，可在上方一键加入
            </span>
          </div>
        </div>

        <div v-if="currentRecord.suggestions" class="detail-section">
          <div class="section-header">
            <el-icon class="section-icon"><Star /></el-icon>
            <h3 class="section-title">改进建议</h3>
            <el-tooltip content="支持 Markdown 格式" placement="top">
              <el-icon class="info-icon"><InfoFilled /></el-icon>
            </el-tooltip>
          </div>
          
          <div class="suggestions-container">
            <el-scrollbar max-height="400px">
              <div v-html="formatAnalysisText(currentRecord.suggestions)" class="suggestions-text markdown-content"></div>
            </el-scrollbar>
          </div>
        </div>
      </div>
    </el-dialog>

    <!-- 编辑记录对话框 -->
    <el-dialog v-model="showEditDialog" title="编辑记录" width="500px" @close="resetEditForm" class="edit-dialog">
      <el-form
        ref="editFormRef"
        :model="editForm"
        :rules="editRules"
        label-width="80px"
      >
        <el-form-item label="标题" prop="title">
          <el-input v-model="editForm.title" placeholder="请输入标题" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input
            v-model="editForm.description"
            type="textarea"
            placeholder="请输入描述"
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
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, reactive } from 'vue'
import { ElMessage, ElMessageBox, ElForm } from 'element-plus'
import { 
  Plus, 
  Search, 
  View, 
  Edit, 
  Delete, 
  Check, 
  Close, 
  InfoFilled, 
  Picture, 
  List, 
  MagicStick, 
  ChatDotRound, 
  Star,
  RefreshLeft
} from '@element-plus/icons-vue'
import {
  detectionApi,
  type AIReviewResult,
  type BatchDatasetGroup,
  type DetectionRecord,
  type HumanReviewAnnotation,
  type UpdateDetectionRequest
} from '@/api/detection'
import { modelApi, type YoloClassesConfig } from '@/api/model'
import { datasetApi } from '@/api/dataset'
import { useUserStore } from '@/stores/user'
import { marked } from 'marked'

// 使用用户store
const userStore = useUserStore()

// 响应式数据
const loading = ref(false)
const aiReviewLoading = ref<string | null>(null)
const batchReviewLoading = ref(false)
const batchFeedbackLoading = ref(false)
const versionBuildingId = ref<string | null>(null)
const humanReviewLoading = ref(false)
const feedbackLoading = ref(false)
type EditableReviewAnnotation = HumanReviewAnnotation & { source_index?: number }
const reviewAnnotations = ref<EditableReviewAnnotation[]>([])
const reviewNoDefect = ref(false)
const selectedRecords = ref<DetectionRecord[]>([])
const feedbackDatasetGroups = ref<BatchDatasetGroup[]>([])
const annotationCanvasRef = ref<HTMLElement | null>(null)
const reviewImageSize = ref({ width: 1, height: 1 })
const drawMode = ref(false)
const drawingStart = ref<{ x: number; y: number } | null>(null)
const drawingPreview = ref<[number, number, number, number] | null>(null)
const selectedAnnotationIndex = ref<number | null>(null)
const recordList = ref<DetectionRecord[]>([])
const total = ref(0)
const currentPage = ref(1)
const pageSize = ref(10)
const searchQuery = ref('')
const yoloClasses = ref<YoloClassesConfig>({})

// 对话框显示状态
const showViewDialog = ref(false)
const showEditDialog = ref(false)

// 表单数据
const editForm = reactive<UpdateDetectionRequest>({
  title: '',
  description: ''
})

// 表单引用
const editFormRef = ref<InstanceType<typeof ElForm>>()

// 加载状态
const editLoading = ref(false)

// 当前操作的记录
const currentRecord = ref<DetectionRecord | null>(null)

const normalizeReviewAnnotations = async (record: DetectionRecord) => {
  const dimensions = await new Promise<{ width: number; height: number }>((resolve) => {
    const image = new Image()
    image.onload = () => resolve({ width: image.naturalWidth || 1, height: image.naturalHeight || 1 })
    image.onerror = () => resolve({ width: 1, height: 1 })
    image.src = record.original_image_url
  })
  reviewImageSize.value = dimensions
  if (record.corrected_annotations) {
    try {
      const corrected = JSON.parse(record.corrected_annotations)
      if (Array.isArray(corrected)) {
        reviewAnnotations.value = corrected.map((item, index) => ({ ...item, source_index: index }))
        reviewNoDefect.value = corrected.length === 0 && record.human_review_status === 'confirmed'
        return
      }
    } catch {
      // 回退到原始检测结果。
    }
  }
  const detections = getDetections(record)
  const classNames = Object.keys(yoloClasses.value)
  reviewAnnotations.value = detections.map((item: any, index: number) => {
    const raw = (item.bbox || [0, 0, 1, 1]).map(Number)
    const normalized = raw.every((value: number) => value >= 0 && value <= 1)
      ? raw
      : [raw[0] / dimensions.width, raw[1] / dimensions.height, raw[2] / dimensions.width, raw[3] / dimensions.height]
    const className = item.class_name || classNames[Number(item.class)] || classNames[0]
    return {
      class_name: className,
      class_id: classNames.indexOf(className),
      bbox: normalized.map((value: number) => Math.max(0, Math.min(1, value))) as [number, number, number, number],
      source_index: index
    }
  })
  reviewNoDefect.value = false
}

const syncClassId = (annotation: any) => {
  annotation.class_id = Object.keys(yoloClasses.value).indexOf(annotation.class_name)
}

const annotationBoxStyle = (bbox: number[]) => ({
  left: `${bbox[0] * 100}%`,
  top: `${bbox[1] * 100}%`,
  width: `${Math.max(0, bbox[2] - bbox[0]) * 100}%`,
  height: `${Math.max(0, bbox[3] - bbox[1]) * 100}%`
})

const formatNormalizedBox = (bbox: number[]) => bbox.map(value => Number(value).toFixed(4)).join(', ')

const normalizedPointer = (event: PointerEvent) => {
  const element = annotationCanvasRef.value
  if (!element) return null
  const rect = element.getBoundingClientRect()
  if (!rect.width || !rect.height) return null
  return {
    x: Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)),
    y: Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height))
  }
}

const toggleDrawMode = () => {
  drawMode.value = !drawMode.value
  drawingStart.value = null
  drawingPreview.value = null
}

const startDrawing = (event: PointerEvent) => {
  if (!drawMode.value) return
  const point = normalizedPointer(event)
  if (!point) return
  event.preventDefault()
  annotationCanvasRef.value?.setPointerCapture?.(event.pointerId)
  drawingStart.value = point
  drawingPreview.value = [point.x, point.y, point.x, point.y]
}

const updateDrawing = (event: PointerEvent) => {
  if (!drawMode.value || !drawingStart.value) return
  const point = normalizedPointer(event)
  if (!point) return
  const start = drawingStart.value
  drawingPreview.value = [
    Math.min(start.x, point.x),
    Math.min(start.y, point.y),
    Math.max(start.x, point.x),
    Math.max(start.y, point.y)
  ]
}

const finishDrawing = (event: PointerEvent) => {
  if (!drawMode.value || !drawingPreview.value) return
  updateDrawing(event)
  const bbox = drawingPreview.value
  if (bbox[2] - bbox[0] < 0.01 || bbox[3] - bbox[1] < 0.01) {
    ElMessage.warning('标注框过小，请重新拖动绘制')
    drawingStart.value = null
    drawingPreview.value = null
    return
  }
  const className = Object.keys(yoloClasses.value)[0] || ''
  reviewAnnotations.value.push({ class_name: className, class_id: 0, bbox: [...bbox] })
  selectedAnnotationIndex.value = reviewAnnotations.value.length - 1
  drawingStart.value = null
  drawingPreview.value = null
  drawMode.value = false
  ElMessage.success(`已添加标注 #${reviewAnnotations.value.length}`)
}

const cancelDrawing = () => {
  drawingStart.value = null
  drawingPreview.value = null
}

const handleReviewImageLoad = (event: Event) => {
  const image = event.target as HTMLImageElement
  reviewImageSize.value = {
    width: image.naturalWidth || 1,
    height: image.naturalHeight || 1
  }
}

const removeReviewAnnotation = (index: number) => {
  reviewAnnotations.value.splice(index, 1)
  selectedAnnotationIndex.value = null
}

const selectReviewRow = (row: EditableReviewAnnotation) => {
  const index = reviewAnnotations.value.indexOf(row)
  selectedAnnotationIndex.value = index >= 0 ? index : null
}

const handleNoDefectChange = (checked: boolean | string | number) => {
  if (Boolean(checked)) {
    reviewAnnotations.value = []
    drawMode.value = false
    selectedAnnotationIndex.value = null
  }
}

const submitHumanReview = async () => {
  if (!currentRecord.value) return
  try {
    humanReviewLoading.value = true
    const result = await detectionApi.submitHumanReview(currentRecord.value.id, {
      expected_version: currentRecord.value.review_version || 0,
      decision: reviewNoDefect.value ? 'no_defect' : 'confirmed',
      annotations: reviewNoDefect.value ? [] : reviewAnnotations.value
    })
    currentRecord.value.review_version = result.new_version
    currentRecord.value.human_review_status = result.status
    currentRecord.value.corrected_annotations = JSON.stringify(result.annotations)
    const row = recordList.value.find(item => item.id === currentRecord.value?.id)
    if (row) {
      row.review_version = result.new_version
      row.human_review_status = result.status
    }
    ElMessage.success('人工复核已保存，可加入反馈数据集')
  } catch (error: any) {
    ElMessage.error(error.response?.data?.msg || '人工复核保存失败，请刷新后重试')
  } finally {
    humanReviewLoading.value = false
  }
}

const addCurrentToFeedback = async () => {
  if (!currentRecord.value) return
  if (!currentRecord.value.source_dataset_id) {
    ElMessage.warning('当前检测模型没有关联原训练数据集')
    return
  }
  try {
    feedbackLoading.value = true
    const result = await detectionApi.addRecordsToFeedbackBatch([currentRecord.value.id])
    const item = result.items[0]
    if (!item || item.status === 'failed' || !item.dataset_id) {
      ElMessage.error(item?.error || '加入反馈数据集失败')
      return
    }
    currentRecord.value.feedback_dataset_id = item.dataset_id
    const row = recordList.value.find(record => record.id === currentRecord.value?.id)
    if (row) row.feedback_dataset_id = item.dataset_id
    feedbackDatasetGroups.value = result.dataset_groups || []
    ElMessage.success(item.created ? '已加入原数据集反馈池' : '该版本标注已在反馈池中')
  } catch (error: any) {
    ElMessage.error(error.response?.data?.msg || '加入反馈数据集失败')
  } finally {
    feedbackLoading.value = false
  }
}

const emptyAIReview = (): AIReviewResult => ({
  status: 'needs_human_review',
  summary: '尚未复核',
  possible_false_positives: [],
  possible_missed_detections: [],
  attention_regions: []
})

const parseAIReview = (value?: string): AIReviewResult => {
  if (!value) return emptyAIReview()
  try {
    const parsed = JSON.parse(value)
    return {
      ...emptyAIReview(),
      ...parsed,
      possible_false_positives: Array.isArray(parsed.possible_false_positives) ? parsed.possible_false_positives : [],
      possible_missed_detections: Array.isArray(parsed.possible_missed_detections) ? parsed.possible_missed_detections : [],
      attention_regions: Array.isArray(parsed.attention_regions) ? parsed.attention_regions : []
    }
  } catch {
    return { ...emptyAIReview(), summary: value }
  }
}

const getCurrentAIReview = () => parseAIReview(currentRecord.value?.ai_review_result)

const getDetectionByIndex = (index: number) => {
  if (!currentRecord.value || !Number.isInteger(index) || index < 0) return null
  return getDetections(currentRecord.value)[index] || null
}

const normalizeSuggestedBox = (rawBox: number[]): [number, number, number, number] | null => {
  if (!Array.isArray(rawBox) || rawBox.length !== 4) return null
  const values = rawBox.map(Number)
  if (!values.every(Number.isFinite)) return null
  const normalized = values.every(value => value >= 0 && value <= 1)
    ? values
    : [
        values[0] / reviewImageSize.value.width,
        values[1] / reviewImageSize.value.height,
        values[2] / reviewImageSize.value.width,
        values[3] / reviewImageSize.value.height
      ]
  const bbox = normalized.map(value => Math.max(0, Math.min(1, value))) as [number, number, number, number]
  return bbox[2] > bbox[0] && bbox[3] > bbox[1] ? bbox : null
}

const resolveSuggestedClass = (suggestedName: string) => {
  const classNames = Object.keys(yoloClasses.value)
  const directIndex = classNames.indexOf(suggestedName)
  if (directIndex >= 0) return { class_name: classNames[directIndex], class_id: directIndex }
  const chineseIndex = classNames.findIndex(name => yoloClasses.value[name]?.chinese_name === suggestedName)
  const index = chineseIndex >= 0 ? chineseIndex : 0
  return { class_name: classNames[index] || '', class_id: index }
}

const addMissedDetection = (suggestion: { class_name: string; bbox: number[]; reason: string }) => {
  const bbox = normalizeSuggestedBox(suggestion.bbox)
  if (!bbox) {
    ElMessage.warning('AI 未返回有效坐标，请使用“绘制新增框”在图片上拖动标注')
    drawMode.value = true
    return
  }
  const classInfo = resolveSuggestedClass(suggestion.class_name)
  const duplicated = reviewAnnotations.value.some(annotation =>
    annotation.class_name === classInfo.class_name
    && annotation.bbox.every((value, index) => Math.abs(value - bbox[index]) < 0.005)
  )
  if (duplicated) {
    ElMessage.info('该 AI 建议框已经加入人工标注')
    return
  }
  reviewNoDefect.value = false
  reviewAnnotations.value.push({ ...classInfo, bbox })
  selectedAnnotationIndex.value = reviewAnnotations.value.length - 1
  ElMessage.success(`已将 AI 漏检建议加入为标注 #${reviewAnnotations.value.length}`)
}

const removeFalsePositive = (sourceIndex: number) => {
  let index = reviewAnnotations.value.findIndex(annotation => annotation.source_index === sourceIndex)
  if (index < 0 && sourceIndex < reviewAnnotations.value.length) index = sourceIndex
  if (index < 0) {
    ElMessage.info(`标注 #${sourceIndex + 1} 已不存在`)
    return
  }
  reviewAnnotations.value.splice(index, 1)
  selectedAnnotationIndex.value = null
  ElMessage.success(`已移除 AI 提示的多余标注 #${sourceIndex + 1}`)
}

const isSuggestedFalsePositive = (annotation: EditableReviewAnnotation, rowIndex: number) => {
  const sourceIndex = annotation.source_index ?? rowIndex
  return getCurrentAIReview().possible_false_positives.includes(sourceIndex)
}

const reviewRowClassName = ({ row, rowIndex }: { row: EditableReviewAnnotation; rowIndex: number }) => {
  if (selectedAnnotationIndex.value === rowIndex) return 'selected-review-row'
  return isSuggestedFalsePositive(row, rowIndex) ? 'suspected-false-positive-row' : ''
}

const reviewWithAI = async (record: any) => {
  try {
    aiReviewLoading.value = record.id
    const review = await detectionApi.reviewRecord(record.id)
    record.ai_review_status = review.status
    record.ai_review_result = JSON.stringify(review)
    const openedRecord = currentRecord.value
    if (openedRecord && openedRecord.id === record.id) {
      openedRecord.ai_review_status = review.status
      openedRecord.ai_review_result = JSON.stringify(review)
    }
    ElMessage.success('AI辅助复核已完成，请继续人工确认')
  } catch (error: any) {
    ElMessage.error(error.response?.data?.msg || 'AI辅助复核失败')
  } finally {
    aiReviewLoading.value = null
  }
}

const handleSelectionChange = (rows: DetectionRecord[]) => {
  selectedRecords.value = rows
}

const batchReviewWithAI = async () => {
  if (!selectedRecords.value.length) return
  try {
    batchReviewLoading.value = true
    const result = await detectionApi.reviewRecordsBatch(selectedRecords.value.map(record => record.id))
    for (const item of result.items) {
      if (item.status !== 'success' || !item.result) continue
      const record = recordList.value.find(row => row.id === item.record_id)
      if (record) {
        record.ai_review_status = item.result.status
        record.ai_review_result = JSON.stringify(item.result)
      }
    }
    if (result.failed) {
      const reasons = result.items
        .filter(item => item.status === 'failed')
        .slice(0, 3)
        .map(item => `${item.record_id}: ${item.error}`)
        .join('；')
      ElMessage.warning(`批量AI核查完成：成功 ${result.succeeded}，失败 ${result.failed}。${reasons}`)
    } else {
      ElMessage.success(`批量AI核查完成：${result.succeeded} 条，请继续人工确认`)
    }
  } catch (error: any) {
    ElMessage.error(error.response?.data?.msg || '批量AI核查失败')
  } finally {
    batchReviewLoading.value = false
  }
}

const batchAddToFeedback = async () => {
  const confirmed = selectedRecords.value.filter(record => record.human_review_status === 'confirmed')
  if (!confirmed.length) {
    ElMessage.warning('请选择已经提交人工确认的记录')
    return
  }
  const skipped = selectedRecords.value.length - confirmed.length
  try {
    batchFeedbackLoading.value = true
    const result = await detectionApi.addRecordsToFeedbackBatch(confirmed.map(record => record.id))
    for (const item of result.items) {
      if (item.status !== 'success' || !item.dataset_id) continue
      const record = recordList.value.find(row => row.id === item.record_id)
      if (record) record.feedback_dataset_id = item.dataset_id
    }
    feedbackDatasetGroups.value = result.dataset_groups || []
    const suffix = skipped ? `，另有 ${skipped} 条未人工确认已跳过` : ''
    if (result.failed) {
      ElMessage.warning(`反馈回流完成：成功 ${result.succeeded}，失败 ${result.failed}${suffix}`)
    } else {
      ElMessage.success(`已回流 ${result.succeeded} 条到原数据集反馈池${suffix}`)
    }
  } catch (error: any) {
    ElMessage.error(error.response?.data?.msg || '批量加入反馈数据集失败')
  } finally {
    batchFeedbackLoading.value = false
  }
}

const createFeedbackVersion = async (group: BatchDatasetGroup) => {
  try {
    await ElMessageBox.confirm(
      `将从“${group.dataset_name}”的已确认反馈样本生成一个新数据集版本，原数据集不会被覆盖。`,
      '生成新数据集版本',
      { confirmButtonText: '生成版本', cancelButtonText: '取消', type: 'info' }
    )
    versionBuildingId.value = group.dataset_id
    const version = await datasetApi.createFeedbackVersion(group.dataset_id)
    ElMessage.success(`已生成 ${version.name}（${version.sample_count} 个样本）`)
    feedbackDatasetGroups.value = feedbackDatasetGroups.value.filter(
      item => item.dataset_id !== group.dataset_id
    )
    await fetchRecords()
  } catch (error: any) {
    if (error === 'cancel' || error === 'close') return
    ElMessage.error(error.response?.data?.msg || '生成反馈数据集版本失败')
  } finally {
    versionBuildingId.value = null
  }
}
const currentEditId = ref('')

// 表单验证规则
const editRules = {
  title: [
    { required: true, message: '请输入标题', trigger: 'blur' }
  ]
}

// 获取YOLO类别配置
const fetchYoloClasses = async () => {
  try {
    const response = await modelApi.getYoloClasses()
    yoloClasses.value = response.classes
  } catch (error) {
    console.error('Error fetching YOLO classes:', error)
  }
}

// 获取记录列表
const fetchRecords = async () => {
  try {
    loading.value = true
    const response = await detectionApi.getRecords({
      page: currentPage.value,
      page_size: pageSize.value,
      search: searchQuery.value
    })
    recordList.value = response.records
    total.value = response.total
  } catch (error) {
    ElMessage.error('获取记录列表失败')
    console.error('Error fetching records:', error)
  } finally {
    loading.value = false
  }
}

// 格式化时间
const formatDateTime = (dateTime: string): string => {
  return new Date(dateTime).toLocaleString('zh-CN')
}

// 格式化日期
const formatDate = (dateTime: string): string => {
  return new Date(dateTime).toLocaleDateString('zh-CN')
}

// 格式化时间
const formatTime = (dateTime: string): string => {
  return new Date(dateTime).toLocaleTimeString('zh-CN')
}

// 获取检测结果数量
const getDetectionCount = (record: any): number => {
  try {
    const detections = JSON.parse(record.detection_result)
    return Array.isArray(detections) ? detections.length : 0
  } catch {
    return 0
  }
}

// 获取检测结果详情
const getDetections = (record: any): any[] => {
  try {
    const detections = JSON.parse(record.detection_result)
    return Array.isArray(detections) ? detections : []
  } catch {
    return []
  }
}

// 获取置信度颜色类型
const getConfidenceType = (confidence: number) => {
  if (confidence >= 0.8) return 'success'
  if (confidence >= 0.6) return 'warning'
  return 'danger'
}

// 获取置信度颜色
const getConfidenceColor = (confidence: number) => {
  if (confidence >= 0.8) return '#67c23a' // 成功
  if (confidence >= 0.6) return '#e6a23c' // 警告
  return '#f56c6c' // 危险
}

// 获取类别显示名称
const getClassDisplayName = (className: string) => {
  return yoloClasses.value[className]?.chinese_name || className
}

// 获取类别颜色
const getClassColor = (className: string) => {
  return yoloClasses.value[className]?.color || '#409eff'
}

// 获取预览图片列表
const getPreviewImages = (record: DetectionRecord) => {
  const images = []
  if (record.original_image_url) {
    images.push(record.original_image_url)
  }
  if (record.secondary_image_url) {
    images.push(record.secondary_image_url)
  }
  if (record.result_image_url) {
    images.push(record.result_image_url)
  }
  if (record.enhanced_image_url) {
    images.push(record.enhanced_image_url)
  }
  return images
}

// 搜索处理
let searchTimeout: NodeJS.Timeout | null = null
const handleSearch = () => {
  if (searchTimeout) {
    clearTimeout(searchTimeout)
  }
  searchTimeout = setTimeout(() => {
    currentPage.value = 1
    fetchRecords()
  }, 500)
}

// 清空搜索
const handleClear = () => {
  searchQuery.value = ''
  currentPage.value = 1
  fetchRecords()
}

// 分页处理
const handleSizeChange = (size: number) => {
  pageSize.value = size
  currentPage.value = 1
  fetchRecords()
}

const handleCurrentChange = (page: number) => {
  currentPage.value = page
  fetchRecords()
}

// 查看记录
const viewRecord = async (record: any) => {
  try {
    const response = await detectionApi.getRecord(record.id)
    currentRecord.value = response
    drawMode.value = false
    drawingStart.value = null
    drawingPreview.value = null
    selectedAnnotationIndex.value = null
    await normalizeReviewAnnotations(response)
    showViewDialog.value = true
  } catch (error) {
    ElMessage.error('获取记录详情失败')
  }
}

// 编辑记录
const editRecord = (record: any) => {
  currentEditId.value = record.id
  editForm.title = record.title
  editForm.description = record.description || ''
  showEditDialog.value = true
}

const handleEdit = async () => {
  if (!editFormRef.value) return
  
  const valid = await editFormRef.value.validate().catch(() => false)
  if (!valid) return

  try {
    editLoading.value = true
    await detectionApi.updateRecord(currentEditId.value, editForm)
    ElMessage.success('更新成功')
    showEditDialog.value = false
    resetEditForm()
    fetchRecords()
  } catch (error: any) {
    ElMessage.error(error.response?.data?.message || '更新失败')
  } finally {
    editLoading.value = false
  }
}

// 重置编辑表单
const resetEditForm = () => {
  Object.assign(editForm, {
    title: '',
    description: ''
  })
  editFormRef.value?.clearValidate()
}

// 删除记录
const deleteRecord = async (id: string, title: string) => {
  try {
    await ElMessageBox.confirm(`确定要删除记录"${title}"吗？这个操作不能撤销。`, '警告', {
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      type: 'warning'
    })
    
    await detectionApi.deleteRecord(id)
    ElMessage.success('删除成功')
    fetchRecords()
  } catch (error: any) {
    if (error !== 'cancel') {
      ElMessage.error(error.response?.data?.message || '删除失败')
    }
  }
}

// 配置 marked 选项
marked.setOptions({
  breaks: true, // 支持换行
  gfm: true    // 支持 GitHub 风格的 Markdown
})

// 格式化分析文本（支持完整的 Markdown 格式）
const formatAnalysisText = (text: string) => {
  if (!text) return ''
  
  try {
    // 使用 marked 渲染 Markdown
    return marked(text)
  } catch (error) {
    console.error('Markdown 渲染错误:', error)
    // 如果渲染失败，回退到基本格式处理
    return text
      .replace(/\n/g, '<br>')
      .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
      .replace(/\*(.*?)\*/g, '<em>$1</em>')
      .replace(/#{3,}\s*(.*?)$/gm, '<h4>$1</h4>')
      .replace(/#{2}\s*(.*?)$/gm, '<h3>$1</h3>')
      .replace(/#{1}\s*(.*?)$/gm, '<h2>$1</h2>')
  }
}

// 初始化
onMounted(async () => {
  await Promise.all([
    fetchRecords(),
    fetchYoloClasses()
  ])
})
</script>

<style scoped>
.batch-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
  padding: 14px 16px;
  border: 1px solid #e4e7ed;
  border-radius: 8px;
  background: #f8fafc;
}

.batch-toolbar-tip {
  margin-left: 12px;
  color: #909399;
  font-size: 13px;
}

.batch-toolbar-actions {
  display: flex;
  flex-shrink: 0;
}

.feedback-version-alert {
  margin-bottom: 16px;
}

.feedback-version-alert :deep(.el-alert__content) {
  display: grid;
  gap: 10px;
}

@media (max-width: 900px) {
  .batch-toolbar {
    align-items: flex-start;
    flex-direction: column;
  }

  .batch-toolbar-tip {
    display: block;
    margin: 6px 0 0;
  }
}

.ai-review-actions {
  display: grid;
  gap: 12px;
  margin-top: 14px;
}

.ai-suggestion-group {
  padding: 12px;
  border: 1px solid #dcdfe6;
  border-radius: 8px;
  background: #fafafa;
}

.false-positive-group {
  border-color: #f3b7b7;
  background: #fef0f0;
}

.missed-detection-group {
  border-color: #a0cfff;
  background: #ecf5ff;
}

.ai-suggestion-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 10px;
}

.annotation-workspace {
  margin-top: 16px;
  padding: 12px;
  overflow: auto;
  border: 1px solid #dcdfe6;
  border-radius: 8px;
  background: #1f2329;
  text-align: center;
}

.annotation-canvas {
  position: relative;
  display: inline-block;
  max-width: 100%;
  overflow: hidden;
  line-height: 0;
  user-select: none;
  touch-action: none;
}

.annotation-canvas.is-drawing {
  cursor: crosshair;
}

.annotation-source-image {
  display: block;
  max-width: 100%;
  max-height: 560px;
  object-fit: contain;
  pointer-events: none;
}

.annotation-box {
  position: absolute;
  box-sizing: border-box;
  min-width: 2px;
  min-height: 2px;
  padding: 0;
  border: 2px solid #20d472;
  background: rgba(32, 212, 114, 0.08);
  cursor: pointer;
}

.annotation-box.selected {
  border-color: #ffd04b;
  background: rgba(255, 208, 75, 0.15);
  box-shadow: 0 0 0 2px rgba(255, 208, 75, 0.35);
}

.annotation-box.suspected-false-positive {
  border-color: #f56c6c;
  background: rgba(245, 108, 108, 0.16);
}

.annotation-box.drawing-preview {
  border-style: dashed;
  border-color: #409eff;
  background: rgba(64, 158, 255, 0.18);
  pointer-events: none;
}

.annotation-number {
  position: absolute;
  top: -2px;
  left: -2px;
  padding: 2px 5px;
  border-radius: 0 0 5px 0;
  background: #20d472;
  color: #101418;
  font-size: 12px;
  font-weight: 700;
  line-height: 18px;
}

.suspected-false-positive .annotation-number {
  background: #f56c6c;
  color: #fff;
}

.annotation-help {
  margin-top: 10px;
  color: #c0c4cc;
  font-size: 13px;
  line-height: 1.5;
}

.ai-review-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
  margin-top: 12px;
  color: #606266;
}

:deep(.suspected-false-positive-row > td.el-table__cell) {
  background: #fef0f0 !important;
}

:deep(.selected-review-row > td.el-table__cell) {
  background: #fdf6ec !important;
}

.ai-review-json {
  margin-top: 12px;
  padding: 12px;
  overflow: auto;
  border-radius: 6px;
  background: #f5f7fa;
  color: #303133;
  font-size: 12px;
  white-space: pre-wrap;
}

.human-review-panel .review-toolbar,
.human-review-panel .review-actions {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 14px;
}

.detection-records {
  padding: 20px;
}

.page-header {
  background-color: #fff;
  padding: 20px 0;
  margin-bottom: 20px;
  box-shadow: 0 2px 12px 0 rgba(0, 0, 0, 0.1);
  border-radius: 8px;
}

.header-content {
  display: flex;
  justify-content: space-between;
  align-items: center;
  max-width: 1200px;
  margin: 0 auto;
  padding: 0 20px;
}

.header-left {
  flex: 1;
}

.page-title {
  margin: 0 0 8px 0;
  font-size: 28px;
  color: #303133;
  font-weight: bold;
}

.page-subtitle {
  font-size: 16px;
  color: #606266;
  margin-bottom: 10px;
}

.new-btn {
  margin-left: 20px;
}

.main-content {
  max-width: 1200px;
  margin: 0 auto;
}

.search-card {
  margin-bottom: 20px;
}

.search-section {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 20px;
}

.search-left {
  flex: 1;
  display: flex;
  align-items: center;
  gap: 16px;
}

.search-input {
  flex: 1;
  max-width: 400px;
}

.search-buttons {
  display: flex;
  align-items: center;
  gap: 8px;
}

.search-btn {
  min-width: 80px;
}

.clear-btn {
  min-width: 80px;
}

.search-right {
  display: flex;
  align-items: center;
  gap: 10px;
}

.stats-info {
  font-size: 14px;
  color: #606266;
}

.records-card {
  margin-bottom: 20px;
}

.records-table {
  width: 100%;
}

.title-cell {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  width: 100%;
}

.title-text {
  font-size: 16px;
  color: #303133;
  font-weight: bold;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  text-align: center;
  min-width: 0;
}

.desc-tag {
  margin-left: 10px;
}

.time-cell {
  display: flex;
  flex-direction: column;
  align-items: center;
  font-size: 14px;
  color: #606266;
}

.time-date {
  font-size: 18px;
  color: #303133;
  font-weight: bold;
}

.time-time {
  font-size: 14px;
  color: #909399;
}

.description-text {
  font-size: 14px;
  color: #606266;
  word-break: break-all;
}

.action-buttons {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  justify-content: center;
}

.action-btn {
  padding: 6px 8px;
  font-size: 12px;
}

.pagination-wrapper {
  display: flex;
  justify-content: center;
  margin-top: 20px;
  padding: 16px 0;
}

.pagination {
  width: auto;
}

/* 查看对话框样式 */
.view-dialog .el-dialog {
  max-width: 1400px;
  width: 90%;
  margin: 0 auto;
}

.view-dialog .el-dialog__body {
  padding: 20px 24px;
  max-height: 80vh;
  overflow-y: auto;
}

.record-detail {
  max-height: 75vh;
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
  padding: 18px;
  background: linear-gradient(135deg, #ffffff 0%, #f8f9fa 100%);
  border-radius: 10px;
  border: 1px solid #ebeef5;
  transition: all 0.3s ease;
  position: relative;
  overflow: hidden;
}

.info-item::before {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  width: 4px;
  height: 100%;
  background: #409eff;
  border-radius: 0 8px 8px 0;
  opacity: 0;
  transition: opacity 0.3s ease;
}

.info-item:hover {
  border-color: #c6e2ff;
  box-shadow: 0 6px 16px rgba(64, 158, 255, 0.12);
  transform: translateY(-2px);
}

.info-item:hover::before {
  opacity: 1;
}

.info-label {
  font-size: 12px;
  color: #909399;
  margin-bottom: 10px;
  font-weight: 500;
  text-transform: uppercase;
  letter-spacing: 0.8px;
}

.info-value {
  font-size: 16px;
  color: #303133;
  font-weight: 600;
  line-height: 1.5;
  word-break: break-word;
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

/* 图片对比区域 */
.result-images {
  display: flex;
  gap: 16px;
  margin: 20px 0;
  flex-wrap: wrap;
  justify-content: space-between;
}

.image-container {
  flex: 1;
  min-width: 280px;
  max-width: 32%;
}

.image-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 15px;
  padding-bottom: 10px;
  border-bottom: 1px solid #ebeef5;
}

.image-header h4 {
  margin: 0;
  color: #303133;
  font-size: 16px;
  font-weight: 600;
}

.image-header .el-tag {
  margin-left: 10px;
}

.image-wrapper {
  width: 100%;
  height: auto;
  border: 1px solid #ebeef5;
  border-radius: 8px;
  overflow: hidden;
  background: white;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
  aspect-ratio: 4/3;
  display: flex;
  align-items: center;
  justify-content: center;
}

.result-image {
  width: 100%;
  height: 100%;
  object-fit: contain;
  border-radius: 6px;
}

.image-error {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: #999;
  font-size: 14px;
}

.image-error .el-icon {
  font-size: 24px;
  margin-bottom: 8px;
}

/* 检测详情区域 */
.detections-container {
  background: white;
  border-radius: 8px;
  padding: 20px;
  border: 1px solid #e4e7ed;
}

.detections-table {
  width: 100%;
}

.detections-table .el-table-column--center .el-tag {
  margin: 0 auto;
}

.confidence-display {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 5px;
}

.confidence-progress {
  flex: 1;
  margin-right: 8px;
}

.confidence-text {
  font-size: 14px;
  color: #303133;
  font-weight: bold;
}

/* 增强图片区域 */
.enhanced-image-container {
  background: white;
  border-radius: 8px;
  padding: 20px;
  border: 1px solid #e4e7ed;
  text-align: center;
}

.enhanced-result-image {
  width: 100%;
  max-width: 500px;
  height: auto;
  border: 1px solid #ebeef5;
  border-radius: 8px;
  object-fit: cover;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
}

/* 分析结果区域 */
.analysis-container, .suggestions-container {
  background: white;
  border-radius: 8px;
  padding: 20px;
  border: 1px solid #e4e7ed;
}

.analysis-text, .suggestions-text {
  line-height: 1.8;
  color: #333;
}

/* Markdown 内容样式 */
.markdown-content {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
}

.markdown-content h1 {
  font-size: 24px;
  color: #303133;
  margin: 24px 0 16px 0;
  padding-bottom: 8px;
  border-bottom: 2px solid #409eff;
}

.markdown-content h2 {
  font-size: 20px;
  color: #409eff;
  margin: 20px 0 12px 0;
  padding-bottom: 6px;
  border-bottom: 1px solid #e4e7ed;
}

.markdown-content h3 {
  font-size: 18px;
  color: #409eff;
  margin: 16px 0 10px 0;
}

.markdown-content h4 {
  font-size: 16px;
  color: #409eff;
  margin: 14px 0 8px 0;
}

.markdown-content p {
  margin: 12px 0;
  line-height: 1.8;
}

.markdown-content ul, .markdown-content ol {
  margin: 12px 0;
  padding-left: 24px;
}

.markdown-content li {
  margin: 6px 0;
  line-height: 1.6;
}

.markdown-content blockquote {
  margin: 16px 0;
  padding: 12px 16px;
  background: #f8f9fa;
  border-left: 4px solid #409eff;
  color: #606266;
  font-style: italic;
}

.markdown-content code {
  background: #f4f4f5;
  padding: 2px 6px;
  border-radius: 3px;
  font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
  font-size: 14px;
  color: #e6a23c;
}

.markdown-content pre {
  background: #f8f9fa;
  padding: 16px;
  border-radius: 6px;
  overflow-x: auto;
  border: 1px solid #e4e7ed;
  margin: 16px 0;
}

.markdown-content pre code {
  background: none;
  padding: 0;
  color: #303133;
}

.markdown-content table {
  width: 100%;
  border-collapse: collapse;
  margin: 16px 0;
}

.markdown-content th, .markdown-content td {
  border: 1px solid #e4e7ed;
  padding: 8px 12px;
  text-align: left;
}

.markdown-content th {
  background: #f8f9fa;
  font-weight: 600;
  color: #409eff;
}

.markdown-content tr:nth-child(even) {
  background: #fafafa;
}

.info-icon {
  margin-left: 5px;
  font-size: 16px;
  color: #c0c4cc;
  cursor: help;
}

/* 进度单元格样式 */
.progress-cell {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
}

.progress-info {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 5px;
  width: 100%;
}

.mini-progress {
  width: 100%;
}

.progress-text {
  font-size: 12px;
  color: #409eff;
  font-weight: bold;
}

.completion-info {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 5px;
}

.frame-info {
  font-size: 12px;
  color: #67c23a;
  font-weight: bold;
}

.status-info {
  display: flex;
  justify-content: center;
}

/* 时间单元格样式 */
.time-cell {
  display: flex;
  flex-direction: column;
  align-items: center;
  font-size: 14px;
  color: #606266;
}

.time-date {
  font-size: 16px;
  color: #303133;
  font-weight: bold;
}

.time-time {
  font-size: 12px;
  color: #909399;
}

/* 描述内容样式 */
.description-content {
  color: #606266;
  line-height: 1.6;
  word-break: break-all;
}

/* 无类别提示样式 */
.no-classes {
  color: #909399;
  font-style: italic;
}

/* 中等屏幕适配 */
@media (max-width: 1200px) {
  .view-dialog .el-dialog {
    width: 95%;
    max-width: 1000px;
  }

  .info-grid {
    grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
  }

  .image-container {
    min-width: 250px;
  }
}

@media (max-width: 768px) {
  .header-content {
    flex-direction: column;
    align-items: flex-start;
    gap: 10px;
  }

  .new-btn {
    margin-left: 0;
  }

  .search-section {
    flex-direction: column;
    align-items: stretch;
    gap: 10px;
  }

  .search-left {
    flex-direction: column;
    gap: 10px;
  }

  .search-input {
    width: 100%;
    max-width: none;
  }

  .search-buttons {
    width: 100%;
    justify-content: center;
  }

  .search-btn,
  .clear-btn {
    flex: 1;
    min-width: auto;
  }

  .result-images {
    flex-direction: column;
  }
  
  .image-container {
    min-width: unset;
    max-width: 100%;
  }
  
  .image-wrapper {
    aspect-ratio: 16/9;
  }

  /* 移动端对话框样式优化 */
  .view-dialog .el-dialog {
    width: 95%;
    max-width: none;
    margin: 0 auto;
  }

  .view-dialog .el-dialog__body {
    padding: 15px;
    max-height: 85vh;
  }

  .record-detail {
    max-height: 80vh;
  }

  .detail-section {
    padding: 16px;
    margin-bottom: 20px;
  }

  .info-grid {
    grid-template-columns: 1fr;
    gap: 12px;
  }

  .info-item {
    padding: 12px;
  }

  .section-header {
    margin-bottom: 15px;
    padding-bottom: 8px;
  }

  .section-title {
    font-size: 16px;
  }
}

/* 简约信息文字样式 */
.info-text-primary {
  color: #409eff;
  font-weight: 600;
  font-size: 16px;
}

.info-text-success {
  color: #67c23a;
  font-weight: 600;
  font-size: 16px;
}

.info-text-warning {
  color: #e6a23c;
  font-weight: 600;
  font-size: 16px;
}

.info-text-danger {
  color: #f56c6c;
  font-weight: 600;
  font-size: 16px;
}

.info-text-info {
  color: #909399;
  font-weight: 600;
  font-size: 16px;
}

.info-text-default {
  color: #303133;
  font-weight: 600;
  font-size: 16px;
}

.info-text-time {
  color: #606266;
  font-weight: 500;
  font-size: 15px;
  line-height: 1.4;
}
</style>
