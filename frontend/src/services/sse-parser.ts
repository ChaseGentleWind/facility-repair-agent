import type { SSEEvent } from '../types'

export type SSECallback = (event: SSEEvent) => void

export async function parseSSEStream(
  reader: ReadableStreamDefaultReader<Uint8Array>,
  onEvent: SSECallback,
): Promise<void> {
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break

    buffer += decoder.decode(value, { stream: true })
    const parts = buffer.split('\n\n')
    buffer = parts.pop() ?? ''

    for (const part of parts) {
      for (const line of part.split('\n')) {
        if (!line.startsWith('data:')) continue
        const raw = line.slice(5).trim()
        if (!raw) continue
        try {
          const event: SSEEvent = JSON.parse(raw)
          onEvent(event)
        } catch {
          // skip malformed JSON
        }
      }
    }
  }

  if (buffer.trim()) {
    for (const line of buffer.split('\n')) {
      if (!line.startsWith('data:')) continue
      const raw = line.slice(5).trim()
      if (!raw) continue
      try {
        onEvent(JSON.parse(raw))
      } catch {
        // skip
      }
    }
  }
}
