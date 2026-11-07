import { ApiError, apiUrl, http } from '@/api/http'
import type {
  AssetSummary,
  BusinessJob,
  DatasetReport,
  DatasetSample,
  DatasetSummary,
  DetectionRecord,
  FeedbackSummary,
  JobEvent,
  ModelSummary,
  RecordFiltersState,
  RecordMatch,
  RecordPage,
  ReflowDraft,
  ReflowPlan,
  ReviewState,
  DocumentSearchResult,
  DocumentStats,
  DocumentWindow,
  ManualDetail,
  ManualSummary,
  McpCallAudit,
  McpCallResult,
  McpDiscoverResult,
  McpServer,
  ProjectDocument,
  ReviewVersion,
  TrainingComparison,
  TrainingDeviceCapabilities,
  TrainingPlan,
  TrainingRun,
} from '@/types'

/**
 * 业务接口层。
 *
 * 这里只做“路径 + 类型”的收敛，不做任何乐观更新：所有展示都以服务端返回为准，
 * 失败就抛给上层显示真实原因（尤其是计费与越权相关的能力）。
 */

const project = (projectId: string) => `/projects/${encodeURIComponent(projectId)}`

// --------------------------------------------------------------------- 资产

export async function listAssets(
  projectId: string,
  params: { kind?: string; role?: string; limit?: number; before?: string } = {},
): Promise<{ assets: AssetSummary[]; has_more: boolean }> {
  const query = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== '') query.set(key, String(value))
  }
  const suffix = query.toString() ? `?${query.toString()}` : ''
  return http.get(`/projects/${encodeURIComponent(projectId)}/assets${suffix}`)
}

export async function listCandidates(
  projectId: string,
  path = '.',
): Promise<{ path: string; entries: { rel_path: string; name: string; kind: string; size: number }[] }> {
  return http.get(
    `/projects/${encodeURIComponent(projectId)}/assets/candidates?path=${encodeURIComponent(path)}`,
  )
}

export async function registerAsset(
  projectId: string,
  relPath: string,
  role?: string,
): Promise<{ asset: AssetSummary }> {
  return http.post(`/projects/${encodeURIComponent(projectId)}/assets/register`, {
    rel_path: relPath,
    role: role ?? null,
  })
}

/**
 * 上传：必须走 multipart，且**只**这个端点允许 multipart（后端有白名单）。
 * 这里不用 http 封装，因为它固定按 JSON 设置 Content-Type 与 CSRF 头。
 */
export async function uploadAsset(
  projectId: string,
  file: File,
  options: { role?: string; sessionId?: string } = {},
): Promise<AssetSummary> {
  const form = new FormData()
  form.append('file', file, file.name)
  if (options.role) form.append('role', options.role)
  if (options.sessionId) form.append('session_id', options.sessionId)
  const token = readCsrf()
  const response = await fetch(apiUrl(`${project(projectId)}/assets/upload`), {
    method: 'POST',
    body: form,
    credentials: 'same-origin',
    headers: token ? { 'X-CSRF-Token': token, Accept: 'application/json' } : { Accept: 'application/json' },
  })
  let payload: { code?: number; msg?: string; error?: string; data?: { asset: AssetSummary } } | null = null
  try {
    payload = await response.json()
  } catch {
    payload = null
  }
  if (!response.ok || !payload || payload.code !== 0 || !payload.data) {
    throw new ApiError(response.status, payload?.msg || `上传失败（HTTP ${response.status}）`, payload?.error ?? '')
  }
  return payload.data.asset
}

function readCsrf(): string {
  for (const part of document.cookie.split(';')) {
    const item = part.trim()
    if (item.startsWith('hx17_csrf=')) return decodeURIComponent(item.slice('hx17_csrf='.length))
  }
  return ''
}

// --------------------------------------------------------------------- 数据集

export async function listDatasets(
  projectId: string,
  deleted = 'exclude',
): Promise<{ datasets: DatasetSummary[] }> {
  return http.get(`${project(projectId)}/datasets?deleted=${encodeURIComponent(deleted)}`)
}

export async function importDataset(
  projectId: string,
  payload: {
    name: string
    rel_path?: string
    asset_id?: string
    modalities?: string[]
    secondary_modality?: string | null
    classes?: string[] | null
  },
): Promise<{ dataset: DatasetSummary }> {
  return http.post(`${project(projectId)}/datasets`, payload)
}

export async function datasetSamples(
  projectId: string,
  datasetId: string,
  split?: string,
): Promise<{ samples: DatasetSample[] }> {
  const suffix = split ? `?split=${encodeURIComponent(split)}` : ''
  return http.get(`${project(projectId)}/datasets/${encodeURIComponent(datasetId)}/samples${suffix}`)
}

// --------------------------------------------------------------------- 模型

export async function listModels(
  projectId: string,
  includeDeleted = false,
): Promise<{ models: ModelSummary[] }> {
  const suffix = includeDeleted ? '?include_deleted=1' : ''
  return http.get(`${project(projectId)}/models${suffix}`)
}

export async function importModel(
  projectId: string,
  payload: {
    name: string
    family: 'yolo' | 'yolomm'
    model_mode?: string
    secondary_modality?: string | null
    dataset_id?: string | null
    class_map?: Record<string, number> | null
    rel_path?: string
    asset_id?: string
    notes?: string
  },
): Promise<{ model: ModelSummary }> {
  return http.post(`${project(projectId)}/models`, payload)
}

export async function publishModel(
  projectId: string,
  modelId: string,
): Promise<{ model: ModelSummary }> {
  return http.post(`${project(projectId)}/models/${encodeURIComponent(modelId)}/publish`, {})
}

export async function unpublishModel(
  projectId: string,
  modelId: string,
): Promise<{ model: ModelSummary }> {
  return http.post(`${project(projectId)}/models/${encodeURIComponent(modelId)}/unpublish`, {})
}

export async function patchModel(
  projectId: string,
  modelId: string,
  payload: { name?: string; notes?: string },
): Promise<{ model: ModelSummary }> {
  return http.patch(`${project(projectId)}/models/${encodeURIComponent(modelId)}`, payload)
}

export async function deleteModel(
  projectId: string,
  modelId: string,
): Promise<{ model: ModelSummary }> {
  return http.del(`${project(projectId)}/models/${encodeURIComponent(modelId)}`)
}

export async function restoreModel(
  projectId: string,
  modelId: string,
): Promise<{ model: ModelSummary }> {
  return http.post(`${project(projectId)}/models/${encodeURIComponent(modelId)}/restore`, {})
}

// --------------------------------------------------------------------- 训练

export async function listTraining(
  projectId: string,
  limit = 30,
): Promise<{ runs: TrainingRun[]; lock: { held: boolean; run_id: string; project_id: string } }> {
  return http.get(`${project(projectId)}/training?limit=${limit}`)
}

export async function planTraining(
  projectId: string,
  payload: {
    dataset_id: string
    params?: Record<string, number | string | boolean>
    parent_model_id?: string | null
    name?: string
  },
): Promise<TrainingPlan> {
  return http.post(`${project(projectId)}/training/plan`, payload)
}

export async function startTraining(
  projectId: string,
  payload: {
    dataset_id: string
    request_key: string
    params?: Record<string, number | string | boolean>
    parent_model_id?: string | null
    name?: string
  },
): Promise<{ run: TrainingRun; created: boolean }> {
  return http.post(`${project(projectId)}/training`, payload)
}

export async function getTrainingRun(
  projectId: string,
  runId: string,
): Promise<{ run: TrainingRun }> {
  return http.get(`${project(projectId)}/training/${encodeURIComponent(runId)}`)
}

export async function trainingLog(
  projectId: string,
  runId: string,
  offset = 0,
): Promise<{ text: string; next: number; size: number; eof: boolean }> {
  return http.get(`${project(projectId)}/training/${encodeURIComponent(runId)}/log?offset=${offset}`)
}

export async function cancelTraining(
  projectId: string,
  runId: string,
): Promise<{ run: TrainingRun }> {
  return http.post(`${project(projectId)}/training/${encodeURIComponent(runId)}/cancel`, {})
}

export async function compareTraining(
  projectId: string,
  runId: string,
): Promise<TrainingComparison> {
  return http.get(`${project(projectId)}/training/${encodeURIComponent(runId)}/compare`)
}

/** 训练设备真实能力：CPU 恒可用；GPU 看部署机探测结果（不写死 CPU）。 */
export async function trainingDevice(projectId: string): Promise<TrainingDeviceCapabilities> {
  return http.get(`${project(projectId)}/training/device`)
}

// --------------------------------------------------------------------- 记录

export async function listRecords(
  projectId: string,
  filters: Partial<RecordFiltersState> & {
    session_id?: string
    limit?: number
    before?: string
  } = {},
): Promise<RecordPage> {
  const query = new URLSearchParams()
  for (const [key, value] of Object.entries(filters)) {
    if (value === undefined || value === null || value === '') continue
    query.set(key, String(value))
  }
  const suffix = query.toString() ? `?${query.toString()}` : ''
  return http.get(`${project(projectId)}/detections${suffix}`)
}

export async function getRecord(projectId: string, recordId: string): Promise<{ record: DetectionRecord }> {
  return http.get(`${project(projectId)}/detections/${encodeURIComponent(recordId)}`)
}

export async function detectOne(
  projectId: string,
  payload: { model_id: string; rgb_asset_id: string; x_asset_id?: string | null; confidence?: number },
): Promise<{ record: DetectionRecord }> {
  return http.post(`${project(projectId)}/detections`, payload)
}

export async function reviewRecords(
  projectId: string,
  recordIds: string[],
): Promise<{ job: BusinessJob; created: boolean }> {
  return http.post(`${project(projectId)}/detections/review`, { record_ids: recordIds })
}

// --------------------------------------------------------------------- 作业

export async function listJobs(
  projectId: string,
  params: { session_id?: string; limit?: number } = {},
): Promise<{ jobs: BusinessJob[] }> {
  const query = new URLSearchParams()
  if (params.session_id) query.set('session_id', params.session_id)
  if (params.limit) query.set('limit', String(params.limit))
  const suffix = query.toString() ? `?${query.toString()}` : ''
  return http.get(`${project(projectId)}/jobs${suffix}`)
}

export async function jobEvents(
  projectId: string,
  params: { after: number; session_id?: string },
): Promise<{ events: JobEvent[]; last_seq: number }> {
  const query = new URLSearchParams({ after: String(params.after) })
  if (params.session_id) query.set('session_id', params.session_id)
  return http.get(`${project(projectId)}/job-events?${query.toString()}`)
}

// --------------------------------------------------------------------- 人工复核

export interface ReviewBoxPayload {
  class_id: number
  bbox: [number, number, number, number]
  severity?: string
  source?: string
}

export async function listReviewQueue(
  projectId: string,
  params: { view?: string; record_ids?: string[]; limit?: number } = {},
): Promise<{
  view: string
  items: DetectionRecord[]
  counts: Record<string, number>
  flow_ids: string[]
}> {
  const query = new URLSearchParams()
  if (params.view) query.set('view', params.view)
  if (params.record_ids?.length) query.set('record_ids', params.record_ids.join(','))
  if (params.limit) query.set('limit', String(params.limit))
  return http.get(`${project(projectId)}/reviews?${query.toString()}`)
}

export async function getReviewState(projectId: string, recordId: string): Promise<{ state: ReviewState }> {
  return http.get(`${project(projectId)}/detections/${encodeURIComponent(recordId)}/review`)
}

export async function saveReviewDraft(
  projectId: string,
  recordId: string,
  payload: {
    boxes: ReviewBoxPayload[]
    /** 辅模态框：各自原图像素坐标；不传保留已存值，显式 [] 才清空。 */
    x_boxes?: ReviewBoxPayload[]
    note?: string
    base_version: number
  },
): Promise<{ state: ReviewState }> {
  return http.post(`${project(projectId)}/detections/${encodeURIComponent(recordId)}/review/draft`, payload)
}

export async function submitReview(
  projectId: string,
  recordId: string,
  payload: {
    boxes: ReviewBoxPayload[]
    x_boxes?: ReviewBoxPayload[]
    outcome: 'defect' | 'no_defect'
    note?: string
    base_version: number
    request_key: string
    session_id?: string
  },
): Promise<{ version: ReviewVersion; created: boolean }> {
  return http.post(`${project(projectId)}/detections/${encodeURIComponent(recordId)}/review/submit`, payload)
}

export interface ReviewEvent {
  record_id: string
  version: number
  status: 'draft' | 'confirmed' | 'rejected'
  outcome: 'defect' | 'no_defect' | null
  updated_at: string
}

/** Agent/其他页面改草稿后的只读投影；after 传上次收到的 updated_at，避免重复刷新。 */
export interface ReviewProjection {
  id: string
  record_id: string
  version: number
  status: 'draft' | 'confirmed' | 'rejected'
  outcome: 'defect' | 'no_defect' | null
  updated_at: string
}

export async function getReviewEvents(
  projectId: string,
  params: {
    after?: string
    after_version?: number
    record_id?: string
    limit?: number
    latest?: boolean
  } = {},
): Promise<{
  events: ReviewEvent[]
  last: string | null
  last_version?: number | null
  projection?: ReviewProjection | null
}> {
  const query = new URLSearchParams()
  if (params.after) query.set('after', params.after)
  if (typeof params.after_version === 'number') query.set('after_version', String(params.after_version))
  if (params.record_id) query.set('record_id', params.record_id)
  if (params.limit) query.set('limit', String(params.limit))
  if (params.latest) query.set('latest', '1')
  const suffix = query.toString()
  return http.get(`${project(projectId)}/review-events${suffix ? `?${suffix}` : ''}`)
}

export async function rejectReview(
  projectId: string,
  recordId: string,
  payload: { reason?: string; base_version: number; request_key: string; session_id?: string },
): Promise<{ version: ReviewVersion; created: boolean }> {
  return http.post(`${project(projectId)}/detections/${encodeURIComponent(recordId)}/review/reject`, payload)
}

export async function reopenReview(projectId: string, recordId: string): Promise<{ state: ReviewState }> {
  return http.post(`${project(projectId)}/detections/${encodeURIComponent(recordId)}/review/reopen`, {})
}

// --------------------------------------------------------------------- 记录操作

export async function deleteRecords(
  projectId: string,
  recordIds: string[],
): Promise<{ items: { record_id: string; status: string; error?: string }[]; deleted: number }> {
  return http.post(`${project(projectId)}/detections/delete`, { record_ids: recordIds })
}

export async function restoreRecords(
  projectId: string,
  recordIds: string[],
): Promise<{ items: { record_id: string; status: string }[]; restored: number }> {
  return http.post(`${project(projectId)}/detections/restore`, { record_ids: recordIds })
}

export async function addFeedback(
  projectId: string,
  recordId: string,
  note = '',
): Promise<{ feedback: FeedbackSummary; created: boolean }> {
  return http.post(`${project(projectId)}/detections/${encodeURIComponent(recordId)}/feedback`, {
    note,
    request_key: `fbk:${recordId}:${Date.now()}`,
  })
}

export async function listFeedback(
  projectId: string,
  status = 'pending',
): Promise<{ feedback: FeedbackSummary[] }> {
  return http.get(`${project(projectId)}/feedback?status=${encodeURIComponent(status)}`)
}

// --------------------------------------------------------------------- 数据集检查与回流

export async function datasetReport(projectId: string, datasetId: string): Promise<DatasetReport> {
  return http.get(`${project(projectId)}/datasets/${encodeURIComponent(datasetId)}/report`)
}

export async function matchRecords(
  projectId: string,
  datasetId: string,
  recordIds: string[],
): Promise<{ matches: RecordMatch[] }> {
  return http.post(`${project(projectId)}/datasets/${encodeURIComponent(datasetId)}/match`, {
    record_ids: recordIds,
  })
}

export async function reflowPreview(
  projectId: string,
  payload: {
    mode: 'revision' | 'new'
    dataset_id?: string | null
    selections: {
      record_id: string
      action: string
      split?: string
      review_version_id?: string
      target_sample_id?: string
    }[]
    strategy?: 'even' | 'ratio' | 'manual'
    ratios?: Record<string, number>
    seed?: number
    reference_modality?: 'rgb' | 'ir' | 'edge' | 'depth' | null
  },
): Promise<ReflowPlan> {
  return http.post(`${project(projectId)}/reflow/preview`, payload)
}

export async function reflowApply(
  projectId: string,
  payload: {
    mode: 'revision' | 'new'
    dataset_id?: string | null
    selections: {
      record_id: string
      action: string
      split?: string
      review_version_id?: string
      target_sample_id?: string
    }[]
    name?: string
    strategy?: 'even' | 'ratio' | 'manual'
    ratios?: Record<string, number>
    seed?: number
    request_key: string
    session_id?: string
    reference_modality?: 'rgb' | 'ir' | 'edge' | 'depth' | null
  },
): Promise<{ dataset: DatasetSummary }> {
  return http.post(`${project(projectId)}/reflow/apply`, payload)
}

// --------------------------------------------------------------------- 回流草稿 / 定稿 / 重新划分

export async function listReflowDrafts(
  projectId: string,
  includeDeleted = false,
): Promise<{ drafts: ReflowDraft[] }> {
  return http.get(
    `${project(projectId)}/reflow/drafts${includeDeleted ? '?include_deleted=1' : ''}`,
  )
}

export async function saveReflowDraft(
  projectId: string,
  payload: {
    draft_id?: string
    name: string
    mode: 'revision' | 'new'
    dataset_id?: string | null
    selections: { record_id: string; action: string; split?: string; review_version_id?: string; target_sample_id?: string }[]
    strategy: 'even' | 'ratio' | 'manual'
    ratios?: Record<string, number>
    seed: number
    session_id?: string
    reference_modality?: 'rgb' | 'ir' | 'edge' | 'depth' | null
  },
): Promise<{ draft: ReflowDraft }> {
  return http.post(`${project(projectId)}/reflow/drafts`, payload)
}

export async function finalizeReflowDraft(
  projectId: string,
  draftId: string,
): Promise<{ dataset: DatasetSummary; created: boolean; note?: string }> {
  return http.post(`${project(projectId)}/reflow/drafts/${encodeURIComponent(draftId)}/finalize`, {})
}

export async function deleteReflowDraft(projectId: string, draftId: string): Promise<{ status: string }> {
  return http.post(`${project(projectId)}/reflow/drafts/${encodeURIComponent(draftId)}/delete`, {})
}

export async function restoreReflowDraft(projectId: string, draftId: string): Promise<{ status: string }> {
  return http.post(`${project(projectId)}/reflow/drafts/${encodeURIComponent(draftId)}/restore`, {})
}

export async function resplitDataset(
  projectId: string,
  datasetId: string,
  payload: { strategy: 'even' | 'ratio'; ratios?: Record<string, number>; seed: number; request_key: string; session_id?: string },
): Promise<{ dataset: DatasetSummary }> {
  return http.post(`${project(projectId)}/datasets/${encodeURIComponent(datasetId)}/resplit`, payload)
}

export async function deleteDataset(projectId: string, datasetId: string): Promise<{ status: string }> {
  return http.post(`${project(projectId)}/datasets/${encodeURIComponent(datasetId)}/delete`, {})
}

export async function restoreDataset(projectId: string, datasetId: string): Promise<{ status: string }> {
  return http.post(`${project(projectId)}/datasets/${encodeURIComponent(datasetId)}/restore`, {})
}

/**
 * 数据集导出包地址。
 *
 * 下载用 `<a href download>`，不经过 XHR；地址必须和其余接口走同一个 base，
 * 否则嵌入式宿主（`__HX17_API_BASE__` 指到别的源）会下到一个不存在的 `/api`。
 */
export function datasetExportUrl(projectId: string, datasetId: string): string {
  return apiUrl(`${project(projectId)}/datasets/${encodeURIComponent(datasetId)}/export`)
}

/**
 * 识别记录 CSV 导出地址：只支持 scope=selected（明确 ID 清单）。
 * 「导出当前显示」传页面现有记录，「导出所选」传勾选的记录——范围与文件内容一致，
 * 不做含糊的整项目导出。下载走 `<a href download>`，不经过 XHR。
 */
export function recordsExportUrl(projectId: string, recordIds: string[]): string {
  const query = new URLSearchParams({ scope: 'selected', ids: recordIds.join(',') })
  return apiUrl(`${project(projectId)}/detections/export?${query.toString()}`)
}

// --------------------------------------------------------------------- 资料与手册

export async function listDocuments(
  projectId: string,
  options: { includeDeleted?: boolean } = {},
): Promise<{ documents: ProjectDocument[]; stats: DocumentStats }> {
  const suffix = options.includeDeleted ? '?include_deleted=1' : ''
  return http.get(`${project(projectId)}/documents${suffix}`)
}

export async function uploadDocument(
  projectId: string,
  file: File,
  name?: string,
): Promise<ProjectDocument> {
  const form = new FormData()
  form.append('file', file, file.name)
  if (name) form.append('name', name)
  const token = readCsrf()
  const response = await fetch(apiUrl(`${project(projectId)}/documents`), {
    method: 'POST',
    body: form,
    credentials: 'same-origin',
    headers: token ? { 'X-CSRF-Token': token, Accept: 'application/json' } : { Accept: 'application/json' },
  })
  let payload: { code?: number; msg?: string; error?: string; data?: { document: ProjectDocument } } | null = null
  try {
    payload = await response.json()
  } catch {
    payload = null
  }
  if (!response.ok || !payload || payload.code !== 0 || !payload.data) {
    throw new ApiError(response.status, payload?.msg || `上传失败（HTTP ${response.status}）`, payload?.error ?? '')
  }
  return payload.data.document
}

export async function searchDocuments(
  projectId: string,
  query: string,
  documentId?: string,
): Promise<DocumentSearchResult> {
  const params = new URLSearchParams({ q: query })
  if (documentId) params.set('document_id', documentId)
  return http.get(`${project(projectId)}/documents/search?${params.toString()}`)
}

export async function documentPages(
  projectId: string,
  documentId: string,
  page = 1,
  span = 0,
): Promise<DocumentWindow> {
  return http.get(
    `${project(projectId)}/documents/${encodeURIComponent(documentId)}/pages?page=${page}&span=${span}`,
  )
}

export async function deleteDocument(projectId: string, documentId: string): Promise<{ document: ProjectDocument }> {
  return http.del(`${project(projectId)}/documents/${encodeURIComponent(documentId)}`)
}

export async function restoreDocument(projectId: string, documentId: string): Promise<{ document: ProjectDocument }> {
  return http.post(`${project(projectId)}/documents/${encodeURIComponent(documentId)}/restore`, {})
}

/** 下载走 `<a href download>`，与其余接口共用同一个 base，避免嵌入宿主下打到错误的源。 */
export function documentDownloadUrl(projectId: string, documentId: string): string {
  return apiUrl(`${project(projectId)}/documents/${encodeURIComponent(documentId)}/download`)
}

export async function listManuals(projectId: string): Promise<{ manuals: ManualSummary[] }> {
  return http.get(`${project(projectId)}/manuals`)
}

export async function readManual(projectId: string, manualId: string): Promise<ManualDetail> {
  return http.get(`${project(projectId)}/manuals/${encodeURIComponent(manualId)}`)
}

export async function createManual(
  projectId: string,
  payload: { name: string; content: string; description?: string },
): Promise<ManualDetail> {
  return http.post(`${project(projectId)}/manuals`, payload)
}

export async function addManualVersion(
  projectId: string,
  manualId: string,
  payload: { content: string; note?: string },
): Promise<ManualDetail> {
  return http.post(`${project(projectId)}/manuals/${encodeURIComponent(manualId)}/versions`, payload)
}

export async function activateManualVersion(
  projectId: string,
  manualId: string,
  versionId: string,
): Promise<ManualDetail> {
  return http.post(
    `${project(projectId)}/manuals/${encodeURIComponent(manualId)}/versions/${encodeURIComponent(versionId)}/activate`,
    {},
  )
}

export async function deleteManual(projectId: string, manualId: string): Promise<{ status: string; id: string }> {
  return http.del(`${project(projectId)}/manuals/${encodeURIComponent(manualId)}`)
}

// --------------------------------------------------------------------- 外部 MCP

export async function listMcpServers(projectId: string): Promise<{ servers: McpServer[]; note: string }> {
  return http.get(`${project(projectId)}/mcp`)
}

export interface McpServerInput {
  name: string
  transport: 'stdio' | 'http'
  command?: string
  args?: string[]
  cwd_rel?: string
  url?: string
  env?: Record<string, string>
  headers?: Record<string, string>
  enabled?: boolean
}

export async function createMcpServer(
  projectId: string,
  payload: McpServerInput,
): Promise<{ server: McpServer }> {
  return http.post(`${project(projectId)}/mcp`, payload)
}

export async function updateMcpServer(
  projectId: string,
  serverId: string,
  payload: Partial<McpServerInput>,
): Promise<{ server: McpServer }> {
  return http.patch(`${project(projectId)}/mcp/${encodeURIComponent(serverId)}`, payload)
}

export async function deleteMcpServer(
  projectId: string,
  serverId: string,
): Promise<{ status: string; id: string }> {
  return http.del(`${project(projectId)}/mcp/${encodeURIComponent(serverId)}`)
}

export async function discoverMcpServer(
  projectId: string,
  serverId: string,
): Promise<McpDiscoverResult> {
  return http.post(`${project(projectId)}/mcp/${encodeURIComponent(serverId)}/discover`, {})
}

export async function authorizeMcpTool(
  projectId: string,
  serverId: string,
  tool: string,
  decision: 'allow' | 'deny' | 'revoke',
): Promise<{ tool: McpServer['tools'][number] }> {
  return http.post(`${project(projectId)}/mcp/${encodeURIComponent(serverId)}/authorize`, {
    tool,
    decision,
  })
}

export async function callMcpTool(
  projectId: string,
  serverId: string,
  tool: string,
  args: Record<string, unknown>,
): Promise<McpCallResult> {
  return http.post(`${project(projectId)}/mcp/${encodeURIComponent(serverId)}/call`, {
    tool,
    arguments: args,
  })
}

export async function listMcpCalls(
  projectId: string,
  serverId: string,
): Promise<{ calls: McpCallAudit[] }> {
  return http.get(`${project(projectId)}/mcp/${encodeURIComponent(serverId)}/calls`)
}
