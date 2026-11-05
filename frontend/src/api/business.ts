import { ApiError, apiUrl, http } from '@/api/http'
import type {
  AssetSummary,
  BusinessJob,
  DatasetSample,
  DatasetSummary,
  DetectionRecord,
  JobEvent,
  ModelSummary,
  RecordFiltersState,
  RecordPage,
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
): Promise<{ datasets: DatasetSummary[] }> {
  return http.get(`${project(projectId)}/datasets`)
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

export async function listModels(projectId: string): Promise<{ models: ModelSummary[] }> {
  return http.get(`${project(projectId)}/models`)
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
