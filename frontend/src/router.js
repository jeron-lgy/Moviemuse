import { createRouter, createWebHistory } from 'vue-router'

const DashboardView = () => import('./views/DashboardView.vue')
const LoginView = () => import('./views/LoginView.vue')
const TaskCenterView = () => import('./views/TaskCenterView.vue')
const CompareView = () => import('./components/CompareView.vue')
const SubscriptionSearchView = () => import('./views/SubscriptionSearchView.vue')
const SubscriptionListView = () => import('./views/SubscriptionListView.vue')
const MakersView = () => import('./views/MakersView.vue')
const DmmRankingsView = () => import('./views/DmmRankingsView.vue')
const SubscriptionTasksView = () => import('./views/SubscriptionTasksView.vue')
const SubscriptionSettingsView = () => import('./views/SubscriptionSettingsView.vue')
const AutomationView = () => import('./views/AutomationView.vue')
const DuplicatesView = () => import('./views/DuplicatesView.vue')
const LogsView = () => import('./views/LogsView.vue')
const ScanApiView = () => import('./views/ScanApiView.vue')
const UiPreviewView = () => import('./views/UiPreviewView.vue')

export const routes = [
  { path: '/', redirect: '/dashboard' },
  { path: '/login', name: 'login', component: LoginView, meta: { title: '登录', public: true, fullBleed: true } },
  { path: '/dashboard', name: 'dashboard', component: DashboardView, meta: { title: '首页' } },
  { path: '/subtitles', name: 'task-center', component: TaskCenterView, meta: { title: '任务中心' } },
  { path: '/subtitles/compare', name: 'subtitle-compare', component: CompareView, meta: { title: '字幕对比', fullBleed: true } },
  { path: '/subscription-search', name: 'subscription-search', component: SubscriptionSearchView, meta: { title: '搜索', module: '订阅管理' } },
  { path: '/subscriptions', name: 'subscriptions', component: SubscriptionListView, meta: { title: '订阅', module: '订阅管理' } },
  { path: '/makers', name: 'makers', component: MakersView, meta: { title: '厂牌发售', module: '订阅管理' } },
  { path: '/rankings', name: 'rankings', component: DmmRankingsView, meta: { title: 'DMM/FANZA 榜单', module: '订阅管理' } },
  { path: '/subscription-tasks', redirect: '/automation' },
  { path: '/subscription-settings', redirect: '/system' },
  { path: '/subscription-wash', redirect: '/system' },
  { path: '/notifications', redirect: '/system' },
  { path: '/system', name: 'system', component: SubscriptionSettingsView, meta: { title: '系统' } },
  { path: '/duplicates', name: 'duplicates', component: DuplicatesView, meta: { title: '重复视频' } },
  { path: '/automation', name: 'automation', component: AutomationView, meta: { title: '自动任务' } },
  { path: '/logs', name: 'logs', component: LogsView, meta: { title: '日志系统' } },
  { path: '/scan-api', name: 'scan-api', component: ScanApiView, meta: { title: '扫描 API' } },
  { path: '/ui-preview', name: 'ui-preview', component: UiPreviewView, meta: { title: 'UI Preview', public: true } }
]

export const router = createRouter({
  history: createWebHistory(),
  routes
})

const STALE_BUILD_RELOAD_KEY = 'moviemuse:stale-build-reload'
const STALE_BUILD_ERROR_RE = /Failed to fetch dynamically imported module|Importing a module script failed|Unable to preload CSS|dynamically imported module/i

function isStaleBuildError(error) {
  return STALE_BUILD_ERROR_RE.test(String(error?.message || error || ''))
}

router.onError((error) => {
  if (!isStaleBuildError(error)) return
  if (sessionStorage.getItem(STALE_BUILD_RELOAD_KEY) === '1') return
  sessionStorage.setItem(STALE_BUILD_RELOAD_KEY, '1')
  window.location.reload()
})

router.beforeEach(async (to) => {
  if (to.meta.public) return true
  try {
    const response = await fetch('/api/auth/me', { headers: { Accept: 'application/json' } })
    if (response.ok) {
      const payload = await response.json()
      if (payload.authenticated) return true
    }
  } catch {
    // Fall through to login.
  }
  return { path: '/login', query: { redirect: to.fullPath } }
})

router.afterEach((to) => {
  sessionStorage.removeItem(STALE_BUILD_RELOAD_KEY)
  document.title = `${to.meta.title || 'MovieMuse'} - MovieMuse`
})
