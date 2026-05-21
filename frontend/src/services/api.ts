import type { InitResponse, UploadResponse } from '../types'

let _apiBase = ''

export function setApiBase(base: string) {
  _apiBase = base.replace(/\/+$/, '')
}

function url(path: string): string {
  return `${_apiBase}${path}`
}

export async function chatInit(clientId: string): Promise<InitResponse> {
  const resp = await fetch(url('/api/v1/chat/init'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ client_id: clientId, metadata: { source: 'web' } }),
  })
  if (!resp.ok) throw new Error(`init failed: ${resp.status}`)
  return resp.json()
}

export async function chatMessage(
  sessionId: string,
  type: 'text' | 'image_url',
  content: string,
  imageUrl?: string,
): Promise<ReadableStreamDefaultReader<Uint8Array>> {
  const resp = await fetch(url('/api/v1/chat/message'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      session_id: sessionId,
      message: { type, content, image_url: imageUrl ?? null },
    }),
  })
  if (!resp.ok) throw new Error(`message failed: ${resp.status}`)
  return resp.body!.getReader()
}

export async function uploadImage(
  sessionId: string,
  file: Blob,
  filename: string,
): Promise<UploadResponse> {
  const form = new FormData()
  form.append('session_id', sessionId)
  form.append('file', file, filename)
  const resp = await fetch(url('/api/v1/upload/image'), {
    method: 'POST',
    body: form,
  })
  if (!resp.ok) throw new Error(`upload failed: ${resp.status}`)
  return resp.json()
}
