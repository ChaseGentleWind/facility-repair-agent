import { LitElement, html, css } from 'lit'
import { customElement, property } from 'lit/decorators.js'
import { repeat } from 'lit/directives/repeat.js'
import { sharedStyles } from '../styles/theme'
import type { ChatMessage } from '../types'
import './message-bubble'

@customElement('message-list')
export class MessageList extends LitElement {
  static styles = [
    sharedStyles,
    css`
      :host {
        display: block;
        flex: 1;
        overflow-y: auto;
        padding: 8px 0;
      }
      .empty {
        text-align: center;
        color: var(--ra-text-secondary);
        padding: 40px 16px;
        font-size: 13px;
      }
    `,
  ]

  @property({ attribute: false, hasChanged: () => true }) messages: ChatMessage[] = []
  @property({ type: Boolean }) streaming = false

  private _userScrolled = false

  updated() {
    if (!this._userScrolled) this._scrollToBottom()
  }

  connectedCallback() {
    super.connectedCallback()
    this.addEventListener('scroll', this._onScroll)
  }

  disconnectedCallback() {
    super.disconnectedCallback()
    this.removeEventListener('scroll', this._onScroll)
  }

  render() {
    if (this.messages.length === 0) {
      return html`<div class="empty">开始对话吧</div>`
    }
    return html`
      ${repeat(
        this.messages,
        (_, i) => i,
        (msg, i) => {
          const isLast = i === this.messages.length - 1
          const isStreamingMsg = isLast && msg.role === 'bot' && this.streaming
          return html`
            <message-bubble
              .msg=${msg}
              role=${msg.role}
              ?streaming=${isStreamingMsg}
            ></message-bubble>
          `
        },
      )}
    `
  }

  private _onScroll = () => {
    const el = this.renderRoot.querySelector(':host') ?? this
    const { scrollTop, scrollHeight, clientHeight } = el as HTMLElement
    this._userScrolled = scrollHeight - scrollTop - clientHeight > 60
  }

  private _scrollToBottom() {
    requestAnimationFrame(() => {
      this.scrollTop = this.scrollHeight
    })
  }

  scrollDown() {
    this._userScrolled = false
    this._scrollToBottom()
  }
}
