export interface WidgetConfig {
  apiBase: string
  clientId: string
  position: 'bottom-right' | 'bottom-left'
  themeColor: string
}

export const DEFAULT_CONFIG: WidgetConfig = {
  apiBase: '',
  clientId: 'default',
  position: 'bottom-right',
  themeColor: '#1677ff',
}

export interface ChatMessage {
  role: 'user' | 'bot'
  type: 'text' | 'image'
  content: string
  imageUrl?: string
  timestamp: number
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
  type: 'text_delta' | 'state_update' | 'ticket_ready' | 'human_service' | 'error' | 'done'
  content?: string
  state?: AgentState
  collected?: Record<string, string>
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
