import { useDemoStore } from '../stores/demo'

function itemId(item) {
  return item?.id || item?.code || item?.name || ''
}

function itemReleased(item) {
  const value = String(item?.release_date || item?.date || '').slice(0, 10)
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false
  const release = new Date(`${value}T00:00:00`)
  const today = new Date()
  today.setHours(0, 0, 0, 0)
  return release <= today
}

export function imageProxyUrl(url, item = null, options = {}) {
  const demoUrl = demoImageUrl(item, options)
  if (demoUrl) return demoUrl
  if (options.kind === 'cover' || (!options.kind && item)) {
    const existing = item?.cover_proxy || item?.coverProxy
    if (existing && (!url || sameProxySource(existing, url))) return existing
  }
  if (!url) return ''
  if (url.startsWith('/api/proxy/image') || url.startsWith('data:')) return url
  const params = new URLSearchParams({ url })
  const kind = options.kind || (item ? 'cover' : 'image')
  if (kind) params.set('kind', kind)
  const avId = options.avId || itemId(item)
  if (avId) params.set('av_id', avId)
  if (options.entityId) params.set('entity_id', options.entityId)
  const immutable = options.immutable ?? itemReleased(item)
  if (immutable) params.set('immutable', 'true')
  return `/api/proxy/image?${params.toString()}`
}

export function mediaProxyUrl(url, item = null, options = {}) {
  if (!url) return ''
  if (url.startsWith('/api/proxy/media') || url.startsWith('data:')) return url
  if (!directMediaUrl(url)) return url
  const params = new URLSearchParams({ url })
  const avId = options.avId || itemId(item)
  if (avId) params.set('av_id', avId)
  if (options.entityId) params.set('entity_id', options.entityId)
  const immutable = options.immutable ?? itemReleased(item)
  if (immutable) params.set('immutable', 'true')
  return `/api/proxy/media?${params.toString()}`
}

function sameProxySource(proxyUrl, sourceUrl) {
  try {
    const parsed = new URL(proxyUrl, window.location.origin)
    return parsed.searchParams.get('url') === sourceUrl
  } catch {
    return proxyUrl.includes(encodeURIComponent(sourceUrl))
  }
}

function directMediaUrl(url) {
  return /\.(mp4|webm|ogg|mov)(\?|#|$)/i.test(String(url || ''))
}

function demoImageUrl(item, options = {}) {
  let demo
  try {
    demo = useDemoStore()
  } catch {
    return ''
  }
  if (!demo.enabled) return ''
  const kind = String(options.kind || (item ? 'cover' : 'image')).toLowerCase()
  if (!['cover', 'actor', 'image'].includes(kind)) return ''
  return demo.replacementCoverUrl
}
