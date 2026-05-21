const MAX_DIMENSION = 1920
const JPEG_QUALITY = 0.85

export async function compressImage(file: File): Promise<Blob> {
  const bitmap = await createImageBitmap(file)
  let { width, height } = bitmap

  if (width > MAX_DIMENSION || height > MAX_DIMENSION) {
    const ratio = Math.min(MAX_DIMENSION / width, MAX_DIMENSION / height)
    width = Math.round(width * ratio)
    height = Math.round(height * ratio)
  }

  const canvas = new OffscreenCanvas(width, height)
  const ctx = canvas.getContext('2d')!
  ctx.drawImage(bitmap, 0, 0, width, height)
  bitmap.close()

  return canvas.convertToBlob({ type: 'image/jpeg', quality: JPEG_QUALITY })
}
