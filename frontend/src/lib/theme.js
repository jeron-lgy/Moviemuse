const STORAGE_KEY = 'moviemuse-theme'
const THEMES = ['system', 'light', 'dark']

export function normalizeTheme(value) {
  return THEMES.includes(value) ? value : 'system'
}

export function getStoredTheme() {
  if (typeof window === 'undefined') return 'system'
  return normalizeTheme(window.localStorage.getItem(STORAGE_KEY))
}

export function getEffectiveTheme(value) {
  const theme = normalizeTheme(value)
  if (theme === 'light' || theme === 'dark') return theme
  if (typeof window === 'undefined') return 'light'
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

export function applyTheme(value) {
  if (typeof document === 'undefined') return 'system'
  const theme = normalizeTheme(value)
  document.documentElement.dataset.theme = theme
  document.documentElement.style.colorScheme = getEffectiveTheme(theme)
  return theme
}

export function setStoredTheme(value) {
  const theme = normalizeTheme(value)
  window.localStorage.setItem(STORAGE_KEY, theme)
  applyTheme(theme)
  return theme
}

export function initTheme() {
  applyTheme(getStoredTheme())
}
