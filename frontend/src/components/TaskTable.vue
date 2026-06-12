<template>
  <div class="task-table">
    <div class="task-row head">
      <span></span>
      <span>#</span>
      <span>番号 / 任务</span>
      <span>阶段</span>
      <span>路径</span>
      <span>创建时间</span>
      <span>状态</span>
      <span>操作</span>
    </div>
    <article v-for="job in jobs" :key="job.id" class="task-row">
      <input class="select-cell" :checked="isSelected(job.id)" type="checkbox" @change="$emit('toggle', job.id)">
      <span class="id-cell" :title="taskId(job)">{{ shortTaskId(job) }}</span>
      <div class="title-cell">
        <strong>{{ job.title }}</strong>
        <em>{{ job.modelLabel }}</em>
      </div>
      <span class="phase-cell mm-pill">{{ job.phaseLabel }}</span>
      <div class="path-cell">
        <p>{{ job.path }}</p>
        <div v-if="job.showProgress" class="progress-line">
          <span class="progress-track">
            <i :style="{ width: `${job.progressPercent || 0}%` }"></i>
          </span>
          <strong>{{ job.progressLabel || '0%' }}</strong>
        </div>
        <em v-if="job.progressDetail" class="progress-detail">{{ job.progressDetail }}</em>
      </div>
      <span class="created-cell">{{ job.createdLabel }}</span>
      <span :class="['status-pill', job.statusKey]">{{ job.statusLabel }}</span>
      <div class="row-actions">
        <BaseButton v-if="job.canRetry" type="button" :disabled="retrying[job.id]" @click="$emit('retry', job)">
          {{ retrying[job.id] ? '重试中' : '重试' }}
        </BaseButton>
        <BaseButton v-if="job.canCancel" type="button" @click="$emit('cancel', job)">取消</BaseButton>
        <BaseButton as="a" v-if="job.resultSrt" :href="`/subtitles/jobs/${job.fileId}/files/translated_srt`">结果</BaseButton>
      </div>
    </article>
  </div>
</template>

<script setup>
const props = defineProps({
  jobs: {
    type: Array,
    default: () => []
  },
  selectedIds: {
    type: Object,
    default: () => new Set()
  },
  retrying: {
    type: Object,
    default: () => ({})
  }
})

defineEmits(['toggle', 'retry', 'cancel'])

function isSelected(id) {
  return typeof props.selectedIds?.has === 'function' && props.selectedIds.has(id)
}

function taskId(job) {
  return String(job.rawId || job.fileId || '-')
}

function shortTaskId(job) {
  const value = taskId(job)
  return value.length > 10 ? value.slice(0, 8) : value
}
</script>

<style scoped>
.task-table {
  overflow: auto;
  border: 1px solid var(--mm-border);
  border-radius: var(--mm-radius-md);
}

.task-row {
  display: grid;
  grid-template-columns: 34px 92px minmax(240px, .95fr) 112px minmax(340px, 1.3fr) 132px 124px 150px;
  gap: 16px;
  align-items: center;
  min-width: 1280px;
  padding: 16px;
  border-bottom: 1px solid var(--mm-border);
}

.task-row:last-child {
  border-bottom: 0;
}

.task-row.head {
  background: var(--mm-surface);
  color: var(--mm-muted);
  font-size: var(--mm-font-size-sm);
  font-weight: var(--mm-font-weight-semibold);
}

.task-row.head > span,
.select-cell,
.id-cell,
.phase-cell,
.created-cell,
.status-pill,
.row-actions {
  justify-self: center;
}

.task-row.head > span:nth-child(3),
.task-row.head > span:nth-child(5) {
  justify-self: start;
}

.task-row strong {
  display: block;
  min-width: 0;
  font-weight: var(--mm-font-weight-semibold);
}

.task-row em {
  display: block;
  margin-top: 3px;
  color: var(--mm-muted);
  font-size: var(--mm-font-size-sm);
  font-style: normal;
}

.select-cell {
  width: 18px;
  min-height: 18px;
}

.id-cell {
  overflow: hidden;
  max-width: 100%;
  color: var(--mm-text);
  font-variant-numeric: tabular-nums;
  line-height: 1.2;
  text-align: center;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.title-cell {
  display: grid;
  gap: 2px;
  min-width: 0;
  justify-self: stretch;
  text-align: left;
}

.title-cell strong,
.title-cell em {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.mm-pill {
  display: inline-flex;
  justify-content: center;
  min-width: 84px;
}

.path-cell {
  min-width: 0;
}

.path-cell p {
  overflow: hidden;
  margin: 0;
  color: var(--mm-muted);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.progress-line {
  display: grid;
  grid-template-columns: minmax(120px, 1fr) auto;
  gap: 10px;
  align-items: center;
  margin-top: 8px;
}

.progress-track {
  overflow: hidden;
  height: 8px;
  border-radius: 999px;
  background: var(--mm-primary-soft);
}

.progress-track i {
  display: block;
  width: 0;
  height: 100%;
  border-radius: inherit;
  background: var(--mm-primary);
  transition: width .25s ease;
}

.progress-line strong {
  color: var(--mm-primary);
  font-size: var(--mm-font-size-sm);
  font-weight: var(--mm-font-weight-semibold);
}

.progress-detail {
  overflow: hidden;
  margin-top: 6px;
  color: var(--mm-muted);
  font-size: var(--mm-font-size-sm);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.created-cell {
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.status-pill {
  display: inline-flex;
  justify-content: center;
  min-width: 96px;
  min-height: 28px;
  padding: 4px 10px;
  border-radius: 999px;
  background: var(--mm-surface);
  color: var(--mm-muted);
  font-weight: var(--mm-font-weight-medium);
}

.status-pill.running,
.status-pill.failed,
.status-pill.translating {
  background: var(--mm-primary-soft);
  color: var(--mm-primary);
}

.status-pill.completed {
  background: var(--mm-success-soft);
  color: var(--mm-success);
}

.row-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  justify-content: center;
}
</style>
