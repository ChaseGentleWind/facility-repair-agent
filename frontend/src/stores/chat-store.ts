import type { AgentState, ChatMessage, SSEEvent, WidgetConfig } from '../types'
import { chatInit, chatMessage, uploadImage, setApiBase } from '../services/api'
import { parseSSEStream } from '../services/sse-parser'
import { compressImage } from '../services/image-compress'

const SESSION_KEY = 'repair-agent-session'

type Listener = () => void

export class ChatStore {
  sessionId: string | null = null
  messages: ChatMessage[] = []
  agentState: AgentState = 'GREETING'
  collectedFields: Record<string, string> = {}
  isStreaming = false
  isPanelOpen = false
  unreadCount = 0

  private _config!: WidgetConfig
  private _listeners = new Set<Listener>()
  private _hostElement: HTMLElement | null = null

  get config() {
    return this._config
  }

  subscribe(fn: Listener) {
    this._listeners.add(fn)
    return () => this._listeners.delete(fn)
  }

  private _notify() {
    this.messages = [...this.messages]
    for (const fn of this._listeners) fn()
  }

  setHost(el: HTMLElement) {
    this._hostElement = el
  }

  async init(config: WidgetConfig) {
    this._config = config
    setApiBase(config.apiBase)

    const saved = sessionStorage.getItem(SESSION_KEY)
    if (saved) {
      this.sessionId = saved
      this._notify()
      return
    }

    try {
      const resp = await chatInit(config.clientId)
      this.sessionId = resp.session_id
      sessionStorage.setItem(SESSION_KEY, resp.session_id)
      this.messages.push({
        role: 'bot',
        type: 'text',
        content: resp.greeting,
        timestamp: Date.now(),
      })
      this._notify()
    } catch (e) {
      console.error('[repair-agent] init failed:', e)
    }
  }

  togglePanel() {
    this.isPanelOpen = !this.isPanelOpen
    if (this.isPanelOpen) this.unreadCount = 0
    this._notify()
  }

  closePanel() {
    this.isPanelOpen = false
    this._notify()
  }

  async sendText(text: string) {
    if (!text.trim() || !this.sessionId || this.isStreaming) return

    this.messages.push({
      role: 'user',
      type: 'text',
      content: text,
      timestamp: Date.now(),
    })
    this._notify()

    await this._streamAgentReply('text', text)
  }

  async sendImage(file: File) {
    if (!this.sessionId || this.isStreaming) return

    const localUrl = URL.createObjectURL(file)
    this.messages.push({
      role: 'user',
      type: 'image',
      content: '',
      imageUrl: localUrl,
      timestamp: Date.now(),
    })
    this._notify()

    try {
      const compressed = await compressImage(file)
      const resp = await uploadImage(this.sessionId, compressed, 'photo.jpg')
      await this._streamAgentReply('image_url', '图片已上传', resp.image_url)
    } catch (e) {
      console.error('[repair-agent] image upload failed:', e)
      this.messages.push({
        role: 'bot',
        type: 'text',
        content: '图片上传失败，您可以跳过图片继续报修。',
        timestamp: Date.now(),
      })
      this._notify()
    }
  }

  private async _streamAgentReply(
    type: 'text' | 'image_url',
    content: string,
    imageUrl?: string,
  ) {
    this.isStreaming = true
    const botMsg: ChatMessage = {
      role: 'bot',
      type: 'text',
      content: '',
      timestamp: Date.now(),
    }
    this.messages.push(botMsg)
    this._notify()

    try {
      const reader = await chatMessage(this.sessionId!, type, content, imageUrl)
      await parseSSEStream(reader, (evt) => this._handleSSE(evt, botMsg))
    } catch (e: any) {
      console.error('[repair-agent] stream error:', e)
      if (e?.message?.includes('404')) {
        sessionStorage.removeItem(SESSION_KEY)
        botMsg.content = '会话已过期，正在重新连接...'
        this._notify()
        this.isStreaming = false
        await this.init(this._config)
        return
      }
      botMsg.content = '抱歉，系统暂时繁忙，请稍后重试。'
    }

    this.isStreaming = false
    if (!this.isPanelOpen) this.unreadCount++
    this._notify()
  }

  private _handleSSE(evt: SSEEvent, botMsg: ChatMessage) {
    switch (evt.type) {
      case 'text_delta':
        botMsg.content += evt.content ?? ''
        this._notify()
        break

      case 'state_update':
        if (evt.state) this.agentState = evt.state
        if (evt.collected) Object.assign(this.collectedFields, evt.collected)
        this._notify()
        break

      case 'ticket_ready':
        if (evt.ticket) {
          this._dispatchEvent('onRepairTicketGenerated', evt.ticket)
        }
        this.agentState = 'COMPLETED'
        this._notify()
        break

      case 'human_service':
        this._dispatchEvent('onRequestHumanService', {
          session_id: evt.session_id,
          reason: evt.reason,
          partial_ticket: evt.partial_ticket,
        })
        this.agentState = 'ESCALATED'
        this._notify()
        break

      case 'error':
        botMsg.content += `\n[错误] ${evt.message ?? '未知错误'}`
        this._notify()
        break

      case 'done':
        break
    }
  }

  private _dispatchEvent(name: string, detail: unknown) {
    this._hostElement?.dispatchEvent(
      new CustomEvent(name, { bubbles: true, composed: true, detail }),
    )
  }

  resetSession() {
    sessionStorage.removeItem(SESSION_KEY)
    this.sessionId = null
    this.messages = []
    this.agentState = 'GREETING'
    this.collectedFields = {}
    this.isStreaming = false
    this.unreadCount = 0
    this._notify()
    this.init(this._config)
  }
}
