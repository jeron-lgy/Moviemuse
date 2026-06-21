<template>
  <section class="mm-page dashboard-page">
    <PageHeader
      kicker="控制台"
      title="首页"
      description="订阅、扫描、后处理、转码和通知链路的实时状态总览。"
    >
      <template #actions>
        <BaseButton type="button" :disabled="isFetching" @click="refreshDashboard">
          <RefreshCw :size="16" />{{ isFetching ? '刷新中' : '刷新' }}
        </BaseButton>
        <BaseButton type="button" :disabled="scanning || !scanDirs.length" @click="runScan">
          <Play :size="16" />{{ scanning ? '启动中' : '手动扫描' }}
        </BaseButton>
        <BaseButton as="RouterLink" variant="primary" to="/subscription-search">
          <Search :size="16" />搜索番号
        </BaseButton>
      </template>
    </PageHeader>

    <NoticeBanner v-if="notice">{{ notice }}</NoticeBanner>
    <NoticeBanner v-if="actionError" tone="error">{{ actionError }}</NoticeBanner>

    <BaseCard v-if="isLoading" class="loading-card">正在读取本地状态...</BaseCard>
    <BaseCard v-else-if="error" class="loading-card">首页数据读取失败：{{ error.message }}</BaseCard>
    <template v-else>
      <section class="status-strip">
        <div class="status-copy">
          <span class="live-dot" :class="healthTone"></span>
          <strong>{{ healthTitle }}</strong>
          <em>{{ healthSubtitle }}</em>
        </div>
        <div class="quick-links">
          <BaseButton as="RouterLink" to="/subtitles" size="sm">
            <ListChecks :size="15" />查看任务
          </BaseButton>
          <BaseButton as="RouterLink" to="/duplicates" size="sm">
            <Copy :size="15" />重复视频
          </BaseButton>
          <BaseButton as="RouterLink" to="/system" size="sm">
            <Settings :size="15" />系统设置
          </BaseButton>
        </div>
      </section>

      <section class="metric-grid">
        <RouterLink
          v-for="card in metricCards"
          :key="card.label"
          class="metric-link"
          :to="card.to"
        >
          <BaseCard as="article" class="metric-card">
            <span>{{ card.label }}</span>
            <strong>{{ card.value }}</strong>
            <p>{{ card.note }}</p>
            <StatusPill :tone="card.pillTone">{{ card.trendText }}</StatusPill>
          </BaseCard>
        </RouterLink>
      </section>

      <section class="dashboard-grid top-grid">
        <BaseCard as="article" class="panel queue-panel">
          <div class="panel-head">
            <div>
              <h2>后处理队列状态</h2>
              <p>转码、字幕和下载接管任务的当前分布。</p>
            </div>
            <StatusPill :tone="queue.failed ? 'danger' : 'success'">{{ queue.failed ? '需要处理' : '运行正常' }}</StatusPill>
          </div>
          <div class="queue-stats">
            <RouterLink v-for="item in queueItems" :key="item.key" :to="item.to" class="queue-stat">
              <span>{{ item.label }}</span>
              <strong>{{ item.value }}</strong>
            </RouterLink>
          </div>
          <div class="completion-row">
            <div>
              <span>队列完成率</span>
              <strong>{{ queueCompletion }}%</strong>
            </div>
            <i><b :style="{ width: `${queueCompletion}%` }"></b></i>
          </div>
          <div class="panel-actions">
            <BaseButton as="RouterLink" to="/subtitles" size="sm">打开任务中心</BaseButton>
            <BaseButton as="RouterLink" to="/automation" size="sm">自动任务策略</BaseButton>
          </div>
        </BaseCard>

        <BaseCard as="article" class="panel health-panel">
          <div class="panel-head">
            <div>
              <h2>链路健康度</h2>
              <p>核心服务、目录和通知通道的可用状态。</p>
            </div>
            <HeartPulse :size="22" />
          </div>
          <div class="health-list">
            <RouterLink v-for="item in integrations" :key="item.name" to="/system" class="health-row">
              <span>{{ item.name }}</span>
              <StatusPill :tone="integrationTone(item)">{{ item.status }}</StatusPill>
            </RouterLink>
          </div>
        </BaseCard>
      </section>

      <section class="dashboard-grid middle-grid">
        <BaseCard as="article" class="panel scan-panel">
          <div class="panel-head">
            <div>
              <h2>媒体扫描</h2>
              <p>重复文件缓存快照和扫描进度。</p>
            </div>
            <StatusPill :tone="scanPillTone">{{ scanStatusLabel }}</StatusPill>
          </div>
          <div class="scan-progress">
            <div>
              <strong>{{ scanProgress }}%</strong>
              <span>{{ scan.current_path || '当前没有正在扫描的路径' }}</span>
            </div>
            <i><b :style="{ width: `${scanProgress}%` }"></b></i>
          </div>
          <div class="scan-meta">
            <span><Clock3 :size="15" />开始 {{ scan.started_at || '未知' }}</span>
            <span><CheckCircle2 :size="15" />完成 {{ scan.finished_at || '未知' }}</span>
            <span><Database :size="15" />目录 {{ scanDirs.length }} 个</span>
          </div>
          <div class="panel-actions">
            <BaseButton as="RouterLink" to="/duplicates" size="sm">查看重复组</BaseButton>
            <BaseButton type="button" size="sm" :disabled="scanning || !scanDirs.length" @click="runScan">
              {{ scanning ? '启动中' : '按当前目录扫描' }}
            </BaseButton>
          </div>
        </BaseCard>

        <BaseCard as="article" class="panel automation-panel">
          <div class="panel-head">
            <div>
              <h2>自动化策略</h2>
              <p>订阅下载完成后的自动推进状态。</p>
            </div>
            <Sparkles :size="22" />
          </div>
          <div class="automation-list">
            <RouterLink v-for="item in automationItems" :key="item.label" to="/automation" class="automation-row">
              <span>{{ item.label }}</span>
              <StatusPill :tone="item.enabled ? 'success' : 'neutral'">{{ item.enabled ? '已开启' : '未开启' }}</StatusPill>
            </RouterLink>
          </div>
          <div class="codec-summary">
            <span>编码目标</span>
            <strong>{{ automation.target_codec || 'av1' }} / CRF {{ automation.crf || 36 }} / {{ automation.preset || 'p1' }}</strong>
          </div>
        </BaseCard>
      </section>

      <section class="dashboard-grid bottom-grid">
        <BaseCard as="article" class="panel subscription-panel">
          <div class="panel-head">
            <div>
              <h2>订阅概况</h2>
              <p>当前番号订阅的流转状态。</p>
            </div>
            <BaseButton as="RouterLink" to="/subscriptions" size="sm">查看订阅</BaseButton>
          </div>
          <div class="bars">
            <RouterLink v-for="item in subscriptionBars" :key="item.label" to="/subscriptions" class="bar-row">
              <div><span>{{ item.label }}</span><strong>{{ item.value }}</strong></div>
              <i><b :style="{ width: `${item.percent}%` }"></b></i>
            </RouterLink>
          </div>
        </BaseCard>

        <BaseCard as="article" class="panel recent-panel">
          <div class="panel-head">
            <div>
              <h2>最近任务流</h2>
              <p>最新的后处理、订阅和自动任务动作。</p>
            </div>
            <BaseButton as="RouterLink" to="/subtitles" size="sm">任务中心</BaseButton>
          </div>
          <div class="recent-list">
            <RouterLink v-for="task in recentTasks" :key="`${task.type}-${task.title}-${task.ts}`" to="/subtitles" class="recent-row">
              <StatusPill :tone="taskTone(task.status)">{{ task.type }}</StatusPill>
              <div>
                <strong>{{ task.title }}</strong>
                <p>{{ task.note || task.status || '暂无详情' }}</p>
              </div>
              <time>{{ task.time || '未知' }}</time>
            </RouterLink>
            <div v-if="!recentTasks.length" class="empty">暂时没有最近任务。</div>
          </div>
        </BaseCard>

        <BaseCard as="article" class="panel alert-panel">
          <div class="panel-head">
            <div>
              <h2>异常与提醒</h2>
              <p>失败任务、接口错误和最近日志。</p>
            </div>
            <BaseButton as="RouterLink" to="/logs" size="sm">日志</BaseButton>
          </div>
          <div class="alert-summary">
            <RouterLink v-for="item in alertItems" :key="item.label" :to="item.to" class="alert-row">
              <span>{{ item.label }}</span>
              <StatusPill :tone="item.tone">{{ item.value }}</StatusPill>
            </RouterLink>
          </div>
          <div class="log-snippets">
            <RouterLink v-for="(item, index) in logs" :key="`${item.ts || item.time || index}-${index}`" to="/logs" class="log-line">
              <span class="level" :class="item.level">{{ item.level || 'info' }}</span>
              <strong>{{ item.message || item.source || '日志事件' }}</strong>
            </RouterLink>
            <div v-if="!logs.length" class="empty success">当前没有需要处理的异常。</div>
          </div>
        </BaseCard>
      </section>
    </template>
  </section>
</template>

<script setup>
import { computed, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { useQuery } from '@tanstack/vue-query'
import {
  CheckCircle2,
  Clock3,
  Copy,
  Database,
  HeartPulse,
  ListChecks,
  Play,
  RefreshCw,
  Search,
  Settings,
  Sparkles
} from '@lucide/vue'
import { api, postFormData } from '../lib/api'
import { BaseButton, BaseCard, NoticeBanner, PageHeader, StatusPill } from '../components/ui'

const scanning = ref(false)
const notice = ref('')
const actionError = ref('')

const { data, isLoading, isFetching, error, refetch } = useQuery({
  queryKey: ['dashboard'],
  queryFn: () => api('/api/dashboard'),
  staleTime: 20_000,
  refetchInterval: 10_000
})

const dashboard = computed(() => data.value?.dashboard || {})
const queue = computed(() => dashboard.value.queue || {})
const scan = computed(() => dashboard.value.scan || {})
const automation = computed(() => dashboard.value.automation || {})
const integrations = computed(() => dashboard.value.integrations || [])
const recentTasks = computed(() => dashboard.value.recent_tasks || [])
const logs = computed(() => (dashboard.value.logs || []).slice(0, 3))
const scanDirs = computed(() => scan.value.scanned_dirs || [])

const metricConfig = {
  订阅番号: { to: '/subscriptions' },
  订阅女优: { to: '/subscriptions' },
  媒体扫描: { to: '/duplicates' },
  异常事件: { to: '/logs' }
}

const metricCards = computed(() => (dashboard.value.cards || []).map((card) => {
  const config = metricConfig[card.label] || { to: '/dashboard' }
  const isErrorCard = card.label.includes('异常')
  const hasValue = Number(card.value || 0) > 0
  return {
    ...card,
    ...config,
    trendText: card.trend?.text || '暂无变化',
    pillTone: isErrorCard && hasValue ? 'danger' : card.trend?.tone === 'flat' ? 'success' : 'primary'
  }
}))

const subscriptionBars = computed(() => {
  const sub = dashboard.value.subscription || {}
  const total = Math.max(Number(sub.total || 0), 1)
  return [
    { label: '订阅中', value: Number(sub.pending || 0) },
    { label: '已完成', value: Number(sub.done || 0) },
    { label: '已入库', value: Number(sub.in_library || 0) },
    { label: 'MTeam 已命中 / 已推送', value: Number(sub.downloaded || 0) }
  ].map((item) => ({ ...item, percent: Math.min(100, Math.round(item.value / total * 100)) }))
})

const queueItems = computed(() => [
  { key: 'running', label: '运行中', value: Number(queue.value.running || 0), to: '/subtitles' },
  { key: 'waiting', label: '等待中', value: Number(queue.value.waiting || 0), to: '/subtitles' },
  { key: 'completed', label: '已完成', value: Number(queue.value.completed || 0), to: '/subtitles' },
  { key: 'failed', label: '失败', value: Number(queue.value.failed || 0), to: '/subtitles' }
])

const queueCompletion = computed(() => {
  const total = Math.max(Number(queue.value.total || 0), 1)
  return Math.min(100, Math.round(Number(queue.value.completed || 0) / total * 100))
})

const automationItems = computed(() => [
  { label: '自动转码', enabled: !!automation.value.auto_transcode_enabled },
  { label: '自动字幕', enabled: !!automation.value.auto_subtitle_enabled },
  { label: '算力端自动执行', enabled: !!automation.value.worker_auto_run }
])

const missingIntegrations = computed(() => integrations.value.filter((item) => item.tone !== 'ok').length)
const errorLogs = computed(() => (dashboard.value.logs || []).filter((item) => item.level === 'error').length)
const healthTone = computed(() => (queue.value.failed || errorLogs.value ? 'danger' : missingIntegrations.value ? 'warning' : 'success'))
const healthTitle = computed(() => {
  if (queue.value.failed || errorLogs.value) return '有异常需要处理'
  if (missingIntegrations.value) return '链路部分待配置'
  return '系统运行正常'
})
const healthSubtitle = computed(() => {
  if (queue.value.failed) return `${queue.value.failed} 个失败任务，建议进入任务中心重试或查看日志。`
  if (errorLogs.value) return `最近日志中有 ${errorLogs.value} 条错误记录。`
  if (missingIntegrations.value) return `${missingIntegrations.value} 个集成还未完整配置。`
  return '最近日志平稳，订阅和后处理链路可以继续自动推进。'
})

const scanProgress = computed(() => Math.max(0, Math.min(100, Number(scan.value.progress || 0))))
const scanStatusLabel = computed(() => ({
  running: '扫描中',
  completed: '已完成',
  failed: '失败',
  idle: '待扫描'
}[scan.value.status] || scan.value.status || '待扫描'))
const scanPillTone = computed(() => scan.value.status === 'failed' ? 'danger' : scan.value.status === 'running' ? 'primary' : 'success')

const alertItems = computed(() => [
  { label: '失败任务', value: Number(queue.value.failed || 0), tone: queue.value.failed ? 'danger' : 'success', to: '/subtitles' },
  { label: '接口错误', value: errorLogs.value, tone: errorLogs.value ? 'danger' : 'success', to: '/logs' },
  { label: '重复文件', value: `${dashboard.value.cards?.find((item) => item.label === '媒体扫描')?.note || '暂无快照'}`, tone: 'primary', to: '/duplicates' }
])

function integrationTone(item) {
  return item.tone === 'ok' ? 'success' : 'neutral'
}

function taskTone(status) {
  const value = String(status || '')
  if (['failed', 'ignored', 'conflict', 'expired', 'error'].includes(value)) return 'danger'
  if (['completed', 'done', 'ok', 'in_library'].includes(value)) return 'success'
  return 'primary'
}

async function refreshDashboard() {
  notice.value = ''
  actionError.value = ''
  await refetch()
}

async function runScan() {
  if (!scanDirs.value.length) {
    actionError.value = '没有可扫描目录，请先到重复视频页面选择扫描范围。'
    return
  }
  scanning.value = true
  notice.value = ''
  actionError.value = ''
  try {
    const form = new FormData()
    for (const path of scanDirs.value) form.append('paths', path)
    const payload = await postFormData('/api/scan/run', form)
    notice.value = `扫描已启动：${payload.scan_dirs?.length || scanDirs.value.length} 个目录。`
    await refetch()
  } catch (scanError) {
    actionError.value = scanError.message || '启动扫描失败'
  } finally {
    scanning.value = false
  }
}
</script>

<style scoped>
.dashboard-page {
  gap: 18px;
}

.status-strip {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  min-height: 64px;
  padding: 14px 16px;
  border: 1px solid var(--mm-border);
  border-radius: var(--mm-radius-md);
  background: var(--mm-surface);
}

.status-copy {
  display: grid;
  grid-template-columns: 12px minmax(0, auto) minmax(0, 1fr);
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.status-copy strong {
  font-size: 15px;
  font-weight: var(--mm-font-weight-semibold);
}

.status-copy em {
  overflow: hidden;
  color: var(--mm-muted);
  font-size: 13px;
  font-style: normal;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.live-dot {
  width: 10px;
  height: 10px;
  border-radius: 999px;
  background: var(--mm-success);
  box-shadow: 0 0 0 6px var(--mm-success-soft);
}

.live-dot.warning {
  background: var(--mm-warning);
  box-shadow: 0 0 0 6px var(--mm-warning-soft);
}

.live-dot.danger {
  background: var(--mm-danger);
  box-shadow: 0 0 0 6px var(--mm-danger-soft);
}

.quick-links,
.panel-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
}

.metric-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 16px;
}

.metric-link {
  color: inherit;
  text-decoration: none;
}

.metric-card {
  min-height: 160px;
  transition: border-color .18s ease, color .18s ease, transform .18s ease;
}

.metric-link:hover .metric-card {
  border-color: rgba(255, 56, 92, .36);
  color: var(--mm-primary);
  transform: translateY(-1px);
}

.metric-card span {
  color: var(--mm-muted);
  font-size: 14px;
  font-weight: 400;
}

.metric-card strong {
  display: block;
  margin-top: 18px;
  color: var(--mm-text);
  font-size: 40px;
  font-weight: 600;
  line-height: 1;
}

.metric-card p {
  margin: 16px 0 0;
  overflow: hidden;
  color: var(--mm-muted);
  font-size: 14px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.metric-card :deep(.status-pill) {
  margin-top: 12px;
}

.dashboard-grid {
  display: grid;
  gap: 12px;
  align-items: stretch;
}

.top-grid,
.middle-grid {
  grid-template-columns: minmax(0, 1.35fr) minmax(320px, .85fr);
}

.bottom-grid {
  grid-template-columns: minmax(280px, .95fr) minmax(360px, 1.15fr) minmax(280px, .85fr);
}

.panel {
  display: grid;
  align-content: start;
  gap: 16px;
  min-width: 0;
}

.panel-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 14px;
}

.panel h2,
.panel p {
  margin: 0;
}

.panel h2 {
  font-size: 20px;
  font-weight: var(--mm-font-weight-semibold);
}

.panel p {
  margin-top: 6px;
  color: var(--mm-muted);
  font-size: 13px;
  line-height: 1.6;
}

.queue-stats {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
}

.queue-stat,
.health-row,
.automation-row,
.alert-row,
.recent-row,
.log-line,
.bar-row {
  color: var(--mm-text);
  text-decoration: none;
}

.queue-stat {
  display: grid;
  gap: 8px;
  min-height: 74px;
  padding: 12px;
  border-radius: var(--mm-radius-sm);
  background: var(--mm-surface);
}

.queue-stat span,
.codec-summary span,
.completion-row span {
  color: var(--mm-muted);
  font-size: 13px;
}

.queue-stat strong {
  font-size: 24px;
  font-weight: var(--mm-font-weight-semibold);
}

.completion-row {
  display: grid;
  gap: 8px;
}

.completion-row div {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.completion-row strong {
  font-size: 14px;
}

.completion-row i,
.bar-row i,
.scan-progress i {
  display: block;
  height: 8px;
  overflow: hidden;
  border-radius: 999px;
  background: var(--mm-surface);
}

.completion-row b,
.bar-row b,
.scan-progress b {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: var(--mm-primary);
  transition: width .2s ease;
}

.health-list,
.automation-list,
.alert-summary,
.log-snippets,
.recent-list,
.bars {
  display: grid;
  gap: 10px;
}

.health-row,
.automation-row,
.alert-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  min-height: 44px;
  padding: 0 12px;
  border-radius: var(--mm-radius-sm);
  background: var(--mm-surface);
}

.scan-progress {
  display: grid;
  gap: 10px;
}

.scan-progress div {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  align-items: end;
  gap: 14px;
}

.scan-progress strong {
  font-size: 34px;
  line-height: 1;
}

.scan-progress span {
  overflow: hidden;
  color: var(--mm-muted);
  font-size: 13px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.scan-meta {
  display: grid;
  gap: 8px;
}

.scan-meta span {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  color: var(--mm-muted);
  font-size: 13px;
}

.codec-summary {
  display: grid;
  gap: 6px;
  padding: 12px;
  border-radius: var(--mm-radius-sm);
  background: var(--mm-primary-soft);
}

.codec-summary strong {
  color: var(--mm-primary);
  font-size: 15px;
}

.bar-row {
  display: grid;
  gap: 8px;
}

.bar-row div {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  font-size: 13px;
}

.bar-row span {
  color: var(--mm-muted);
}

.recent-row {
  display: grid;
  grid-template-columns: 82px minmax(0, 1fr) auto;
  gap: 12px;
  align-items: center;
  min-height: 58px;
  padding: 10px 12px;
  border-radius: var(--mm-radius-sm);
  background: var(--mm-surface);
}

.recent-row strong,
.log-line strong {
  overflow: hidden;
  color: var(--mm-text);
  font-size: 14px;
  font-weight: var(--mm-font-weight-semibold);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.recent-row p {
  margin-top: 4px;
  overflow: hidden;
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.recent-row time {
  color: var(--mm-muted);
  font-size: 12px;
  white-space: nowrap;
}

.log-line {
  display: grid;
  grid-template-columns: 54px minmax(0, 1fr);
  align-items: center;
  gap: 10px;
  min-height: 38px;
}

.level {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 24px;
  border-radius: 999px;
  background: var(--mm-surface);
  color: var(--mm-muted);
  font-size: 11px;
  font-weight: var(--mm-font-weight-semibold);
}

.level.error {
  background: var(--mm-danger-soft);
  color: var(--mm-danger);
}

.empty,
.loading-card {
  padding: 24px;
  color: var(--mm-muted);
  text-align: center;
}

.empty.success {
  border-radius: var(--mm-radius-sm);
  background: var(--mm-success-soft);
  color: var(--mm-success);
  text-align: left;
}

@media (max-width: 1280px) {
  .metric-grid,
  .queue-stats {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .top-grid,
  .middle-grid,
  .bottom-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 760px) {
  .status-strip,
  .panel-head {
    display: grid;
  }

  .status-copy {
    grid-template-columns: 12px minmax(0, 1fr);
  }

  .status-copy em {
    grid-column: 2;
    white-space: normal;
  }

  .quick-links,
  .panel-actions {
    justify-content: flex-start;
  }

  .metric-grid,
  .queue-stats {
    grid-template-columns: 1fr;
  }

  .recent-row {
    grid-template-columns: 82px minmax(0, 1fr);
  }

  .recent-row time {
    grid-column: 2;
  }
}
</style>
