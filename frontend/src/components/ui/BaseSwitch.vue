<template>
  <label class="mm-switch" :class="[sizeClass, { checked: modelValue, disabled }]">
    <input
      class="mm-switch-input"
      type="checkbox"
      :checked="modelValue"
      :disabled="disabled"
      :aria-label="ariaLabel || label"
      @change="emit('update:modelValue', $event.target.checked)"
    >
    <span class="mm-switch-track" aria-hidden="true">
      <span class="mm-switch-thumb"></span>
    </span>
    <span v-if="label" class="mm-switch-label">{{ label }}</span>
  </label>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  modelValue: {
    type: Boolean,
    default: false
  },
  label: {
    type: String,
    default: ''
  },
  ariaLabel: {
    type: String,
    default: ''
  },
  disabled: {
    type: Boolean,
    default: false
  },
  size: {
    type: String,
    default: 'md'
  }
})

const emit = defineEmits(['update:modelValue'])
const sizeClass = computed(() => (props.size === 'sm' ? 'sm' : ''))
</script>

<style scoped>
.mm-switch {
  --switch-width: 44px;
  --switch-height: 24px;
  --switch-thumb: 24px;
  --switch-offset: 20px;

  position: relative;
  display: inline-flex;
  align-items: center;
  gap: 10px;
  min-height: var(--switch-height);
  color: var(--mm-text);
  font-size: var(--mm-font-size-body);
  font-weight: var(--mm-font-weight-medium);
  cursor: pointer;
  user-select: none;
}

.mm-switch.sm {
  --switch-width: 34px;
  --switch-height: 18px;
  --switch-thumb: 18px;
  --switch-offset: 16px;
  gap: 8px;
  font-size: var(--mm-font-size-sm);
}

.mm-switch.disabled {
  cursor: not-allowed;
  opacity: .56;
}

.mm-switch-input {
  position: absolute;
  width: 1px;
  height: 1px;
  margin: -1px;
  padding: 0;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
  border: 0;
}

.mm-switch-track {
  position: relative;
  width: var(--switch-width);
  height: var(--switch-height);
  flex: 0 0 var(--switch-width);
  border-radius: 999px;
  background: color-mix(in srgb, var(--mm-muted) 22%, var(--mm-control-bg));
  transition: background .18s ease, box-shadow .18s ease;
}

.mm-switch-thumb {
  position: absolute;
  top: 50%;
  left: 0;
  width: var(--switch-thumb);
  height: var(--switch-thumb);
  border-radius: 999px;
  background: var(--mm-card-bg);
  box-shadow: 0 3px 10px rgba(34, 34, 34, .18);
  transform: translate(0, -50%);
  transition: transform .18s ease, box-shadow .18s ease;
}

.mm-switch.checked .mm-switch-track {
  background: color-mix(in srgb, var(--mm-primary) 45%, var(--mm-card-bg));
}

.mm-switch.checked .mm-switch-thumb {
  background: var(--mm-primary);
  box-shadow: 0 5px 14px color-mix(in srgb, var(--mm-primary) 38%, transparent);
  transform: translate(var(--switch-offset), -50%);
}

.mm-switch-input:focus-visible + .mm-switch-track {
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--mm-primary) 24%, transparent);
}

.mm-switch-label {
  line-height: 1.4;
}
</style>
