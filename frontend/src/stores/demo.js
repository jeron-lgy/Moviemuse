import { defineStore } from 'pinia'
import { api, postJson } from '../lib/api'

const fallbackCoverUrl = '/static/icons/moviemuse-app-icon-1024.png'
const cacheKey = 'moviemuseDemoSettings'

function cachedSettings() {
  if (typeof localStorage === 'undefined') return {}
  try {
    return JSON.parse(localStorage.getItem(cacheKey) || '{}')
  } catch {
    return {}
  }
}

function writeCachedSettings(settings) {
  if (typeof localStorage === 'undefined') return
  try {
    localStorage.setItem(cacheKey, JSON.stringify(settings))
  } catch {
    // Local storage is an optimization to avoid a brief real-cover flash.
  }
}

export const useDemoStore = defineStore('demo', {
  state: () => {
    const cached = cachedSettings()
    return {
      loaded: false,
      enabled: !!cached.enabled,
      coverUrl: String(cached.cover_url || cached.coverUrl || '').trim(),
      hideSystemSettings: cached.hide_system_settings !== false && cached.hideSystemSettings !== false
    }
  },
  getters: {
    replacementCoverUrl: (state) => String(state.coverUrl || '').trim() || fallbackCoverUrl
  },
  actions: {
    applySettings(settings = {}) {
      const demo = settings?.demo || settings || {}
      this.enabled = !!demo.enabled
      this.coverUrl = String(demo.cover_url || demo.coverUrl || '').trim()
      this.hideSystemSettings = demo.hide_system_settings !== false && demo.hideSystemSettings !== false
      this.loaded = true
      writeCachedSettings({
        enabled: this.enabled,
        cover_url: this.coverUrl,
        hide_system_settings: this.hideSystemSettings
      })
    },
    async load() {
      const payload = await api('/api/system-settings')
      this.applySettings(payload.settings?.demo || {})
      return this.$state
    },
    async save() {
      const payload = await postJson('/api/system-settings', {
        demo: {
          enabled: this.enabled,
          cover_url: this.coverUrl,
          hide_system_settings: this.hideSystemSettings
        }
      })
      this.applySettings(payload.settings?.demo || {})
      return this.$state
    }
  }
})
