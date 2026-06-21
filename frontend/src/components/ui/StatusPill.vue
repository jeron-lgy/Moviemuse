<template>
  <span class="status-pill" :class="tone">
    <slot />
  </span>
</template>

<script setup>
defineProps({
  tone: {
    type: String,
    default: 'neutral'
  }
})
</script>

<style scoped>
.status-pill {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 28px;
  padding: 0 10px;
  border-radius: 999px;
  background: var(--mm-surface);
  color: var(--mm-muted);
  font-size: var(--mm-font-size-sm);
  font-weight: var(--mm-font-weight-medium);
  white-space: nowrap;
}

.status-pill.primary {
  background: var(--mm-primary-soft);
  color: var(--mm-primary);
}

.status-pill.success {
  background: var(--mm-success-soft);
  color: var(--mm-success);
}

.status-pill.danger {
  background: var(--mm-danger-soft);
  color: var(--mm-danger);
}
</style>
