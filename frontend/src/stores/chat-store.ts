import type { AgentState, ChatMessage, SSEEvent, WidgetConfig } from '../types'
import { chatInit, chatMessage, uploadImage, submitTicket, setApiBase } from '../services/api'
import { parseSSEStream } from '../services/sse-parser'
import { compressImage } from '../services/image-compress'

const SESSION_KEY = 'repair-agent-session'
const DEFAULT_GREETING = '您好！我是设施报修小助手，请问您遇到了什么问题？（您可以直接描述故障，比如"A栋3楼空调不制冷"）'

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
  private _lastUploadedImageUrl: string | null = null

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
      if (this.messages.length === 0) {
        this.messages.push({
          role: 'bot',
          type: 'text',
          content: DEFAULT_GREETING,
          timestamp: Date.now(),
        })
      }
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

  async sendImage(file: File, text?: string, _retried = false) {
    if (!this.sessionId || this.isStreaming) return

    const localUrl = URL.createObjectURL(file)
    if (!_retried) {
      this.messages.push({
        role: 'user',
        type: 'image',
        content: text || '',
        imageUrl: localUrl,
        timestamp: Date.now(),
      })
      this._notify()
    }

    try {
      const compressed = await compressImage(file)
      const resp = await uploadImage(this.sessionId, compressed, 'photo.jpg')
      this._lastUploadedImageUrl = resp.image_url
      await this._streamAgentReply('image_url', text || '图片已上传', resp.image_url)
    } catch (e: any) {
      console.error('[repair-agent] image upload failed:', e)
      if ((e?.message?.includes('400') || e?.message?.includes('404')) && !_retried) {
        sessionStorage.removeItem(SESSION_KEY)
        this.sessionId = null
        try {
          const resp = await chatInit(this._config.clientId)
          this.sessionId = resp.session_id
          sessionStorage.setItem(SESSION_KEY, resp.session_id)
          await this.sendImage(file, text, true)
        } catch {
          this.messages.push({
            role: 'bot',
            type: 'text',
            content: '连接失败，请刷新页面重试。',
            timestamp: Date.now(),
          })
          this._notify()
        }
        return
      }
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
    _retried = false,
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
      if (e?.message?.includes('404') && !_retried) {
        const idx = this.messages.indexOf(botMsg)
        if (idx >= 0) this.messages.splice(idx, 1)
        sessionStorage.removeItem(SESSION_KEY)
        this.sessionId = null
        this.isStreaming = false
        try {
          const resp = await chatInit(this._config.clientId)
          this.sessionId = resp.session_id
          sessionStorage.setItem(SESSION_KEY, resp.session_id)
          await this._streamAgentReply(type, content, imageUrl, true)
        } catch {
          this.messages.push({
            role: 'bot',
            type: 'text',
            content: '连接失败，请刷新页面重试。',
            timestamp: Date.now(),
          })
          this._notify()
        }
        return
      }
      botMsg.content = '抱歉，系统暂时繁忙，请稍后重试。'
    }

    if (!botMsg.content.trim()) {
      botMsg.content = '抱歉，响应异常，请重试。'
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
        if (evt.state) {
          this.agentState = evt.state
          if (evt.state === 'CONFIRMING' && this._lastUploadedImageUrl) {
            // 必须替换对象引用，Lit 才能检测到 .msg 变化并重新渲染 bubble
            const idx = this.messages.indexOf(botMsg)
            if (idx >= 0) {
              const updated = { ...botMsg, imageUrl: this._lastUploadedImageUrl }
              this.messages[idx] = updated
              Object.assign(botMsg, updated)
            }
          }
        }
        if (evt.collected) Object.assign(this.collectedFields, evt.collected)
        this._notify()
        break

      case 'ticket_ready':
        if (evt.ticket) {
          console.log('[repair-agent] ticket_ready received, dispatching event', evt.ticket)
          this._dispatchEvent('onRepairTicketGenerated', evt.ticket)
        }
        this.agentState = 'PREVIEW_READY'
        this._notify()
        break

      case 'human_service':
        this._dispatchEvent('onRequestHumanService', {
          session_id: evt.session_id,
          reason: evt.reason,
          partial_ticket: evt.partial_ticket,
        })
        this.agentState = 'ESCALATED'
        if (!botMsg.content.trim()) {
          botMsg.content = '好的，正在为您转接人工客服，请稍候。'
        }
        this._notify()
        break

      case 'error':
        // 处理 BUSY 错误：后端正在处理中，提示用户稍后再试
        if (evt.code === 'BUSY') {
          botMsg.content = '正在处理您的上一条消息，请稍后再试。'
        } else {
          botMsg.content += `\n[错误] ${evt.message ?? '未知错误'}`
        }
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

  async submitTicket() {
    if (!this.sessionId) {
      console.error('[chat-store] submitTicket: no session')
      return
    }
    if (this.agentState !== 'PREVIEW_READY') {
      console.error('[chat-store] submitTicket: state is not PREVIEW_READY')
      return
    }

    try {
      const response = await submitTicket(this.sessionId)
      if (response.success) {
        this.agentState = 'SUBMITTED'
        this.messages.push({
          role: 'bot',
          type: 'text',
          content: `工单已成功提交！工单号：${response.ticket_id}`,
          timestamp: Date.now(),
        })
        this._notify()
      }
    } catch (err) {
      console.error('[chat-store] submitTicket error:', err)
      this.messages.push({
        role: 'bot',
        type: 'text',
        content: '提交失败，请稍后重试',
        timestamp: Date.now(),
      })
      this._notify()
    }
  }

  resetSession() {
    sessionStorage.removeItem(SESSION_KEY)
    this.sessionId = null
    this.messages = []
    this.agentState = 'GREETING'
    this.collectedFields = {}
    this.isStreaming = false
    this.unreadCount = 0
    this._lastUploadedImageUrl = null
    this._notify()
    this.init(this._config)
  }
}
