<template>
  <component :is="as" class="mm-card base-card" :class="[`pad-${padding}`]">
    <slot />
  </component>
</template>

<script setup>
defineProps({
  as: {
    type: String,
    default: 'section'
  },
  padding: {
    type: String,
    default: 'md'
  }
})
</script>

<style scoped>
.base-card.pad-none {
  padding: 0;
}

.base-card.pad-sm {
  padding: var(--mm-space-4);
}

.base-card.pad-md {
  padding: var(--mm-space-6);
}

.base-card.pad-lg {
  padding: var(--mm-space-8);
}
</style>
