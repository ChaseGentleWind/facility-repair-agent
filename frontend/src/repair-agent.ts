import { LitElement, html, css } from 'lit'
import { customElement, state } from 'lit/decorators.js'
import { sharedStyles } from './styles/theme'
import { ChatStore } from './stores/chat-store'
import type { WidgetConfig } from './types'
import { DEFAULT_CONFIG } from './types'
import './components/fab-button'
import './components/chat-panel'

@customElement('repair-agent')
export class RepairAgent extends LitElement {
  static styles = [
    sharedStyles,
    css`
      :host {
        display: block;
        position: fixed;
        z-index: 2147483647;
      }
      :host([position='bottom-right']) {
        bottom: 24px;
        right: 24px;
      }
      :host([position='bottom-left']) {
        bottom: 24px;
        left: 24px;
      }
    `,
  ]

  @state() private _store = new ChatStore()

  private _config: WidgetConfig = { ...DEFAULT_CONFIG }

  connectedCallback() {
    super.connectedCallback()
    this._readConfig()
    this._store.setHost(this)
    this._store.subscribe(() => this.requestUpdate())
    this._store.init(this._config)

    this.setAttribute('position', this._config.position)
    this.style.setProperty('--ra-primary', this._config.themeColor)
  }

  private _readConfig() {
    const raw = this.getAttribute('data-config')
    if (raw) {
      try {
        Object.assign(this._config, JSON.parse(raw))
      } catch {
        console.warn('[repair-agent] invalid data-config JSON')
      }
    }
    const script = document.querySelector('script[data-config][src*="repair-agent"]')
    if (script) {
      const scriptRaw = script.getAttribute('data-config')
      if (scriptRaw) {
        try {
          Object.assign(this._config, JSON.parse(scriptRaw))
        } catch {
          // skip
        }
      }
    }
  }

  render() {
    const st = this._store
    return html`
      <fab-button
        ?open=${st.isPanelOpen}
        .unread=${st.unreadCount}
        @fab-click=${() => st.togglePanel()}
      ></fab-button>
      <chat-panel
        ?open=${st.isPanelOpen}
        position=${this._config.position}
        .store=${st}
        @panel-close=${() => st.closePanel()}
      ></chat-panel>
    `
  }
}

function autoMount() {
  if (document.querySelector('repair-agent')) return
  const script = document.querySelector('script[data-config][src*="repair-agent"]')
  if (!script) return
  const el = document.createElement('repair-agent')
  const config = script.getAttribute('data-config')
  if (config) el.setAttribute('data-config', config)
  document.body.appendChild(el)
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', autoMount)
} else {
  autoMount()
}
