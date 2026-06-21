<template>
  <div class="mm-notice" :class="{ error: tone === 'error' }" role="status">
    <slot />
  </div>
</template>

<script setup>
defineProps({
  tone: {
    type: String,
    default: 'info'
  }
})
</script>
