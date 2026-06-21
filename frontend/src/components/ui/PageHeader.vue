<template>
  <header class="mm-page-head">
    <div>
      <p v-if="kicker" class="mm-page-kicker">{{ kicker }}</p>
      <h1 class="mm-page-title">{{ title }}</h1>
      <p v-if="description" class="mm-page-description">{{ description }}</p>
    </div>
    <div v-if="$slots.actions" class="mm-actions">
      <slot name="actions" />
    </div>
  </header>
</template>

<script setup>
defineProps({
  kicker: {
    type: String,
    default: ''
  },
  title: {
    type: String,
    required: true
  },
  description: {
    type: String,
    default: ''
  }
})
</script>
