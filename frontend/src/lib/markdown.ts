/**
 * Markdown 渲染：marked 解析 + DOMPurify 净化后再交给模板。
 *
 * 模型输出属于不可信内容：即使里面写了 <script>、onerror、javascript: 链接，
 * 也必须先净化再展示。这里**只有**经过 sanitize 的字符串才允许进 v-html。
 */

import DOMPurify from 'dompurify'
import { marked } from 'marked'

marked.setOptions({ gfm: true, breaks: true })

const ALLOWED_TAGS = [
  'p', 'br', 'hr', 'strong', 'em', 'del', 'code', 'pre',
  'blockquote', 'ul', 'ol', 'li', 'a',
  'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
  'table', 'thead', 'tbody', 'tr', 'th', 'td',
]

const ALLOWED_ATTR = ['href', 'title', 'target', 'rel', 'class']

DOMPurify.addHook('afterSanitizeAttributes', (node) => {
  if (node.tagName === 'A') {
    // 外链一律新窗口打开，并掐断 window.opener。
    node.setAttribute('target', '_blank')
    node.setAttribute('rel', 'noopener noreferrer nofollow')
  }
})

export function renderMarkdown(text: string): string {
  const source = (text ?? '').toString()
  if (!source.trim()) return ''
  const html = marked.parse(source, { async: false }) as string
  return DOMPurify.sanitize(html, {
    ALLOWED_TAGS,
    ALLOWED_ATTR,
    // 图片、iframe、表单、内联样式一律不允许：助手只输出文字结论。
    FORBID_TAGS: ['img', 'iframe', 'object', 'embed', 'form', 'input', 'button', 'style', 'script'],
    FORBID_ATTR: ['style', 'src', 'srcset', 'onerror', 'onload'],
    ALLOW_DATA_ATTR: false,
  })
}
