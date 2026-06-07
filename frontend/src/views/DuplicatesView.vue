<template>
  <section class="duplicates-view">
    <PageHeader kicker="媒体扫描" title="重复视频" description="扫描媒体目录，查看重复组和单文件统计，继续复用现有扫描 API。">
      <template #actions>
        <BaseButton type="button" :disabled="loading" @click="loadScan">刷新</BaseButton>
        <BaseButton variant="primary" type="button" :disabled="running" @click="runScan">
          {{ running ? '扫描中' : '开始扫描' }}
        </BaseButton>
      </template>
    </PageHeader>

    <NoticeBanner v-if="message" >{{ message }}</NoticeBanner>
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

    <BaseCard class="scan-card" >
      <div class="panel-head">
        <div>
          <h2>扫描目录</h2>
          <p>默认勾选全部可扫描目录。</p>
        </div>
        <span class="mm-pill">{{ selectedPaths.length }} 个目录</span>
      </div>
      <div class="dir-list">
        <label v-for="path in scan.selectable_scan_dirs || []" :key="path" class="dir-row">
          <input v-model="selectedPaths" type="checkbox" :value="path">{{ path }}
        </label>
      </div>
    </BaseCard>

    <BaseCard class="groups-card" >
      <div class="panel-head">
        <div>
          <h2>重复组</h2>
          <p>先展示每组前几个文件，完整文件可在详情中展开。</p>
        </div>
        <span class="mm-pill">{{ groups.length }} 组</span>
      </div>

      <div v-if="groups.length" class="group-list">
        <article v-for="group in groups" :key="group.key" class="group-row">
          <div class="group-title">
            <strong>{{ group.files?.[0]?.name || group.key }}</strong>
            <span>{{ group.files?.length || 0 }} 个文件</span>
          </div>
          <ul>
            <li v-for="file in (group.files || []).slice(0, 4)" :key="file.path">
              <span>{{ file.name || file.path }}</span>
              <em>{{ formatSize(file.size) }}</em>
            </li>
          </ul>
          <details v-if="(group.files || []).length > 4">
            <summary>查看全部</summary>
            <p v-for="file in group.files" :key="`all-${file.path}`">{{ file.path }}</p>
          </details>
        </article>
      </div>
      <div v-else class="empty-state">当前没有重复组数据。</div>
    </BaseCard>
  </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../lib/api'

const scan = ref({})
const selectedPaths = ref([])
const loading = ref(false)
const running = ref(false)
const message = ref('')
const errorMessage = ref('')
const groups = computed(() => scan.value.groups || [])
const percent = computed(() => Math.round(Number(scan.value.progress || 0) * 100))

onMounted(loadScan)

async function loadScan() {
  loading.value = true
  errorMessage.value = ''
  try {
    scan.value = await api('/api/scan')
    if (!selectedPaths.value.length) {
      selectedPaths.value = [...(scan.value.selectable_scan_dirs || [])]
    }
  } catch (error) {
    errorMessage.value = error.message || '读取扫描状态失败'
  } finally {
    loading.value = false
  }
}

async function runScan() {
  running.value = true
  message.value = ''
  errorMessage.value = ''
  try {
    const form = new FormData()
    for (const path of selectedPaths.value) form.append('paths', path)
    const response = await fetch('/api/scan/run', { method: 'POST', body: form })
    const payload = await response.json()
    if (!response.ok) throw new Error(payload.detail || '启动扫描失败')
    message.value = `扫描已启动：${payload.scan_dirs?.length || 0} 个目录`
    await loadScan()
  } catch (error) {
    errorMessage.value = error.message || '启动扫描失败'
  } finally {
    running.value = false
  }
}

function formatSize(bytes) {
  const value = Number(bytes || 0)
  if (!value) return '-'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let size = value
  let index = 0
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024
    index += 1
  }
  return `${size.toFixed(index ? 1 : 0)} ${units[index]}`
}
</script>

<style scoped>
.duplicates-view {
  display: grid;
  gap: 24px;
}

.panel-head,
.page-actions {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.page-actions {
  align-items: center;
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
.panel-head p {
  margin-top: 8px;
  color: var(--mm-muted);
  line-height: 1.7;
}

.summary-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 16px;
}

.summary-card,
.scan-card,
.groups-card {
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

.dir-list,
.group-list {
  display: grid;
  gap: 12px;
  margin-top: 18px;
}

.dir-row,
.group-row {
  border: 1px solid var(--mm-border);
  border-radius: 14px;
  background: #fff;
}

.dir-row {
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: 48px;
  padding: 0 14px;
  color: var(--mm-muted);
}

.group-row {
  padding: 16px;
}

.group-title,
.group-row li {
  display: flex;
  justify-content: space-between;
  gap: 14px;
}

.group-title span,
.group-row em {
  color: var(--mm-muted);
  font-style: normal;
}

ul {
  display: grid;
  gap: 8px;
  margin: 12px 0 0;
  padding: 0;
  list-style: none;
}

li span {
  word-break: break-all;
}

details {
  margin-top: 12px;
  color: var(--mm-muted);
}

.empty-state {
  margin-top: 16px;
  padding: 32px;
  border: 1px dashed var(--mm-border);
  border-radius: 14px;
  color: var(--mm-muted);
  text-align: center;
}

@media (max-width: 980px) {
  .summary-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
