const DEFAULTS = {
  apiBase: 'http://127.0.0.1:18183',
  apiKey: ''
}

const apiBase = document.getElementById('apiBase')
const apiKey = document.getElementById('apiKey')
const statusNode = document.getElementById('status')

chrome.storage.sync.get(DEFAULTS, (settings) => {
  apiBase.value = settings.apiBase || DEFAULTS.apiBase
  apiKey.value = settings.apiKey || ''
})

document.getElementById('save').addEventListener('click', () => {
  const payload = {
    apiBase: apiBase.value.trim() || DEFAULTS.apiBase,
    apiKey: apiKey.value.trim()
  }
  chrome.storage.sync.set(payload, () => {
    statusNode.textContent = '已保存'
    window.setTimeout(() => {
      statusNode.textContent = ''
    }, 1600)
  })
})
