<template>
  <section class="scan-api-view">
    <PageHeader kicker="扫描" title="扫描 API" description="查看重复视频扫描接口的实时返回，确认扫描状态、目录和结果数量。">
      <template #actions>
        <BaseButton type="button" :disabled="loading" @click="loadScan">刷新</BaseButton>
      </template>
    </PageHeader>

    <NoticeBanner v-if="errorMessage" tone="error">{{ errorMessage }}</NoticeBanner>

    <section class="summary-grid">
      <BaseCard as="article" class="summary-card" >
        <span>状态</span>
        <strong>{{ scan.status || 'idle' }}</strong>
      </BaseCard>
      <BaseCard as="article" class="summary-card" >
        <span>进度</span>
        <strong>{{ percent }}%</strong>
      </BaseCard>
      <BaseCard as="article" class="summary-card" >
        <span>重复组</span>
        <strong>{{ scan.duplicate_groups || 0 }}</strong>
      </BaseCard>
      <BaseCard as="article" class="summary-card" >
        <span>重复文件</span>
        <strong>{{ scan.duplicate_files || 0 }}</strong>
      </BaseCard>
    </section>

    <BaseCard class="raw-card" >
      <div class="panel-head">
        <h2>原始响应</h2>
        <span class="mm-pill">{{ scan.files?.length || 0 }} 个文件</span>
      </div>
      <pre>{{ JSON.stringify(scan, null, 2) }}</pre>
    </BaseCard>
  </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../lib/api'

const loading = ref(false)
const errorMessage = ref('')
const scan = ref({})
const percent = computed(() => Math.round(Number(scan.value.progress || 0) * 100))

onMounted(loadScan)

async function loadScan() {
  loading.value = true
  errorMessage.value = ''
  try {
    scan.value = await api('/api/scan')
  } catch (error) {
    errorMessage.value = error.message || '读取扫描 API 失败'
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.scan-api-view {
  display: grid;
  gap: 24px;
}

.panel-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.eyebrow,
h1,
h2,
p {
  margin: 0;
}

.eyebrow {
  color: var(--mm-primary);
  font-size: 13px;
  font-weight: 600;
}

h1 {
  font-size: 30px;
  font-weight: 650;
}

.summary-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 16px;
}

.summary-card,
.raw-card {
  padding: 24px;
}

.summary-card {
  display: grid;
  gap: 12px;
}

.summary-card span {
  color: var(--mm-muted);
  font-weight: 500;
}

.summary-card strong {
  font-size: 34px;
  font-weight: 650;
}

pre {
  max-height: 68vh;
  overflow: auto;
  margin: 18px 0 0;
  padding: 18px;
  border-radius: 14px;
  background: var(--mm-surface);
  color: var(--mm-muted);
  white-space: pre-wrap;
}

@media (max-width: 980px) {
  .summary-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
