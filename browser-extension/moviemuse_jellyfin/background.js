const DEFAULTS = {
  apiBase: 'http://127.0.0.1:18183',
  apiKey: ''
}
const LEGACY_API_BASE = 'http://127.0.0.1:18180'

chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.sync.get(DEFAULTS, (current) => {
    chrome.storage.sync.set(normalizeSettings({ ...DEFAULTS, ...current }))
  })
})

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || message.type !== 'moviemuse:jellyfin-action') return false
  runMovieMuseAction(message)
    .then((payload) => sendResponse({ ok: true, payload }))
    .catch((error) => sendResponse({ ok: false, error: error.message || String(error) }))
  return true
})

async function runMovieMuseAction(message) {
  const settings = await getSettings()
  const endpoint = endpointForAction(message.action)
  const apiBase = String(settings.apiBase || DEFAULTS.apiBase).replace(/\/+$/, '')
  const headers = { 'Content-Type': 'application/json' }
  if (settings.apiKey) headers['X-API-Key'] = settings.apiKey
  const response = await fetch(`${apiBase}${endpoint}`, {
    method: 'POST',
    headers,
    body: JSON.stringify(message.payload || {})
  })
  const text = await response.text()
  let data = {}
  if (text) {
    try {
      data = JSON.parse(text)
    } catch {
      data = { raw: text }
    }
  }
  if (!response.ok) {
    throw new Error(data.detail || data.message || data.raw || response.statusText || 'MovieMuse 请求失败')
  }
  return data
}

function endpointForAction(action) {
  if (action === 'subtitle') return '/api/integrations/jellyfin/subtitle'
  if (action === 'transcode') return '/api/integrations/jellyfin/transcode'
  return '/api/integrations/jellyfin/resolve'
}

function getSettings() {
  return new Promise((resolve) => {
    chrome.storage.sync.get(DEFAULTS, (value) => resolve(normalizeSettings({ ...DEFAULTS, ...value })))
  })
}

function normalizeSettings(settings) {
  const apiBase = String(settings.apiBase || DEFAULTS.apiBase).replace(/\/+$/, '')
  return {
    ...settings,
    apiBase: apiBase === LEGACY_API_BASE ? DEFAULTS.apiBase : apiBase
  }
}
