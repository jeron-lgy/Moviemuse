<template>
  <section class="tasks-view">
    <PageHeader v-if="!embedded" kicker="系统" title="自动任务" description="集中维护订阅链路的定时任务，cron 修改在这里保存，单个任务可立即执行。">
      <template #actions>
        <BaseButton type="button" :disabled="isLoading" @click="refetch">刷新</BaseButton>
        <BaseButton variant="primary" type="button" :disabled="saving" @click="saveCrons">
          {{ saving ? '保存中' : '保存' }}
        </BaseButton>
      </template>
    </PageHeader>

    <NoticeBanner v-if="message" >{{ message }}</NoticeBanner>
    <NoticeBanner v-if="errorMessage" tone="error">{{ errorMessage }}</NoticeBanner>

    <BaseCard class="task-panel" >
      <div class="panel-head">
        <div>
          <h2>定时任务</h2>
          <p>保存后由后台轮询线程按 cron 执行。立即执行会直接跑当前任务链路。</p>
        </div>
        <span class="mm-pill">{{ tasks.length }} 个任务</span>
      </div>

      <div v-if="isLoading" class="empty">正在读取任务配置...</div>
      <div v-else-if="error" class="empty">任务配置读取失败：{{ error.message }}</div>
      <div v-else class="task-table">
        <div class="task-row head">
          <span>任务名称</span>
          <span>Cron 表达式</span>
          <span>最近执行</span>
          <span>说明</span>
          <span>操作</span>
        </div>
        <div v-for="task in tasks" :key="task.id" class="task-row">
          <div>
            <strong>{{ task.name }}</strong>
            <em>{{ task.id }}</em>
          </div>
          <input v-model.trim="cronEdits[task.id]" aria-label="cron">
          <span>{{ formatTime(task.last_run_at) }}</span>
          <p>{{ task.description }}</p>
          <BaseButton  type="button" :disabled="runningId === task.id" @click="runTask(task)">
            {{ runningId === task.id ? '执行中' : '立即执行' }}
          </BaseButton>
        </div>
      </div>
      <div v-if="embedded" class="task-actions">
        <BaseButton type="button" :disabled="isLoading" @click="refetch">刷新</BaseButton>
        <BaseButton variant="primary" type="button" :disabled="saving" @click="saveCrons">
          {{ saving ? '保存中' : '保存' }}
        </BaseButton>
      </div>
    </BaseCard>

    <BaseCard class="result-panel" >
      <h2>最近执行结果</h2>
      <div v-if="recentResult" class="result-summary" :class="{ failed: recentResult.status === 'failed' }">
        <div class="summary-line">
          <strong>{{ taskName(recentResult.task_id) }}</strong>
          <span>{{ recentResult.status === 'failed' ? '失败' : '成功' }}</span>
          <em>{{ formatTime(recentResult.ran_at) }}</em>
        </div>
        <p v-for="line in recentResultLines" :key="line">{{ line }}</p>
      </div>
      <p v-else>还没有手动执行结果。</p>
    </BaseCard>
  </section>
</template>

<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import { api, postJson } from '../lib/api'

defineProps({
  embedded: {
    type: Boolean,
    default: false
  }
})

const cronKeys = {
  actress_poll: 'actress_cron',
  av_download: 'av_cron',
  wash_download: 'wash_cron',
  postprocess_qb: 'postprocess_cron',
  maker_refresh: 'maker_cron',
  asset_maintenance: 'asset_cron'
}

const cronEdits = reactive({})
const saving = ref(false)
const runningId = ref('')
const message = ref('')
const errorMessage = ref('')
const lastResult = ref('')

const { data, isLoading, error, refetch } = useQuery({
  queryKey: ['subscription-tasks'],
  queryFn: () => api('/api/subscriptions/tasks'),
  staleTime: 10_000,
  refetchInterval: 10_000
})

const tasks = computed(() => Array.isArray(data.value?.tasks) ? data.value.tasks : [])
const recentTaskResult = computed(() => {
  return tasks.value
    .map((task) => task.last_result || {})
    .filter((item) => item && item.ran_at)
    .sort((a, b) => Number(b.ran_at || 0) - Number(a.ran_at || 0))[0] || null
})
const recentResult = computed(() => lastResult.value || recentTaskResult.value)
const recentResultLines = computed(() => formatResultRecord(recentResult.value))

watch(tasks, (rows) => {
  for (const task of rows) {
    if (!(task.id in cronEdits)) cronEdits[task.id] = task.cron || ''
  }
}, { immediate: true })

async function saveCrons() {
  saving.value = true
  message.value = ''
  errorMessage.value = ''
  try {
    const payload = {}
    for (const [taskId, key] of Object.entries(cronKeys)) {
      payload[key] = cronEdits[taskId] || ''
    }
    await postJson('/api/subscriptions/settings', payload)
    await refetch()
    message.value = '定时任务已保存'
  } catch (err) {
    errorMessage.value = err.message || '保存任务失败'
  } finally {
    saving.value = false
  }
}

async function runTask(task) {
  runningId.value = task.id
  message.value = ''
  errorMessage.value = ''
  lastResult.value = ''
  try {
    const payload = await postJson(`/api/subscriptions/tasks/${encodeURIComponent(task.id)}/run`, {})
    lastResult.value = {
      task_id: task.id,
      ran_at: Date.now() / 1000,
      status: payload.status === 'ok' ? 'ok' : (payload.status || 'ok'),
      result: payload.result || payload
    }
    message.value = `${task.name} 执行完成`
    await refetch()
  } catch (err) {
    errorMessage.value = err.message || `${task.name} 执行失败`
    await refetch()
  } finally {
    runningId.value = ''
  }
}

function formatTime(value) {
  const seconds = Number(value || 0)
  if (!seconds) return '暂无'
  return new Date(seconds * 1000).toLocaleString()
}

function formatResultRecord(record) {
  if (!record?.result) return []
  const result = record.result || {}
  if (record.status === 'failed') {
    return [
      `失败位置：${result.failed_at || result.task_id || record.task_id || '任务执行'}`,
      `失败原因：${result.error || result.message || '未知错误'}`
    ]
  }
  const taskId = record.task_id || result.task_id
  if (taskId === 'maker_refresh') return makerRefreshLines(result)
  const lines = []
  const keys = [
    ['checked', '检查'],
    ['sent', '已推送'],
    ['not_found', '未找到'],
    ['errors', '错误'],
    ['expired', '已过期'],
    ['changed', '已更新'],
    ['refreshed_count', '已刷新'],
    ['error_count', '错误']
  ]
  for (const [key, label] of keys) {
    if (result[key] !== undefined) lines.push(`${label}：${result[key]}`)
  }
  if (Array.isArray(result.errors) && result.errors.length) {
    lines.push(...result.errors.slice(0, 5).map((item) => `失败：${item.name || item.id || item.task_id || '未知'} - ${item.error || item.reason || item.message || '未知原因'}`))
  }
  return lines.length ? lines : ['执行完成。']
}

function makerRefreshLines(result) {
  const refreshed = Array.isArray(result.refreshed) ? result.refreshed : []
  const errors = Array.isArray(result.errors) ? result.errors : []
  const lines = [`厂牌：成功 ${refreshed.length} 个，失败 ${errors.length} 个。`]
  for (const item of refreshed) {
    const ok = item.first_screen_cache_ok ? '可秒开' : '首屏缓存不足'
    lines.push(`${item.name || '未知厂牌'}：${ok}，番号 ${item.cached_listing ?? item.count ?? 0} 条，封面 ${item.cover_cached ?? 0}/${item.cover_checked ?? 0}，耗时 ${item.elapsed ?? '-'}s`)
    if (Number(item.cover_errors || 0) > 0) {
      lines.push(`${item.name || '未知厂牌'}：封面失败 ${item.cover_errors} 张`)
    }
  }
  for (const item of errors) {
    lines.push(`失败：${item.name || '未知厂牌'} - ${item.error || '未知原因'}`)
  }
  return lines
}

function taskName(taskId) {
  return tasks.value.find((task) => task.id === taskId)?.name || taskId || '定时任务'
}
</script>

<style scoped>
.tasks-view {
  display: grid;
  gap: 24px;
}

.eyebrow {
  margin: 0 0 6px;
  color: var(--mm-primary);
  font-size: 13px;
  font-weight: 600;
}

h1,
h2,
p {
  margin: 0;
}

h1 {
  font-size: 30px;
  font-weight: 650;
  letter-spacing: -0.3px;
}
.panel-head p,
.result-panel p {
  margin-top: 8px;
  color: var(--mm-muted);
  line-height: 1.7;
}

.page-actions {
  display: flex;
  gap: 10px;
}

.task-panel,
.result-panel {
  padding: 24px;
}

.panel-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 18px;
}

.panel-head h2,
.result-panel h2 {
  font-size: 22px;
  font-weight: 600;
}

.task-table {
  overflow: auto;
  border: 1px solid var(--mm-border);
  border-radius: 14px;
}

.task-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  justify-content: flex-start;
  margin-top: 16px;
}

.task-row {
  display: grid;
  grid-template-columns: minmax(180px, .9fr) minmax(180px, .8fr) minmax(170px, .7fr) minmax(280px, 1.4fr) 120px;
  gap: 16px;
  align-items: center;
  min-width: 980px;
  padding: 16px;
  border-bottom: 1px solid var(--mm-border);
}

.task-row:last-child {
  border-bottom: 0;
}

.task-row.head {
  background: var(--mm-surface);
  color: var(--mm-muted);
  font-size: 13px;
  font-weight: 600;
}

.task-row strong {
  display: block;
  font-weight: 650;
}

.task-row em {
  display: block;
  margin-top: 4px;
  color: var(--mm-muted);
  font-size: 13px;
  font-style: normal;
}

.task-row input {
  min-height: 42px;
  width: 100%;
  padding: 0 12px;
  border: 1px solid var(--mm-border);
  border-radius: 8px;
  background: #fff;
}

.task-row p {
  color: var(--mm-muted);
  line-height: 1.6;
}

.empty {
  padding: 40px;
  color: var(--mm-muted);
  text-align: center;
}

.result-summary {
  display: grid;
  gap: 8px;
  margin-top: 14px;
  padding: 14px;
  border: 1px solid rgba(18, 184, 134, .22);
  border-radius: 8px;
  background: #f3fff9;
}

.result-summary.failed {
  border-color: rgba(255, 56, 92, .28);
  background: #fff5f7;
}

.summary-line {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: center;
}

.summary-line strong {
  font-weight: 650;
}

.summary-line span {
  padding: 3px 9px;
  border-radius: 999px;
  background: rgba(18, 184, 134, .12);
  color: #087f5b;
  font-size: 13px;
  font-weight: 650;
}

.result-summary.failed .summary-line span {
  background: rgba(255, 56, 92, .12);
  color: var(--mm-primary);
}

.summary-line em {
  color: var(--mm-muted);
  font-size: 13px;
  font-style: normal;
}

@media (max-width: 760px) {
    .panel-head {
    display: grid;
  }

  .page-actions {
    flex-wrap: wrap;
  }
}
</style>
