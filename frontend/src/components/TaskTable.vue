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
      <input :checked="isSelected(job.id)" type="checkbox" @change="$emit('toggle', job.id)">
      <span>{{ job.rawId || job.fileId || '-' }}</span>
      <div>
        <strong>{{ job.title }}</strong>
        <em>{{ job.modelLabel }}</em>
      </div>
      <span class="mm-pill">{{ job.phaseLabel }}</span>
      <p>{{ job.path }}</p>
      <span>{{ job.createdLabel }}</span>
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
</script>

<style scoped>
.task-table {
  overflow: auto;
  border: 1px solid var(--mm-border);
  border-radius: var(--mm-radius-md);
}

.task-row {
  display: grid;
  grid-template-columns: 34px 72px minmax(180px, .9fr) 90px minmax(260px, 1.25fr) 140px 110px 180px;
  gap: 14px;
  align-items: center;
  min-width: 1180px;
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

.task-row strong {
  display: block;
  font-weight: var(--mm-font-weight-semibold);
}

.task-row em {
  display: block;
  margin-top: 4px;
  color: var(--mm-muted);
  font-size: var(--mm-font-size-sm);
  font-style: normal;
}

.task-row p {
  overflow: hidden;
  color: var(--mm-muted);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.status-pill {
  display: inline-flex;
  justify-content: center;
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
}
</style>
