<template>
  <BaseButton
    :class="['subscription-hover-button', { cancelling: isCancelIntent }]"
    :variant="isCancelIntent ? 'danger' : 'primary'"
    :size="size"
    type="button"
    :disabled="disabled || busy"
    @mouseenter="hovering = true"
    @mouseleave="hovering = false"
    @focus="hovering = true"
    @blur="hovering = false"
    @click="$emit('click', $event)"
  >
    {{ displayLabel }}
  </BaseButton>
</template>

<script setup>
import { computed, ref } from 'vue'
import { BaseButton } from './ui'

const props = defineProps({
  label: {
    type: String,
    default: '已订阅'
  },
  hoverLabel: {
    type: String,
    default: '取消'
  },
  busyLabel: {
    type: String,
    default: '取消中'
  },
  size: {
    type: String,
    default: 'md'
  },
  disabled: {
    type: Boolean,
    default: false
  },
  busy: {
    type: Boolean,
    default: false
  }
})

defineEmits(['click'])

const hovering = ref(false)
const isCancelIntent = computed(() => hovering.value && !props.disabled && !props.busy)
const displayLabel = computed(() => {
  if (props.busy) return props.busyLabel
  return isCancelIntent.value ? props.hoverLabel : props.label
})
</script>
