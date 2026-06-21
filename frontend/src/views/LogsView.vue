<template>
  <section class="logs-view">
    <PageHeader kicker="系统" title="日志系统" description="查看订阅轮询、Jellyfin 查重、MTeam、qBittorrent、洗版和任务链路日志。">
      <template #actions>
        <select v-model.number="limit" @change="loadLogs">
          <option :value="120">最近 120 条</option>
          <option :value="300">最近 300 条</option>
          <option :value="600">最近 600 条</option>
        </select>
        <BaseButton type="button" :disabled="loading" @click="loadLogs">刷新</BaseButton>
      </template>
    </PageHeader>

    <NoticeBanner v-if="errorMessage" tone="error">{{ errorMessage }}</NoticeBanner>

    <BaseCard class="log-panel" >
      <div class="panel-head">
        <h2>最近 {{ logs.length }} 条</h2>
        <span class="mm-pill">{{ loading ? '读取中' : '已同步' }}</span>
      </div>

      <div v-if="logs.length" class="log-list">
        <article v-for="(item, index) in logs" :key="`${item.time}-${index}`" class="log-row">
          <time>{{ item.time || '-' }}</time>
          <span class="level" :class="item.level">{{ item.level || 'info' }}</span>
          <span class="source">{{ item.source || '-' }}</span>
          <div class="log-main">
            <strong>{{ item.message || '-' }}</strong>
            <span v-if="item.data?.stage" class="stage">{{ item.data.stage }}</span>
            <div v-if="item.data" class="kv">
              <span v-if="item.data.av_id">番号 {{ item.data.av_id }}</span>
              <span v-if="item.data.task_id">任务 {{ item.data.task_id }}</span>
              <span v-if="item.data.torrent_id">种子 {{ item.data.torrent_id }}</span>
              <span v-if="item.data.status">状态 {{ item.data.status }}</span>
              <span v-if="item.data.error">{{ item.data.error }}</span>
            </div>
            <details v-if="item.data">
              <summary>详情</summary>
              <pre>{{ JSON.stringify(item.data, null, 2) }}</pre>
            </details>
          </div>
        </article>
      </div>
      <div v-else class="empty-state">暂无日志。</div>
    </BaseCard>
  </section>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../lib/api'

const logs = ref([])
const limit = ref(300)
const loading = ref(false)
const errorMessage = ref('')

onMounted(loadLogs)

async function loadLogs() {
  loading.value = true
  errorMessage.value = ''
  try {
    const payload = await api(`/api/logs?limit=${limit.value}`)
    logs.value = payload.logs || []
  } catch (error) {
    errorMessage.value = error.message || '读取日志失败'
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.logs-view {
  display: grid;
  gap: 24px;
}

.panel-head,
.page-actions {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.page-actions {
  align-items: center;
}

.eyebrow,
h1,
h2,
p {
  margin: 0;
}

.eyebrow {
  color: var(--mm-primary);
  font-size: 13px;
  font-weight: 600;
}

h1 {
  font-size: 30px;
  font-weight: 650;
}

select {
  min-height: 42px;
  padding: 0 14px;
  border: 1px solid var(--mm-border);
  border-radius: 8px;
  background: var(--mm-control-bg);
  color: var(--mm-text);
}

.log-panel {
  padding: 24px;
}

.log-list {
  display: grid;
  gap: 10px;
  margin-top: 16px;
}

.log-row {
  display: grid;
  grid-template-columns: 160px 80px 130px minmax(0, 1fr);
  gap: 12px;
  padding: 14px;
  border: 1px solid var(--mm-border);
  border-radius: 14px;
  background: var(--mm-card-bg);
}

.level {
  color: var(--mm-primary);
  font-weight: 700;
}

.level.error {
  color: var(--mm-danger);
}

.source,
time {
  color: var(--mm-muted);
  font-weight: 500;
}

.log-main {
  display: grid;
  gap: 8px;
}

.stage,
.kv span {
  display: inline-flex;
  width: fit-content;
  min-height: 26px;
  align-items: center;
  padding: 0 10px;
  border-radius: 999px;
  background: var(--mm-primary-soft);
  color: var(--mm-primary);
  font-size: 12px;
  font-weight: 600;
}

.kv {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

summary {
  color: var(--mm-muted);
  cursor: pointer;
}

pre {
  max-height: 220px;
  overflow: auto;
  margin: 8px 0 0;
  padding: 12px;
  border-radius: 8px;
  background: var(--mm-surface);
  color: var(--mm-muted);
  white-space: pre-wrap;
}

.empty-state {
  margin-top: 16px;
  padding: 32px;
  border: 1px dashed var(--mm-border);
  border-radius: 14px;
  color: var(--mm-muted);
  text-align: center;
}

@media (max-width: 980px) {
    .log-row {
    grid-template-columns: 1fr;
    display: grid;
  }
}
</style>
