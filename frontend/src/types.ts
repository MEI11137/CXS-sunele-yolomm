export interface ApiEnvelope<T> {
  code: number
  msg: string
  error?: string
  data: T
}

export interface HealthData {
  status: string
  service: string
  version: string
  stage: string
  checked_at: string
}

export type FeatureFlags = Record<string, boolean>

export interface MetaData {
  service: string
  version: string
  stage: string
  serve_dist: boolean
  features: FeatureFlags
  initialized: boolean
  auth: string
}

export type UserRole = 'admin' | 'member'
export type UserStatus = 'active' | 'disabled'

export interface User {
  id: number
  username: string
  display_name: string
  role: UserRole
  status: UserStatus
  created_at: string
  updated_at: string
}

export interface Project {
  id: string
  name: string
  description: string
  root_path: string | null
  root_bound_at: string | null
  status: 'active' | 'deleted'
  created_at: string
  updated_at: string
  deleted_at: string | null
}

export interface ChatSession {
  id: string
  project_id: string
  title: string
  status: 'active' | 'deleted'
  created_at: string
  updated_at: string
  deleted_at: string | null
}

export type ChatRole = 'user' | 'assistant' | 'system'

export interface ChatMessage {
  id: number
  session_id: string
  role: ChatRole
  content: string
  meta: Record<string, unknown>
  created_at: string
}

export interface DirectoryEntry {
  name: string
  path: string
  is_dir: boolean
  size: number
  is_link: boolean
}

export interface BrowseResult {
  path: string | null
  base: string | null
  entries: DirectoryEntry[]
  blocked: number
  roots?: string[]
}

export interface RootGrant {
  user_id: number
  username: string
  display_name: string
  granted_at: string
}

export interface AllowedRoot {
  id: number
  path: string
  label: string
  created_at: string
  /** 管理员列表里才有；普通用户只看得到自己获授的目录。 */
  grants?: RootGrant[]
}

export interface ProviderPreset {
  id: string
  label: string
  base_url: string
  models: string[]
  note: string
}

export interface CapabilityEvidence {
  step?: string
  status?: number
  outcome?: string
  latency_ms?: number
  finish_reason?: string | null
  /** 固定说明，不是上游原文（上游可能回显凭据）。 */
  note?: string | null
  tool_calls?: boolean
  [key: string]: unknown
}

export type AuthState =
  | 'verified'
  | 'rejected'
  | 'payment_required'
  | 'rate_limited'
  | 'upstream_error'
  | 'network_error'
  | 'unverified'
  | 'superseded'

export interface Capabilities {
  auth?: boolean
  chat?: boolean
  vision?: boolean
  tools?: boolean
  auth_state?: AuthState | string
  checked_at?: string
  evidence?: Record<string, CapabilityEvidence>
  [key: string]: unknown
}

export interface ModelConnection {
  provider: string
  base_url: string
  model: string
  display_name: string
  key_hint: string
  key_set: boolean
  capabilities: Capabilities
  capabilities_stale: boolean
  checked_at: string | null
  updated_at: string
  summary?: string
  /** check 期间配置被改动时为 false，结论已丢弃。 */
  applied?: boolean
}

export interface AuditEntry {
  id: number
  at: string
  actor_id: number | null
  action: string
  target: string
  detail: string
}
