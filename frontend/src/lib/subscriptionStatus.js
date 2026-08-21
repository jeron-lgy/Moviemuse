export function normalizeAvId(value) {
  const text = String(value || '').trim()
  if (!text) return ''
  const compact = text.toUpperCase().replace(/[^A-Z0-9]/g, '')
  const match = compact.match(/^([A-Z]{2,})(\d{2,})$/)
  return match ? `${match[1]}-${match[2]}` : text.toUpperCase()
}

export function avIdentityKeys(item) {
  const keys = new Set()
  const addId = (value) => {
    const key = normalizeAvId(value)
    if (key) keys.add(`id:${key}`)
  }
  const addUrl = (value) => {
    const text = String(value || '').trim().toLowerCase()
    if (text) keys.add(`url:${text}`)
  }
  addId(item?.id)
  addId(item?.code)
  addId(item?.av_id)
  addId(item?.number)
  addUrl(item?.url)
  addUrl(item?.javdb_url)
  return keys
}

export function subscribedAv(item, subscriptions = []) {
  const target = avIdentityKeys(item)
  if (!target.size) return null
  return subscriptions.find((row) => {
    for (const key of avIdentityKeys(row)) {
      if (target.has(key)) return true
    }
    return false
  }) || null
}

export function washStatus(item) {
  const wash = item?.wash && typeof item.wash === 'object' ? item.wash : null
  return String(wash?.status || '').toLowerCase()
}

export function washActive(item) {
  return ['requested', 'downloading', 'error'].includes(washStatus(item))
}

export function subscriptionStatus(item) {
  const displayStatus = String(item?.display_status || '').toLowerCase()
  const status = String(item?.status || '').toLowerCase()
  const libraryStatus = String(item?.library_status || '').toLowerCase()
  if (displayStatus === 'washing' || washActive(item)) return 'washing'
  if (displayStatus === 'in_library') return 'in_library'
  if (displayStatus === 'subscribing') return 'subscribed'
  if ((status === 'in_library' || libraryStatus === 'in_library') && (item?.library_confirmed_at || item?.jellyfin_item_id)) return 'in_library'
  return 'subscribed'
}

export function avSubscribeLabel(item, subscriptions = [], fallback = '订阅') {
  const current = subscribedAv(item, subscriptions)
  if (!current) return fallback
  const labels = {
    washing: '洗版中',
    in_library: '已入库',
    subscribed: '已订阅'
  }
  return labels[subscriptionStatus(current)] || '已订阅'
}

export function actressIdentityKeys(item) {
  return [
    item?.id,
    item?.name,
    item?.dmm_name,
    item?.javdb_id,
    item?.javlibrary_star_id,
    item?.dmm_url,
    item?.url
  ].map((value) => String(value || '').trim().toLowerCase()).filter(Boolean)
}

export function isActressSubscribed(item, subscriptions = [], extraKeys = []) {
  const target = new Set([...actressIdentityKeys(item), ...extraKeys].filter(Boolean))
  if (!target.size) return false
  return subscriptions.some((row) => actressIdentityKeys(row).some((key) => target.has(key)))
}

export function subscribedActress(item, subscriptions = [], extraKeys = []) {
  const target = new Set([...actressIdentityKeys(item), ...extraKeys].filter(Boolean))
  if (!target.size) return null
  return subscriptions.find((row) => actressIdentityKeys(row).some((key) => target.has(key))) || null
}
