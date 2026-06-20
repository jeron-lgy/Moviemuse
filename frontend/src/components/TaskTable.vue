<template>
  <div class="task-table">
    <div class="task-row head">
      <span></span>
      <span>#</span>
      <span>番号 / 任务</span>
      <span>阶段</span>
      <span>路径</span>
      <span>进度</span>
      <span>状态</span>
    </div>
    <article v-for="job in jobs" :key="job.id" class="task-row">
      <input class="select-cell" :checked="isSelected(job.id)" type="checkbox" @change="$emit('toggle', job.id)">
      <span class="id-cell" :title="taskId(job)">{{ taskId(job) }}</span>
      <div class="title-cell">
        <strong>{{ job.title }}</strong>
        <em>{{ job.modelLabel }} · {{ job.createdLabel }}</em>
      </div>
      <span class="phase-cell mm-pill">{{ job.phaseLabel }}</span>
      <div class="path-cell">
        <p>{{ job.path }}</p>
        <em v-if="job.progressDetail" class="progress-detail">{{ job.progressDetail }}</em>
      </div>
      <div class="progress-cell">
        <span :class="['progress-track', job.progressTone || job.statusKey]">
          <i :style="{ width: `${progressWidth(job)}%` }"></i>
        </span>
        <strong v-if="job.showProgress || job.progressLabel">{{ job.progressLabel || `${progressWidth(job)}%` }}</strong>
      </div>
      <span :class="['status-pill', job.statusKey]">{{ job.statusLabel }}</span>
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

function progressWidth(job) {
  if (job.statusKey === 'completed') return 100
  return Math.max(0, Math.min(100, Number(job.progressPercent || 0)))
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
  grid-template-columns: 32px minmax(230px, .56fr) minmax(220px, .82fr) 96px minmax(360px, 1.25fr) minmax(320px, .86fr) 108px;
  gap: 14px;
  align-items: center;
  min-width: 1450px;
  padding: 14px 16px;
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
.progress-cell,
.status-pill {
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
  overflow: visible;
  max-width: 100%;
  color: var(--mm-text);
  font-size: var(--mm-font-size-sm);
  font-variant-numeric: tabular-nums;
  justify-self: stretch;
  line-height: 1.2;
  text-align: left;
  text-overflow: clip;
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

.progress-cell {
  display: grid;
  grid-template-columns: minmax(260px, 1fr) 44px;
  gap: 10px;
  align-items: center;
  width: 100%;
  max-width: 420px;
}

.progress-track {
  overflow: hidden;
  height: 7px;
  border-radius: 999px;
  background: var(--mm-surface);
}

.progress-track i {
  display: block;
  width: 0;
  height: 100%;
  border-radius: inherit;
  background: color-mix(in srgb, var(--mm-muted) 35%, var(--mm-surface));
  transition: width .25s ease;
}

.progress-track.completed i {
  background: color-mix(in srgb, var(--mm-muted) 35%, var(--mm-surface));
}

.progress-track.detached i {
  background: color-mix(in srgb, var(--mm-muted) 55%, var(--mm-surface));
}

.progress-track.failed i {
  background: var(--mm-danger);
}

.progress-track.queued i,
.progress-track.idle i {
  background: color-mix(in srgb, var(--mm-muted) 28%, var(--mm-surface));
}

.progress-cell strong {
  color: var(--mm-primary);
  font-size: var(--mm-font-size-sm);
  font-weight: var(--mm-font-weight-semibold);
  text-align: right;
}

.progress-detail {
  overflow: hidden;
  margin-top: 6px;
  color: var(--mm-muted);
  font-size: var(--mm-font-size-sm);
  text-overflow: ellipsis;
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

.status-pill.detached {
  background: var(--mm-surface);
  color: var(--mm-muted);
}

</style>
