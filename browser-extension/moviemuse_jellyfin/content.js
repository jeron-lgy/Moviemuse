const PANEL_ID = 'moviemuse_jellyfin-panel'
let lastItemId = ''
let busyAction = ''

init()

function init() {
  renderIfNeeded()
  window.addEventListener('popstate', delayedRender)
  window.addEventListener('hashchange', delayedRender)
  const observer = new MutationObserver(delayedRender)
  observer.observe(document.documentElement, { childList: true, subtree: true })
}

function delayedRender() {
  window.clearTimeout(delayedRender.timer)
  delayedRender.timer = window.setTimeout(renderIfNeeded, 250)
}

function renderIfNeeded() {
  const context = readJellyfinContext()
  const existing = document.getElementById(PANEL_ID)
  if (!context.itemId) {
    if (existing) existing.remove()
    lastItemId = ''
    return
  }
  if (existing && lastItemId === context.itemId) return
  if (existing) existing.remove()
  lastItemId = context.itemId
  const panel = createPanel(context)
  document.body.appendChild(panel)
  resolveContext(context)
}

function readJellyfinContext() {
  const url = new URL(window.location.href)
  const hashUrl = parseHashUrl(url.hash)
  const itemId = firstValue(
    url.searchParams.get('id'),
    url.searchParams.get('itemId'),
    hashUrl.searchParams.get('id'),
    hashUrl.searchParams.get('itemId'),
    matchFromText(window.location.href, /[?&](?:id|itemId)=([a-fA-F0-9]{8,})/)
  )
  if (!itemId || !looksLikeJellyfin()) return { itemId: '' }
  return {
    itemId,
    title: readTitle(),
    pageUrl: window.location.href
  }
}

function parseHashUrl(hash) {
  const clean = String(hash || '').replace(/^#!?\/?/, '')
  try {
    return new URL(`http://jellyfin.local/${clean}`)
  } catch {
    return new URL('http://jellyfin.local/')
  }
}

function looksLikeJellyfin() {
  const marker = document.querySelector('meta[name="application-name"], .skinHeader, .detailPageWrapper, [data-role="page"]')
  const text = `${document.title} ${document.documentElement.innerHTML.slice(0, 3000)}`.toLowerCase()
  return Boolean(marker) || text.includes('jellyfin')
}

function readTitle() {
  const selectors = [
    '.itemName',
    '.detailPagePrimaryContainer h1',
    '.itemDetailsGroup h1',
    'h1'
  ]
  for (const selector of selectors) {
    const value = document.querySelector(selector)?.textContent?.trim()
    if (value) return value
  }
  return document.title.replace(/\s*[-|].*$/, '').trim()
}

function createPanel(context) {
  const panel = document.createElement('section')
  panel.id = PANEL_ID
  panel.innerHTML = `
    <div class="mmjf-title">
      <strong>MovieMuse</strong>
      <span>${escapeHtml(context.title || 'Jellyfin 媒体')}</span>
    </div>
    <div class="mmjf-actions">
      <button type="button" data-action="subtitle">发送字幕</button>
      <button type="button" data-action="transcode">发送转码</button>
    </div>
    <div class="mmjf-message" role="status"></div>
  `
  panel.addEventListener('click', (event) => {
    const button = event.target.closest('button[data-action]')
    if (!button) return
    sendAction(button.dataset.action, context)
  })
  return panel
}

async function resolveContext(context) {
  setPanelState('', '正在识别媒体...')
  try {
    const response = await chrome.runtime.sendMessage({
      type: 'moviemuse:jellyfin-action',
      action: 'resolve',
      payload: {
        item_id: context.itemId,
        title: context.title
      }
    })
    if (!response?.ok) throw new Error(response?.error || 'MovieMuse 识别失败')
    if (context.itemId !== lastItemId) return
    const media = response.payload?.media || {}
    const source = media.source === 'subscription_db' ? '订阅库' : 'Jellyfin'
    const path = media.path ? shortPath(media.path) : '未返回路径'
    setPanelState('', `${source}：${path}`, 'ok')
  } catch (error) {
    if (context.itemId !== lastItemId) return
    setPanelState('', friendlyError(error), 'error')
  }
}

async function sendAction(action, context) {
  if (busyAction) return
  busyAction = action
  setPanelState(action, action === 'subtitle' ? '正在发送字幕任务...' : '正在发送转码任务...')
  try {
    const response = await chrome.runtime.sendMessage({
      type: 'moviemuse:jellyfin-action',
      action,
      payload: {
        item_id: context.itemId,
        title: context.title
      }
    })
    if (!response?.ok) throw new Error(response?.error || 'MovieMuse 请求失败')
    const payload = response.payload || {}
    const id = payload.job_id || payload.job?.id || payload.result?.job_id || ''
    const suffix = id ? `：${id.slice(0, 8)}` : ''
    setPanelState('', action === 'subtitle' ? `字幕任务已加入队列${suffix}` : `转码任务已加入队列${suffix}`, 'ok')
  } catch (error) {
    setPanelState('', friendlyError(error), 'error')
  } finally {
    busyAction = ''
  }
}

function setPanelState(action, message, tone = '') {
  const panel = document.getElementById(PANEL_ID)
  if (!panel) return
  panel.querySelectorAll('button[data-action]').forEach((button) => {
    button.disabled = Boolean(action)
    button.classList.toggle('is-loading', button.dataset.action === action)
  })
  const messageNode = panel.querySelector('.mmjf-message')
  messageNode.textContent = message || ''
  messageNode.dataset.tone = tone
}

function firstValue(...values) {
  return values.map((value) => String(value || '').trim()).find(Boolean) || ''
}

function matchFromText(value, pattern) {
  const match = String(value || '').match(pattern)
  return match ? match[1] : ''
}

function shortPath(value) {
  const text = String(value || '')
  if (text.length <= 68) return text
  return `...${text.slice(-65)}`
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

function escapeHtml(value) {
  return String(value || '').replace(/[&<>"']/g, (char) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;'
  })[char])
}
