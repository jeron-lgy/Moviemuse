<template>
  <div class="mm-theme-toggle" role="group" aria-label="主题切换">
    <button
      v-for="item in options"
      :key="item.value"
      type="button"
      :class="{ active: theme === item.value }"
      :aria-pressed="theme === item.value"
      :title="item.label"
      @click="setTheme(item.value)"
    >
      <component :is="item.icon" :size="16" stroke-width="2.2" />
      <span>{{ item.short }}</span>
    </button>
  </div>
</template>

<script setup>
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { Monitor, Moon, Sun } from '@lucide/vue'
import { applyTheme, getStoredTheme, normalizeTheme, setStoredTheme } from '../../lib/theme'

const options = [
  { value: 'system', label: '跟随系统主题', short: '系统', icon: Monitor },
  { value: 'light', label: '亮色主题', short: '亮', icon: Sun },
  { value: 'dark', label: '暗色主题', short: '暗', icon: Moon }
]

const theme = ref('system')
let mediaQuery

onMounted(() => {
  theme.value = getStoredTheme()
  applyTheme(theme.value)
  mediaQuery = window.matchMedia?.('(prefers-color-scheme: dark)')
  mediaQuery?.addEventListener?.('change', handleSystemChange)
})

onBeforeUnmount(() => {
  mediaQuery?.removeEventListener?.('change', handleSystemChange)
})

function setTheme(value) {
  theme.value = setStoredTheme(normalizeTheme(value))
}

function handleSystemChange() {
  if (theme.value === 'system') applyTheme('system')
}
</script>

<style scoped>
.mm-theme-toggle {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  min-height: 38px;
  padding: 4px;
  border: 1px solid var(--mm-border);
  border-radius: 999px;
  background: var(--mm-control-bg);
  box-shadow: var(--mm-shadow-soft);
}

.mm-theme-toggle button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  min-width: 56px;
  min-height: 30px;
  padding: 0 10px;
  border: 0;
  border-radius: 999px;
  background: transparent;
  color: var(--mm-muted);
  font: inherit;
  font-size: var(--mm-font-size-sm);
  font-weight: var(--mm-font-weight-medium);
  cursor: pointer;
  transition: background .18s ease, color .18s ease, box-shadow .18s ease;
}

.mm-theme-toggle button:hover {
  color: var(--mm-primary);
}

.mm-theme-toggle button.active {
  background: var(--mm-bg);
  color: var(--mm-primary);
  box-shadow: var(--mm-shadow-soft);
}

.mm-theme-toggle svg {
  flex: 0 0 auto;
}
</style>
