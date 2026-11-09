/**
 * 顶层弹窗栈：只用它判断 Esc 应该关谁。
 *
 * 为什么不用 DOM 的 z-index/event.target：弹窗内容通常把焦点放在输入框里，
 * keydown 目标不是弹窗本身；而多个弹窗嵌套时，Esc 只能关最上面那层。
 */
const stack: number[] = []
let nextId = 1

export function registerDialog(): number {
  const id = nextId++
  stack.push(id)
  return id
}

export function unregisterDialog(id: number): void {
  const index = stack.lastIndexOf(id)
  if (index >= 0) stack.splice(index, 1)
}

export function isTopDialog(id: number): boolean {
  return stack[stack.length - 1] === id
}

export function hasOpenDialogs(): boolean {
  return stack.length > 0
}