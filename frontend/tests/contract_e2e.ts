/**
 * 真实前端适配器 → 真实 HTTP 后端的契约测试（由 scripts/contract_frontend.py 驱动）。
 *
 * 这里 import 的是 `frontend/src/api/business.ts` 本身，不是重写的副本：
 * 路径、方法、请求体、响应字段有任何一处不一致，这个脚本就会失败。
 * 浏览器里靠 document.cookie 取 CSRF；Node 里用一个最小 document 桩 + fetch 注入 Cookie。
 */
import { readFile } from 'node:fs/promises'

const env = process.env
const base = (env.HX17_BASE || '').replace(/\/$/, '')
const cookie = env.HX17_COOKIE || ''
const projectId = env.HX17_PROJECT || ''
const zipPath = env.HX17_ZIP || ''
const weightPath = env.HX17_WEIGHT || ''
const rgbPath = env.HX17_RGB || ''
const irPath = env.HX17_IR || ''

if (!base || !cookie || !projectId || !zipPath) {
  console.error('缺少 HX17_BASE / HX17_COOKIE / HX17_PROJECT / HX17_ZIP')
  process.exit(2)
}

interface GlobalShim {
  __HX17_API_BASE__?: string
  document?: { cookie: string }
  fetch: typeof fetch
}
const shim = globalThis as unknown as GlobalShim
shim.__HX17_API_BASE__ = `${base}/api`
shim.document = { cookie }

const nativeFetch = globalThis.fetch
globalThis.fetch = ((input: RequestInfo | URL, init: RequestInit = {}) => {
  const headers = new Headers(init.headers ?? {})
  headers.set('cookie', cookie)
  return nativeFetch(input, { ...init, headers })
}) as typeof fetch

const api = await import('@/api/business')
// 复核页真实用的取框规则：这里直接用页面同一个函数，避免“测试里另写一套”。
const { preferredBoxes } = await import('@/lib/review')

/** 取一个后端 URL 的真实字节（Cookie 由上面的 fetch 桩统一带上）。 */
async function fetchBinary(url: string): Promise<{ status: number; type: string; size: number; head: number[] }> {
  // 适配器返回的地址可能是绝对地址（`__HX17_API_BASE__` 指到别的源时），也可能是站内路径。
  const target = /^https?:\/\//.test(url) ? url : `${base}${url}`
  const response = await fetch(target)
  const buffer = Buffer.from(await response.arrayBuffer())
  return {
    status: response.status,
    type: response.headers.get('content-type') || '',
    size: buffer.length,
    head: [...buffer.subarray(0, 4)],
  }
}

const report: Record<string, unknown> = { steps: [] }
function note(step: string, data: unknown): void {
  ;(report.steps as unknown[]).push({ step, ok: true, data })
}
function fail(step: string, error: unknown): never {
  ;(report.steps as unknown[]).push({
    step,
    ok: false,
    error: error instanceof Error ? `${error.name}: ${error.message}` : String(error),
    code: (error as { code?: string })?.code ?? '',
    details: (error as { details?: unknown })?.details ?? null,
  })
  console.log(JSON.stringify(report, null, 2))
  process.exit(1)
}

try {
  const zip = await readFile(zipPath)
  const file = new File([zip], 'dataset.zip', { type: 'application/zip' })
  const asset = await api.uploadAsset(projectId, file)
  note('upload_zip', { id: asset.id, kind: asset.kind, size: asset.size, sha256: asset.sha256 })
  if (asset.kind !== 'archive') throw new Error(`上传后类型是 ${asset.kind}，不是 archive`)

  const name = `契约数据集-${Date.now()}`
  const imported = await api.importDataset(projectId, {
    name,
    asset_id: asset.id,
    modalities: ['rgb', 'ir'],
    secondary_modality: 'ir',
  })
  note('import_dataset', imported.dataset)
  if (!imported.dataset.sample_count) throw new Error('导入后的样本数为 0')

  const samples = await api.datasetSamples(projectId, imported.dataset.id)
  note('dataset_samples', {
    count: samples.samples.length,
    with_x: samples.samples.filter((item) => Boolean(item.x_rel)).length,
  })
  if (samples.samples.length !== imported.dataset.sample_count) {
    throw new Error('样本清单数量与数据集统计不一致')
  }

  const events = await api.jobEvents(projectId, { after: 0 })
  note('job_events', { count: events.events.length, last_seq: events.last_seq })
  if (typeof events.last_seq !== 'number') throw new Error('job-events 没有返回数字 last_seq')

  let modelId = ''
  if (weightPath && env.HX17_SKIP_MODEL !== '1') {
    const weight = await readFile(weightPath)
    const weightFile = new File([weight], 'best.pt', { type: 'application/octet-stream' })
    const weightAsset = await api.uploadAsset(projectId, weightFile)
    note('upload_weight', { id: weightAsset.id, kind: weightAsset.kind, size: weightAsset.size })
    if (weightAsset.kind !== 'weight') throw new Error(`权重上传后类型是 ${weightAsset.kind}`)

    const modelName = `契约模型-${Date.now()}`
    const model = await api.importModel(projectId, {
      name: modelName,
      family: 'yolomm',
      model_mode: 'multimodal',
      secondary_modality: 'ir',
      dataset_id: imported.dataset.id,
      class_map: { '0': 0, '1': 1 },
      asset_id: weightAsset.id,
      notes: '契约测试导入',
    })
    note('import_model', model.model)
    const published = await api.publishModel(projectId, model.model.id)
    note('publish_model', published.model)
    if (published.model.status !== 'published') throw new Error('发布后状态不是 published')
    modelId = published.model.id
  }

  // ---------------------------------------------------------------- 人工复核闭环
  //
  // 边界说明：检测器由 scripts/contract_frontend.py 显式注入一个固定实现
  // （只替换“权重 → 框”这一步）。适配器、HTTP、鉴权、服务层、结果图渲染、
  // 文件落盘全部是真的；这里不 mock 任何响应。
  if (modelId && rgbPath && irPath) {
    const rgbAsset = await api.uploadAsset(
      projectId,
      new File([await readFile(rgbPath)], 'rgb.jpg', { type: 'image/jpeg' }),
    )
    const irAsset = await api.uploadAsset(
      projectId,
      new File([await readFile(irPath)], 'ir.jpg', { type: 'image/jpeg' }),
    )
    note('upload_panels', { rgb: rgbAsset.kind, ir: irAsset.kind })
    if (rgbAsset.kind !== 'image' || irAsset.kind !== 'image') throw new Error('RGB/IR 上传后不是 image 资产')

    const detected = await api.detectOne(projectId, {
      model_id: modelId,
      rgb_asset_id: rgbAsset.id,
      x_asset_id: irAsset.id,
    })
    const record = detected.record
    note('detect_one', {
      id: record.id,
      status: record.status,
      boxes: record.boxes.length,
      result_url: record.result_url,
      x_asset_id: record.x_asset_id,
    })
    if (record.status !== 'done') throw new Error(`检测没有成功：${record.status}`)
    if (!record.boxes.length) throw new Error('注入的检测器没有产生框，后面的标注流程无从校验')

    // 1) 复核状态：必须是 {state: {...}}，页面读的就是它。
    const loaded = await api.getReviewState(projectId, record.id)
    if (!loaded || !loaded.state || !loaded.state.record) {
      throw new Error('GET /review 没有返回 {state.record}（前端适配器声明的形状）')
    }
    const state = loaded.state
    note('review_state', {
      record_id: state.record.id,
      queue: state.queue,
      has_x: Boolean(state.image.x_url),
      rgb_url: state.image.rgb_url,
      x_url: state.image.x_url,
      result_url: state.image.result_url,
      width: state.image.width,
      height: state.image.height,
      classes: state.class_options.map((item) => item.name),
    })
    if (!state.image.width || !state.image.height) throw new Error('复核状态没有给出原图尺寸')
    if (!state.image.x_url) throw new Error('多模态记录没有配套 IR 地址')
    if (!state.image.result_url) throw new Error('检测成功却没有结果图地址')

    // 2) 三张图必须真的能取到字节（同源 Cookie），不是空 URL。
    const panels: Record<string, { status: number; type: string; size: number; head: number[] }> = {}
    for (const [key, url] of [
      ['rgb', state.image.rgb_url],
      ['x', state.image.x_url],
      ['result', state.image.result_url],
    ] as [string, string][]) {
      const binary = await fetchBinary(url)
      panels[key] = binary
      if (binary.status !== 200) throw new Error(`${key} 图片 HTTP ${binary.status}`)
      if (!binary.type.startsWith('image/')) throw new Error(`${key} 图片类型是 ${binary.type}`)
      if (binary.size < 1024) throw new Error(`${key} 图片只有 ${binary.size} 字节`)
    }
    note('review_images', panels)

    // 3) 保存空草稿：空数组是有效结果，不能被当成“没有草稿”。
    const base0 = state.latest ? state.latest.version : 0
    const savedDraft = await api.saveReviewDraft(projectId, record.id, {
      boxes: [],
      note: '契约：把框删光',
      base_version: base0,
    })
    note('review_draft_empty', {
      version: savedDraft.state.draft?.version ?? null,
      boxes: savedDraft.state.draft?.boxes?.length ?? -1,
    })
    if (!savedDraft.state.draft) throw new Error('保存草稿后没有 draft')
    if (savedDraft.state.draft.boxes.length !== 0) throw new Error('空草稿存进去又冒出框来')

    // 4) 重新读取（等价于刷新页面）：草稿仍是空，不能回退到模型框。
    const reloaded = await api.getReviewState(projectId, record.id)
    const reloadBoxes = preferredBoxes(reloaded.state)
    note('review_reload_empty', {
      draft_boxes: reloaded.state.draft?.boxes?.length ?? null,
      preferred_boxes: reloadBoxes.length,
      submit_base: reloaded.state.latest_submitted?.version ?? 0,
    })
    if (reloadBoxes.length !== 0) throw new Error('刷新后旧框复活了')

    // 5) 确认无缺陷：版本落地，草稿清空。
    const submitted = await api.submitReview(projectId, record.id, {
      boxes: [],
      outcome: 'no_defect',
      note: '契约：确认无缺陷',
      base_version: reloaded.state.latest_submitted?.version ?? 0,
      request_key: `contract-review:${record.id}`,
    })
    note('review_submit', {
      version: submitted.version.version,
      status: submitted.version.status,
      outcome: submitted.version.outcome,
      boxes: submitted.version.boxes.length,
      created: submitted.created,
    })
    if (submitted.version.outcome !== 'no_defect') throw new Error('提交后 outcome 不是 no_defect')
    if (submitted.version.status !== 'confirmed') throw new Error('提交后状态不是 confirmed')

    // 6) 再进一次（等价于重新打开页面）：无缺陷结论不能被模型框覆盖。
    const afterSubmit = await api.getReviewState(projectId, record.id)
    const afterBoxes = preferredBoxes(afterSubmit.state)
    note('review_after_submit', {
      human_status: afterSubmit.state.human_status,
      draft: afterSubmit.state.draft,
      preferred_boxes: afterBoxes.length,
      queue: afterSubmit.state.queue,
    })
    if (afterSubmit.state.draft) throw new Error('提交后草稿应当清空')
    if (afterBoxes.length !== 0) throw new Error('确认无缺陷后重新进入又出现旧框')

    // 7) 反馈：只有生效结论才能入池。
    const feedback = await api.addFeedback(projectId, record.id, '契约：加入回流池')
    const feedbackList = await api.listFeedback(projectId)
    note('feedback', {
      id: feedback.feedback.id,
      created: feedback.created,
      pending: feedbackList.feedback.length,
    })
    if (!feedbackList.feedback.some((item) => item.id === feedback.feedback.id)) {
      throw new Error('加入反馈后列表里找不到它')
    }

    // 8) 身份检查（只读）。
    const report = await api.datasetReport(projectId, imported.dataset.id)
    const matched = await api.matchRecords(projectId, imported.dataset.id, [record.id])
    note('identity', {
      duplicate_groups: report.duplicate_groups?.length ?? 0,
      cross_split_leakage: report.cross_split_leakage?.length ?? 0,
      annotation_conflicts: report.annotation_conflicts?.length ?? 0,
      matches: matched.matches.map((item) => ({ record_id: item.record_id, status: item.status })),
    })
    if (matched.matches.length !== 1) throw new Error('matchRecords 没有返回这条记录的匹配结果')

    // 9) 回流草稿 → 预览 → 定稿 → 导出 ZIP，全走真实 HTTP。
    const draftName = `回流草稿-${Date.now()}`
    const savedPlan = await api.saveReflowDraft(projectId, {
      name: draftName,
      mode: 'revision',
      dataset_id: imported.dataset.id,
      selections: [{ record_id: record.id, action: 'replace' }],
      strategy: 'even',
      seed: 11,
    })
    note('reflow_draft', {
      id: savedPlan.draft.id,
      status: savedPlan.draft.status,
      error: savedPlan.draft.error?.code ?? null,
      counts: savedPlan.draft.plan?.counts ?? null,
    })
    if (savedPlan.draft.status !== 'ready') {
      throw new Error(`回流草稿不是 ready：${savedPlan.draft.status} ${savedPlan.draft.error?.message ?? ''}`)
    }

    const preview = await api.reflowPreview(projectId, {
      mode: 'revision',
      dataset_id: imported.dataset.id,
      selections: [{ record_id: record.id, action: 'replace' }],
      strategy: 'even',
      seed: 11,
    })
    note('reflow_preview', { counts: preview.counts, items: preview.items.length })
    if (!preview.counts || preview.counts.conflicts) throw new Error('回流预览有冲突')

    const finalized = await api.finalizeReflowDraft(projectId, savedPlan.draft.id)
    note('reflow_finalize', {
      id: finalized.dataset.id,
      kind: finalized.dataset.kind,
      parent: finalized.dataset.parent_dataset_id,
      samples: finalized.dataset.sample_count,
      export_url: finalized.dataset.export_url,
      created: finalized.created,
    })
    if (!finalized.dataset.export_url) throw new Error('定稿后没有导出包地址')

    const exported = await fetchBinary(api.datasetExportUrl(projectId, finalized.dataset.id))
    note('reflow_export', exported)
    if (exported.status !== 200) throw new Error(`导出包 HTTP ${exported.status}`)
    if (exported.head[0] !== 0x50 || exported.head[1] !== 0x4b) throw new Error('导出包不是 ZIP')
    if (exported.size < 1024) throw new Error('导出包太小')

    const newSamples = await api.datasetSamples(projectId, finalized.dataset.id)
    note('reflow_samples', {
      count: newSamples.samples.length,
      with_x: newSamples.samples.filter((item) => Boolean(item.x_rel)).length,
    })
    if (!newSamples.samples.length) throw new Error('定稿后的数据集没有样本')

    // 10) 复核队列：确认后应从待复核移出。
    const queue = await api.listReviewQueue(projectId, { view: 'confirmed' })
    note('review_queue', { view: queue.view, counts: queue.counts })
    if ((queue.counts.confirmed ?? 0) < 1) throw new Error('已确认队列里没有刚提交的记录')
  }

  const listed = await api.listDatasets(projectId)
  note('list_datasets', { count: listed.datasets.length })
  const models = await api.listModels(projectId)
  note('list_models', { count: models.models.length })
  console.log(JSON.stringify(report, null, 2))
} catch (error) {
  fail('contract', error)
}
