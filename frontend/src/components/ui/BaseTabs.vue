<template>
  <div class="base-tabs" role="tablist">
    <button
      v-for="tab in tabs"
      :key="tab.value"
      type="button"
      :class="{ active: modelValue === tab.value }"
      role="tab"
      :aria-selected="modelValue === tab.value"
      @click="$emit('update:modelValue', tab.value)"
    >
      {{ tab.label }}
      <em v-if="tab.count !== undefined">{{ tab.count }}</em>
    </button>
  </div>
</template>

<script setup>
defineEmits(['update:modelValue'])
defineProps({
  modelValue: {
    type: [String, Number],
    required: true
  },
  tabs: {
    type: Array,
    required: true
  }
})
</script>

<style scoped>
.base-tabs {
  display: inline-flex;
  flex-wrap: wrap;
  gap: 4px;
  padding: 4px;
  border: 1px solid var(--mm-border);
  border-radius: var(--mm-radius-md);
  background: var(--mm-surface);
}

.base-tabs button {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-height: var(--mm-button-height-sm);
  padding: var(--mm-button-padding-sm);
  border: 1px solid transparent;
  border-radius: var(--mm-radius-sm);
  background: transparent;
  color: var(--mm-muted);
  font-size: var(--mm-font-size-body);
  font-weight: var(--mm-font-weight-medium);
  cursor: pointer;
}

.base-tabs button.active {
  border-color: rgba(255, 56, 92, .18);
  background: var(--mm-card-bg);
  color: var(--mm-primary);
  box-shadow: var(--mm-shadow);
}

.base-tabs em {
  min-width: 24px;
  min-height: 24px;
  padding: 0 8px;
  border-radius: 999px;
  background: var(--mm-primary-soft);
  color: var(--mm-primary);
  font-size: var(--mm-font-size-sm);
  font-style: normal;
  line-height: 24px;
  text-align: center;
}
</style>
