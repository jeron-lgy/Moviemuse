<template>
  <component
    :is="tag"
    :class="classes"
    v-bind="$attrs"
    :disabled="isNativeButton ? disabled : undefined"
    :aria-disabled="!isNativeButton && disabled ? 'true' : undefined"
  >
    <slot />
  </component>
</template>

<script setup>
import { computed } from 'vue'
import { RouterLink } from 'vue-router'

const props = defineProps({
  as: {
    type: String,
    default: 'button'
  },
  variant: {
    type: String,
    default: 'default'
  },
  size: {
    type: String,
    default: 'md'
  },
  disabled: {
    type: Boolean,
    default: false
  }
})

const tag = computed(() => (props.as === 'RouterLink' ? RouterLink : props.as))
const isNativeButton = computed(() => props.as === 'button')
const classes = computed(() => [
  'mm-button',
  props.variant === 'primary' ? 'primary' : '',
  props.variant === 'danger' ? 'danger' : '',
  props.variant === 'ghost' ? 'ghost' : '',
  props.size === 'sm' ? 'sm' : '',
  props.size === 'lg' ? 'lg' : '',
  props.disabled ? 'disabled' : ''
])
</script>
