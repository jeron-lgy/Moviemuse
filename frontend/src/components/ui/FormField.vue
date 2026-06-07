<template>
  <label class="mm-field" :class="{ wide }">
    <span>{{ label }}</span>
    <slot />
    <em v-if="hint">{{ hint }}</em>
  </label>
</template>

<script setup>
defineProps({
  label: {
    type: String,
    required: true
  },
  hint: {
    type: String,
    default: ''
  },
  wide: {
    type: Boolean,
    default: false
  }
})
</script>

<style scoped>
.wide {
  grid-column: 1 / -1;
}
</style>
