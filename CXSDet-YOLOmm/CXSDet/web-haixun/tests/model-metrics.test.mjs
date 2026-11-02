import assert from 'node:assert/strict'
import { before, after, test } from 'node:test'
import { createServer } from 'vite'
import { fileURLToPath } from 'node:url'
let server, mappers
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('../',import.meta.url)), server:{middlewareMode:true,hmr:false}, appType:'custom' })
  mappers = await server.ssrLoadModule('/src/services/mappers.ts')
})
after(async () => { await server?.close() })
test('父子对比读取实际后端 metrics 结构并保留未知值', () => {
  const rows = mappers.mapComparisonRows({metrics:{mAP50:{parent:.8, child:.9, delta:.1, status:'known'}, recall:{parent:null,child:.7,delta:null,status:'unknown'}}})
  assert.deepEqual(rows[0], {label:'mAP50',parent:'0.800',child:'0.900',delta:'0.100'})
  assert.deepEqual(rows[3], {label:'recall',parent:'—',child:'0.700',delta:'—'})
})
test('模型部分指标缺失不能显示为零', () => {
  const model = mappers.mapModel({metrics_json:JSON.stringify({mAP50:0, 'mAP50-95':.6})})
  assert.equal(model.metrics.mAP50,0)
  assert.equal(model.metrics.mAP5095,.6)
  assert.equal(model.metrics.recall,null)
  assert.equal(mappers.formatMetric(model.metrics.recall),'—')
})
