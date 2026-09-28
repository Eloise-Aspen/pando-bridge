import type { ComponentType, ReactNode } from 'react'
import type { Frame } from '../chat/reducer'

export type HostApi = {
  navigate(view: string): void
  dispatch(frame: Frame): void
  getSessionId(): string | null
}
export type SendPayload = { text: string; attachments?: string[]; voice_mode?: boolean }
export type Message = { id: string; type: string; text: string; sessionId: string | null }

// 九类挂点均由宿主掌握布局与调用时机；插件只交组件、数据或纯处理函数。
export type RouteContribution = {
  id: string; label: string; icon?: ComponentType; Component: ComponentType<{ api: HostApi }>
} // 画导航和切到该 route 时挂载
export type SettingsContribution = {
  id: string; title: string; Component: ComponentType<{ api: HostApi }>
} // 设置页对应插槽渲染时挂载
export type StatusCardContribution = {
  id: string; slot: 'status.metric' | 'status.action'; Component: ComponentType<{ api: HostApi }>
} // 状态页渲染对应卡片区时调用
export type ComposerActionContribution = {
  id: string; label: string; Icon?: ComponentType; run(api: HostApi): void
} // 输入区动作按钮点击时调用
export type MessageActionContribution = {
  id: string; label: string; Icon?: ComponentType; run(message: Message, api: HostApi): void
} // 消息行工具栏点击时调用
export type TopbarBadgeContribution = {
  id: string; render(api: HostApi): ReactNode
} // 顶栏渲染时取徽标内容
export type MemoryDecoratorContribution = {
  id: string; tags?(item: unknown): string[]; Header?: ComponentType<{ api: HostApi }>
  CardAddon?: ComponentType<{ item: unknown; api: HostApi }>
} // 记忆页渲染页头或卡片时调用
export type MessageRendererContribution = {
  type: string; Component: ComponentType<{ message: Message; api: HostApi }>
} // 消息列表遇到对应类型时调用
export type EventContribution = {
  id: string; onFrame?(frame: Frame, api: HostApi): void
  beforeSend?(payload: SendPayload, api: HostApi): SendPayload
  onEvent?(event: { type: string; payload: unknown }, api: HostApi): void
} // 收帧、发送前和领域事件分发时调用

export type FrontendPlugin = {
  id: string
  routes?: RouteContribution[]
  settings?: SettingsContribution[]
  statusCards?: StatusCardContribution[]
  composerActions?: ComposerActionContribution[]
  messageActions?: MessageActionContribution[]
  topbarBadges?: TopbarBadgeContribution[]
  memoryDecorators?: MemoryDecoratorContribution[]
  messageRenderers?: MessageRendererContribution[]
  events?: EventContribution[]
}
