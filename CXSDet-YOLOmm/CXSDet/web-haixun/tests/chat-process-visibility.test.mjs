import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createServer } from 'vite'
import { createSSRApp } from 'vue'
import { renderToString } from 'vue/server-renderer'

test('执行说明默认折叠，最终回答保持可见，原始推理不映射为消息', async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' })
  try {
    const { mapMessage } = await server.ssrLoadModule('/src/services/mappers.ts')
    const Timeline = (await server.ssrLoadModule('/src/components/ChatTimeline.vue')).default
    const messages = [mapMessage({ id: 'mid', role: 'assistant', content: '检查文件', refs: [{ type: 'execution_note' }] }),
      mapMessage({ id: 'final', role: 'assistant', content: '检测完成' })]
    const html = await renderToString(createSSRApp(Timeline, { messages, conversationKey: 's' }))
    assert.match(html, /<details class="execution-note"[^>]*><summary[^>]*>查看执行说明<\/summary>/)
    assert.doesNotMatch(html, /<details[^>]*\bopen\b/)
    assert.match(html, /<div class="message-text"[^>]*>检测完成/)
  } finally { await server.close() }
})
