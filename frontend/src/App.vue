<template>
  <div class="mm-shell" :class="{ 'sidebar-collapsed': ui.sidebarCollapsed }">
    <aside v-if="!route.meta.fullBleed" class="mm-sidebar" :class="{ collapsed: ui.sidebarCollapsed }">
      <div class="mm-brand-row">
        <RouterLink class="mm-brand" to="/dashboard" title="MovieMuse">
          <span class="mm-brand-icon"><img :src="'/static/icons/android-chrome-192x192.png'" alt=""></span>
          <span class="mm-brand-copy">
            <strong>MovieMuse</strong>
            <em>AI-Powered Media Library</em>
          </span>
        </RouterLink>
        <button
          class="mm-sidebar-toggle"
          type="button"
          :aria-label="ui.sidebarCollapsed ? '展开侧边栏' : '折叠侧边栏'"
          @click="ui.toggleSidebar"
        >
          <ChevronRight v-if="ui.sidebarCollapsed" :size="18" />
          <ChevronLeft v-else :size="18" />
        </button>
      </div>

      <nav class="mm-nav" aria-label="MovieMuse navigation">
        <RouterLink class="mm-nav-item" to="/dashboard" title="首页">
          <LayoutDashboard :size="20" />
          <span>首页</span>
        </RouterLink>

        <div class="mm-nav-group" :class="{ open: subscriptionsOpen }">
          <button class="mm-nav-item" type="button" title="订阅管理" @click="ui.toggleGroup('subscriptions')">
            <PanelTop :size="20" />
            <span>订阅管理</span>
            <ChevronDown class="mm-chevron" :size="18" />
          </button>
          <div class="mm-subnav">
            <RouterLink to="/subscription-search">搜索</RouterLink>
            <RouterLink to="/subscriptions">订阅</RouterLink>
            <RouterLink to="/makers">厂牌发售</RouterLink>
            <RouterLink to="/rankings">榜单</RouterLink>
          </div>
        </div>

        <RouterLink class="mm-nav-item" to="/duplicates" title="重复视频">
          <Copy :size="20" />
          <span>重复视频</span>
        </RouterLink>

        <RouterLink class="mm-nav-item" to="/subtitles" title="任务中心">
          <ListChecks :size="20" />
          <span>任务中心</span>
        </RouterLink>

        <RouterLink class="mm-nav-item" to="/automation" title="自动任务">
          <Sparkles :size="20" />
          <span>自动任务</span>
        </RouterLink>

        <div class="mm-nav-divider"></div>

        <RouterLink class="mm-nav-item" to="/system" title="系统">
          <Settings :size="20" />
          <span>系统</span>
        </RouterLink>

        <RouterLink class="mm-nav-item" to="/logs" title="日志系统">
          <FileText :size="20" />
          <span>日志系统</span>
        </RouterLink>

        <RouterLink class="mm-nav-item" to="/scan-api" title="扫描 API">
          <ScanLine :size="20" />
          <span>扫描 API</span>
        </RouterLink>
      </nav>

      <button class="mm-nav-item mm-logout" type="button" title="注销" @click="logout">
        <LogOut :size="20" />
        <span>注销</span>
      </button>
    </aside>

    <main class="mm-main" :class="{ full: route.meta.fullBleed }">
      <div v-if="demo.enabled && !route.meta.fullBleed" class="mm-demo-banner">
        <span>演示模式已开启，封面和系统设置正在以非破坏方式隐藏。</span>
        <RouterLink to="/system?tab=demo">管理</RouterLink>
      </div>
      <div v-if="showThemeToggle" class="mm-app-toolbar">
        <BaseThemeToggle />
      </div>
      <RouterView />
    </main>
  </div>
</template>

<script setup>
import { computed, onMounted, watch } from 'vue'
import { RouterLink, RouterView, useRoute } from 'vue-router'
import {
  ChevronLeft,
  ChevronDown,
  ChevronRight,
  Copy,
  FileText,
  LayoutDashboard,
  ListChecks,
  LogOut,
  PanelTop,
  ScanLine,
  Settings,
  Sparkles
} from '@lucide/vue'
import { useUiStore } from './stores/ui'
import { useDemoStore } from './stores/demo'
import { postJson } from './lib/api'
import { BaseThemeToggle } from './components/ui'

const ui = useUiStore()
const demo = useDemoStore()
const route = useRoute()
const subscriptionsOpen = computed(() => ui.openGroups.subscriptions)
const showThemeToggle = computed(() => !route.meta.fullBleed && route.name !== 'ui-preview')

watch(
  () => route.meta.module,
  (moduleName) => {
    if (moduleName !== '订阅管理') ui.openGroups.subscriptions = false
  },
  { immediate: true }
)

onMounted(() => {
  demo.load().catch(() => {
    demo.loaded = true
  })
})

async function logout() {
  try {
    await postJson('/api/auth/logout', {}, { skipAuthRedirect: true })
  } finally {
    window.location.href = '/login'
  }
}
</script>

<style scoped>
.mm-shell {
  display: grid;
  grid-template-columns: 284px minmax(0, 1fr);
  min-height: 100vh;
  gap: 24px;
  padding: 24px;
  background: var(--mm-bg);
}

.mm-shell.sidebar-collapsed {
  grid-template-columns: 92px minmax(0, 1fr);
}

.mm-sidebar,
.mm-main {
  border: 1px solid var(--mm-border);
  border-radius: 20px;
  background: var(--mm-card-bg);
}

.mm-sidebar {
  position: sticky;
  top: 24px;
  display: flex;
  flex-direction: column;
  align-self: start;
  min-height: calc(100vh - 48px);
  padding: 24px;
  box-shadow: var(--mm-shadow);
}

.mm-brand-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 36px;
  gap: 10px;
  align-items: center;
}

.mm-brand {
  display: grid;
  grid-template-columns: 44px minmax(0, 1fr);
  gap: 14px;
  align-items: center;
  color: var(--mm-text);
  text-decoration: none;
}

.mm-sidebar-toggle {
  display: grid;
  place-items: center;
  width: 36px;
  height: 36px;
  border: 1px solid var(--mm-border);
  border-radius: var(--mm-radius-sm);
  background: var(--mm-control-bg);
  color: var(--mm-muted);
  cursor: pointer;
  transition: background .18s ease, border-color .18s ease, color .18s ease;
}

.mm-sidebar-toggle:hover {
  border-color: rgba(255, 56, 92, .32);
  background: var(--mm-primary-soft);
  color: var(--mm-primary);
}

.mm-brand-icon {
  display: grid;
  place-items: center;
  width: 44px;
  height: 44px;
  overflow: hidden;
  border-radius: 14px;
  background: var(--mm-primary-soft);
}

.mm-brand-icon img {
  width: 34px;
  height: 34px;
}

.mm-brand-copy strong {
  display: block;
  font-size: 20px;
  font-weight: 600;
  letter-spacing: -0.2px;
}

.mm-brand-copy em {
  display: block;
  margin-top: 2px;
  color: var(--mm-muted);
  font-size: 13px;
  font-style: normal;
  font-weight: 400;
}

.mm-nav {
  display: grid;
  gap: 10px;
  margin-top: 28px;
}

.mm-logout {
  margin-top: auto;
  color: var(--mm-muted);
}

.mm-nav-item {
  display: grid;
  grid-template-columns: 24px minmax(0, 1fr) auto;
  align-items: center;
  gap: 12px;
  width: 100%;
  min-height: 48px;
  padding: 0 14px;
  border: 1px solid var(--mm-border);
  border-radius: 14px;
  background: var(--mm-control-bg);
  color: var(--mm-text);
  font-size: 15px;
  font-weight: 500;
  text-align: left;
  text-decoration: none;
  cursor: pointer;
  transition: background .18s ease, border-color .18s ease, color .18s ease, transform .18s ease;
}

.mm-nav-item:hover,
.mm-nav-item.router-link-active,
.mm-logout:hover {
  border-color: rgba(255, 56, 92, .32);
  background: var(--mm-primary-soft);
  color: var(--mm-primary);
}

.mm-nav-item:active {
  transform: translateY(1px);
}

.mm-chevron {
  transition: transform .18s ease;
}

.mm-nav-group.open .mm-chevron {
  transform: rotate(180deg);
}

.mm-subnav {
  display: grid;
  grid-template-rows: 0fr;
  max-height: 0;
  overflow: hidden;
  transition: grid-template-rows .2s ease, max-height .2s ease, margin .2s ease;
}

.mm-nav-group.open .mm-subnav {
  grid-template-rows: 1fr;
  max-height: 180px;
  margin-top: 8px;
}

.mm-subnav::before {
  content: "";
  min-height: 0;
}

.mm-subnav a {
  display: flex;
  align-items: center;
  min-height: 38px;
  padding: 0 14px 0 52px;
  border-radius: 14px;
  color: var(--mm-muted);
  font-size: 14px;
  font-weight: 400;
  text-decoration: none;
}

.mm-subnav a:hover,
.mm-subnav a.router-link-active {
  background: var(--mm-surface);
  color: var(--mm-primary);
}

.mm-nav-divider {
  height: 1px;
  margin: 6px 0;
  background: var(--mm-border);
}

.mm-sidebar.collapsed {
  padding: 20px 14px;
}

.mm-sidebar.collapsed .mm-brand-row {
  grid-template-columns: 1fr;
  justify-items: center;
}

.mm-sidebar.collapsed .mm-brand {
  grid-template-columns: 44px;
}

.mm-sidebar.collapsed .mm-brand-copy,
.mm-sidebar.collapsed .mm-nav-item span,
.mm-sidebar.collapsed .mm-logout span,
.mm-sidebar.collapsed .mm-chevron,
.mm-sidebar.collapsed .mm-subnav {
  display: none;
}

.mm-sidebar.collapsed .mm-nav-item,
.mm-sidebar.collapsed .mm-logout {
  grid-template-columns: 1fr;
  justify-items: center;
  padding: 0;
}

.mm-main {
  min-width: 0;
  min-height: calc(100vh - 48px);
  padding: 32px;
}

.mm-app-toolbar {
  display: flex;
  justify-content: flex-end;
  margin: -8px 0 18px;
}

.mm-demo-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  min-height: 44px;
  margin: -8px 0 18px;
  padding: 0 14px;
  border: 1px solid color-mix(in srgb, var(--mm-warning) 34%, var(--mm-border));
  border-radius: var(--mm-radius-md);
  background: var(--mm-warning-soft);
  color: var(--mm-text);
  font-size: 13px;
}

.mm-demo-banner a {
  flex: none;
  color: var(--mm-primary);
  font-weight: var(--mm-font-weight-semibold);
  text-decoration: none;
}

.mm-main.full {
  grid-column: 1 / -1;
  padding: 0;
  border: 0;
}

@media (max-width: 980px) {
  .mm-shell {
    grid-template-columns: 1fr;
    gap: 16px;
    padding: 16px;
  }

  .mm-shell.sidebar-collapsed {
    grid-template-columns: 1fr;
  }

  .mm-sidebar {
    position: relative;
    top: 0;
    min-width: 0;
    min-height: auto;
    overflow: hidden;
    padding: 16px;
  }

  .mm-brand-row {
    grid-template-columns: minmax(0, 1fr);
  }

  .mm-sidebar-toggle {
    display: none;
  }

  .mm-nav {
    grid-auto-flow: column;
    grid-auto-columns: max-content;
    width: 100%;
    min-width: 0;
    overflow-x: auto;
    padding-bottom: 2px;
  }

  .mm-logout {
    margin-top: 10px;
  }

  .mm-nav-item {
    min-width: 132px;
  }

  .mm-nav-group.open .mm-subnav,
  .mm-nav-divider {
    display: none;
  }

  .mm-main {
    padding: 20px;
  }
}
</style>
