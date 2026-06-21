const DEFAULTS = {
  apiBase: 'http://127.0.0.1:18183',
  apiKey: ''
}
const LEGACY_API_BASE = 'http://127.0.0.1:18180'

const apiBase = document.getElementById('apiBase')
const apiKey = document.getElementById('apiKey')
const statusNode = document.getElementById('status')
const contextNode = document.getElementById('context')
const subtitleButton = document.getElementById('subtitle')
const transcodeButton = document.getElementById('transcode')

let currentContext = null

init()

async function init() {
  const settings = await getSettings()
  apiBase.value = settings.apiBase
  apiKey.value = settings.apiKey || ''
  currentContext = await activeJellyfinContext()
  renderContext()
  subtitleButton.addEventListener('click', () => sendAction('subtitle'))
  transcodeButton.addEventListener('click', () => sendAction('transcode'))
  document.getElementById('save').addEventListener('click', saveSettings)
}

function renderContext() {
  const ready = Boolean(currentContext?.itemId)
  subtitleButton.disabled = !ready
  transcodeButton.disabled = !ready
  contextNode.textContent = ready
    ? `${currentContext.title || 'Jellyfin 媒体'} · ${currentContext.itemId.slice(0, 8)}`
    : '请在 Jellyfin 媒体详情页使用'
}

async function sendAction(action) {
  if (!currentContext?.itemId) return
  setBusy(true, action === 'subtitle' ? '正在发送字幕任务...' : '正在发送转码任务...')
  try {
    await saveSettings(false)
    const response = await chrome.runtime.sendMessage({
      type: 'moviemuse:jellyfin-action',
      action,
      payload: {
        item_id: currentContext.itemId,
        title: currentContext.title
      }
    })
    if (!response?.ok) throw new Error(response?.error || 'MovieMuse 请求失败')
    const payload = response.payload || {}
    const id = payload.job_id || payload.job?.id || payload.result?.job_id || ''
    const suffix = id ? `：${id.slice(0, 8)}` : ''
    setStatus(action === 'subtitle' ? `字幕任务已加入队列${suffix}` : `转码任务已加入队列${suffix}`, 'ok')
  } catch (error) {
    setStatus(friendlyError(error), 'error')
  } finally {
    setBusy(false)
  }
}

async function saveSettings(showStatus = true) {
  const payload = {
    apiBase: (apiBase.value.trim() || DEFAULTS.apiBase).replace(/\/+$/, ''),
    apiKey: apiKey.value.trim()
  }
  await chrome.storage.sync.set(payload)
  if (showStatus) setStatus('API 已保存', 'ok')
}

async function activeJellyfinContext() {
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true })
  const tab = tabs[0]
  if (!tab?.url) return null
  const itemId = readItemId(tab.url)
  if (!itemId || !looksLikeJellyfinTab(tab)) return null
  return {
    itemId,
    title: String(tab.title || '').replace(/\s*[-|].*$/, '').trim(),
    pageUrl: tab.url
  }
}

function readItemId(tabUrl) {
  const url = new URL(tabUrl)
  const hashUrl = parseHashUrl(url.hash)
  return firstValue(
    url.searchParams.get('id'),
    url.searchParams.get('itemId'),
    hashUrl.searchParams.get('id'),
    hashUrl.searchParams.get('itemId'),
    matchFromText(tabUrl, /[?&](?:id|itemId)=([a-fA-F0-9]{8,})/)
  )
}

function parseHashUrl(hash) {
  const clean = String(hash || '').replace(/^#!?\/?/, '')
  try {
    return new URL(`http://jellyfin.local/${clean}`)
  } catch {
    return new URL('http://jellyfin.local/')
  }
}

function looksLikeJellyfinTab(tab) {
  const text = `${tab.url || ''} ${tab.title || ''}`.toLowerCase()
  return text.includes('jellyfin') || text.includes('itemdetails') || text.includes('details')
}

function getSettings() {
  return new Promise((resolve) => {
    chrome.storage.sync.get(DEFAULTS, (value) => {
      const apiBaseValue = String(value.apiBase || DEFAULTS.apiBase).replace(/\/+$/, '')
      resolve({
        ...DEFAULTS,
        ...value,
        apiBase: apiBaseValue === LEGACY_API_BASE ? DEFAULTS.apiBase : apiBaseValue
      })
    })
  })
}

function setBusy(busy, message = '') {
  subtitleButton.disabled = busy || !currentContext?.itemId
  transcodeButton.disabled = busy || !currentContext?.itemId
  if (message) setStatus(message)
}

function setStatus(message, tone = '') {
  statusNode.textContent = message || ''
  statusNode.dataset.tone = tone
}

function firstValue(...values) {
  return values.map((value) => String(value || '').trim()).find(Boolean) || ''
}

function matchFromText(value, pattern) {
  const match = String(value || '').match(pattern)
  return match ? match[1] : ''
}

function friendlyError(error) {
  const message = error?.message || String(error)
  if (message.includes('未配置 Jellyfin 地址')) {
    return '18183 的 MovieMuse 未配置 Jellyfin 地址，订阅库未命中时无法解析路径'
  }
  if (message.includes('未配置 Jellyfin API Key')) {
    return '18183 的 MovieMuse 未配置 Jellyfin API Key，订阅库未命中时无法解析路径'
  }
  if (message.includes('Internal Server Error')) {
    return '18183 后端内部错误，通常是服务还没重启到最新代码或后端日志有异常'
  }
  return message
}
