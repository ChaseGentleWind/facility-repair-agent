export interface WidgetConfig {
  apiBase: string
  clientId: string
  position: 'bottom-right' | 'bottom-left'
  themeColor: string
  /** ASR WebSocket 端点。
   *  - 留空（默认）：用同源相对路径 /api/v1/asr/stream
   *  - 联调期可填绝对地址，如 ws://localhost:8001/ws/asr 指向沙箱 voice-asr-lab
   *  - 不想启用 WS ASR：填 'disabled'，前端只走浏览器原生 SpeechRecognition 降级 */
  asrWsUrl: string
  /** ASR 健康检查 URL，留空则跳过探针直接尝试连 WS。
   *  推荐填 health 路径以提前发现服务不可用，避免 mic 按下后才发现要降级。 */
  asrHealthUrl: string
}

export const DEFAULT_CONFIG: WidgetConfig = {
  apiBase: '',
  clientId: 'default',
  position: 'bottom-right',
  themeColor: '#1677ff',
  asrWsUrl: '',
  asrHealthUrl: '',
}

export interface ChatMessage {
  role: 'user' | 'bot'
  type: 'text' | 'image' | 'confirm_card'
  content: string
  imageUrl?: string
  draftConfirm?: DraftConfirm
  timestamp: number
}

export interface DraftConfirm {
  location?: {
    estate?: string | null
    building?: string | null
    floor?: string | null
    area?: string | null
    room?: string | null
  }
  description?: string | null
  visit_time?: string | null
  image_urls?: string[]
  fault_type?: {
    code?: string | null
    displayName?: string | null
  }
  repair_priority?: string | null
  repair_type?: string | null
}

export type AgentState =
  | 'GREETING'
  | 'COLLECTING'
  | 'WAITING_IMAGE'
  | 'CONFIRMING'
  | 'PREVIEW_READY'
  | 'SUBMITTED'
  | 'COMPLETED'
  | 'ESCALATED'

export interface SSEEvent {
  type: 'text_delta' | 'state_update' | 'draft_confirm' | 'ticket_ready' | 'human_service' | 'error' | 'done'
  content?: string
  state?: AgentState
  collected?: Record<string, any>
  draft?: DraftConfirm
  ticket?: Record<string, unknown>
  session_id?: string
  partial_ticket?: Record<string, unknown>
  reason?: string
  code?: string
  message?: string
}

export interface InitResponse {
  session_id: string
  greeting: string
  expires_in: number
}

export interface UploadResponse {
  image_url: string
  file_size: number
}

export interface SubmitTicketResponse {
  success: boolean
  ticket_id: string
  message?: string
}
