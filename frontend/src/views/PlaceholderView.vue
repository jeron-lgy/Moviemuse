<!-- LEGACY: Placeholder for the old mixed UI migration. New routes should use concrete Vue views. -->
<template>
  <section class="placeholder-page">
    <div>
      <p class="eyebrow">{{ route.meta.module || 'MovieMuse' }}</p>
      <h1>{{ route.meta.title }}</h1>
      <p>这个页面已经接入新的 Vue Router 壳，下一步会把原有功能组件迁移进来，避免继续维护两套 UI。</p>
    </div>
    <BaseButton as="a"  :href="legacyHref">打开旧页面</BaseButton>
  </section>
</template>

<script setup>
import { computed } from 'vue'
import { useRoute } from 'vue-router'

const route = useRoute()
const legacyHref = computed(() => `${route.fullPath}?legacy=1`)
</script>

<style scoped>
.placeholder-page {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 24px;
}

.eyebrow {
  margin: 0 0 8px;
  color: var(--mm-primary);
  font-size: 13px;
  font-weight: 500;
}

h1 {
  margin: 0;
  color: var(--mm-text);
  font-size: 28px;
  font-weight: 600;
  letter-spacing: -0.2px;
}

p {
  max-width: 640px;
  margin: 10px 0 0;
  color: var(--mm-muted);
  font-size: 15px;
  line-height: 1.7;
}
</style>
