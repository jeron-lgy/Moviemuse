<template>
  <section class="mm-page">
    <PageHeader title="Dashboard" description="把订阅、扫描、任务和通知链路的关键状态收在一屏。">
      <template #actions>
        <BaseButton as="RouterLink" to="/subtitles">查看任务</BaseButton>
        <BaseButton as="RouterLink" variant="primary" to="/subscription-search">搜索番号</BaseButton>
      </template>
    </PageHeader>

    <BaseCard v-if="isLoading" class="loading-card">正在读取本地状态...</BaseCard>
    <BaseCard v-else-if="error" class="loading-card">Dashboard 数据读取失败：{{ error.message }}</BaseCard>
    <template v-else>
      <section class="metric-grid">
        <BaseCard v-for="card in dashboard.cards || []" :key="card.label" as="article" class="metric-card">
          <span>{{ card.label }}</span>
          <strong>{{ card.value }}</strong>
          <p>{{ card.note }}</p>
          <StatusPill tone="primary">{{ card.trend?.text || '暂无变化' }}</StatusPill>
        </BaseCard>
      </section>

      <section class="dashboard-grid">
        <BaseCard as="article" class="panel wide">
          <h2>订阅概况</h2>
          <p>当前番号订阅的分布，方便判断链路卡在哪一步。</p>
          <div class="bars">
            <div v-for="item in subscriptionBars" :key="item.label" class="bar-row">
              <div><span>{{ item.label }}</span><strong>{{ item.value }}</strong></div>
              <i><b :style="{ width: `${item.percent}%` }"></b></i>
            </div>
          </div>
        </BaseCard>

        <BaseCard as="article" class="panel">
          <h2>最近任务</h2>
          <p>最近订阅、转码、字幕和后处理动作。</p>
          <div class="recent-list">
            <div v-for="task in dashboard.recent_tasks || []" :key="`${task.type}-${task.id}-${task.ts}`" class="recent-row">
              <StatusPill>{{ task.type }}</StatusPill>
              <div>
                <strong>{{ task.title }}</strong>
                <p>{{ task.note || task.detail || '暂无详情' }}</p>
              </div>
            </div>
            <div v-if="!(dashboard.recent_tasks || []).length" class="empty">暂时没有最近任务。</div>
          </div>
        </BaseCard>
      </section>
    </template>
  </section>
</template>

<script setup>
import { computed } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import { api } from '../lib/api'
import { BaseButton, BaseCard, PageHeader, StatusPill } from '../components/ui'

const { data, isLoading, error } = useQuery({
  queryKey: ['dashboard'],
  queryFn: () => api('/api/dashboard'),
  staleTime: 20_000,
  refetchInterval: 10_000
})

const dashboard = computed(() => data.value?.dashboard || {})
const subscriptionBars = computed(() => {
  const sub = dashboard.value.subscription || {}
  const total = Math.max(Number(sub.total || 0), 1)
  return [
    { label: '订阅中', value: Number(sub.pending || 0) },
    { label: '已完成', value: Number(sub.done || 0) },
    { label: '已入库', value: Number(sub.in_library || 0) },
    { label: 'MTeam 已命中/已推送', value: Number(sub.downloaded || 0) }
  ].map((item) => ({ ...item, percent: Math.min(100, Math.round(item.value / total * 100)) }))
})
</script>

<style scoped>
.panel > p {
  margin: 8px 0 0;
  color: var(--mm-muted);
  font-size: 15px;
  line-height: 1.7;
}

.metric-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 16px;
}

.metric-card {
  min-height: 160px;
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
  letter-spacing: -1px;
  line-height: 1;
}

.metric-card p {
  margin: 16px 0 0;
  color: var(--mm-muted);
  font-size: 14px;
}

.dashboard-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.2fr) minmax(360px, .8fr);
  gap: 16px;
  align-items: start;
}

.panel h2 {
  margin: 0;
  font-size: 22px;
  font-weight: 500;
}

.bars,
.recent-list {
  display: grid;
  gap: 14px;
  margin-top: 20px;
}

.bar-row {
  display: grid;
  gap: 8px;
}

.bar-row div {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  color: var(--mm-text);
  font-size: 14px;
}

.bar-row i {
  display: block;
  height: 10px;
  overflow: hidden;
  border-radius: 9999px;
  background: var(--mm-surface);
}

.bar-row b {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: var(--mm-primary);
}

.recent-row {
  display: grid;
  grid-template-columns: 72px minmax(0, 1fr);
  gap: 12px;
  align-items: center;
  padding: 12px;
  border-radius: 14px;
  background: var(--mm-surface);
}

.recent-row strong {
  color: var(--mm-text);
  font-size: 14px;
  font-weight: 500;
}

.recent-row p {
  margin: 4px 0 0;
  overflow: hidden;
  color: var(--mm-muted);
  font-size: 13px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.empty,
.loading-card {
  padding: 24px;
  color: var(--mm-muted);
}

@media (max-width: 1280px) {
  .metric-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .dashboard-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 720px) {
  .metric-grid {
    grid-template-columns: 1fr;
  }
}
</style>
