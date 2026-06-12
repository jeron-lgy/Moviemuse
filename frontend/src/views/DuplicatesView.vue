<template>
  <section class="duplicates-view">
    <PageHeader
      kicker="媒体扫描"
      title="重复视频"
      description="扫描重复视频，批量移动或发送字幕。"
    >
      <template #actions>
        <BaseButton type="button" :disabled="loading" @click="loadScan">刷新</BaseButton>
        <BaseButton variant="primary" type="button" :disabled="running" @click="runScan">
          {{ running ? '扫描中' : '开始扫描' }}
        </BaseButton>
      </template>
    </PageHeader>

    <NoticeBanner v-if="message">{{ message }}</NoticeBanner>
    <NoticeBanner v-if="errorMessage" tone="error">{{ errorMessage }}</NoticeBanner>

    <BaseCard class="status-card">
      <div class="status-head">
        <div>
          <h2>状态栏</h2>
          <p>{{ scan.current_path || '等待扫描。' }}</p>
        </div>
        <span class="mm-pill">{{ formatStatus(scan.status) }}</span>
      </div>
      <div class="status-metrics">
        <article v-for="item in statusItems" :key="item.label">
          <span>{{ item.label }}</span>
          <strong>{{ item.value }}</strong>
          <em>{{ item.detail }}</em>
        </article>
      </div>
      <div class="progress-track" aria-label="扫描进度">
        <i :style="{ width: `${percent}%` }"></i>
      </div>
    </BaseCard>

    <section class="workbench-grid">
      <BaseCard class="scan-card">
        <div class="panel-head">
          <div>
            <h2>扫描目录</h2>
            <p>选择扫描范围。</p>
          </div>
          <span class="mm-pill">{{ selectedPaths.length }} 个目录</span>
        </div>
        <div class="dir-list">
          <label v-for="path in scan.selectable_scan_dirs || []" :key="path" class="dir-row">
            <input v-model="selectedPaths" type="checkbox" :value="path">
            <span>{{ path }}</span>
          </label>
        </div>
        <div v-if="!(scan.selectable_scan_dirs || []).length" class="compact-empty">没有可扫描目录。</div>
      </BaseCard>

      <div class="right-stack">
        <BaseCard class="rules-card">
          <div class="panel-head">
            <div>
              <h2>批量操作菜单</h2>
              <p>批量勾选后再执行。</p>
            </div>
            <span class="mm-pill">{{ selectedActionCount }} 个已选</span>
          </div>

          <div class="batch-actions">
            <BaseButton
              type="button"
              :variant="autoRules.move ? 'danger' : ''"
              @click="toggleBatch('move')"
            >
              批量重复
            </BaseButton>
            <BaseButton
              type="button"
              :variant="autoRules.subtitle ? 'primary' : ''"
              @click="toggleBatch('subtitle')"
            >
              批量字幕
            </BaseButton>
            <BaseButton
              variant="danger"
              type="button"
              :disabled="!moveSelection.length || submittingAction"
              @click="submitPaths('/move/jobs', moveSelection)"
            >
              移动选中
            </BaseButton>
            <BaseButton
              variant="primary"
              type="button"
              :disabled="!subtitleSelection.length || submittingAction"
              @click="submitPaths('/scan/subtitles', subtitleSelection)"
            >
              发送到字幕
            </BaseButton>
          </div>
        </BaseCard>

        <BaseCard class="groups-card">
          <div class="panel-head groups-head">
            <div>
              <h2>重复组数据</h2>
              <p>移动优先于字幕。</p>
            </div>
            <div class="head-tools">
              <BaseButton type="button" size="sm" :disabled="!groups.length" @click="clearManualSelection">清空手动选择</BaseButton>
              <span class="mm-pill">{{ groups.length }} 组</span>
            </div>
          </div>
          <div class="selection-summary">
            <article>
              <span>待移动</span>
              <strong>{{ moveSelection.length }}</strong>
            </article>
            <article>
              <span>待字幕</span>
              <strong>{{ subtitleSelection.length }}</strong>
            </article>
            <article>
              <span>已选择</span>
              <strong>{{ selectedActionCount }}</strong>
            </article>
          </div>

          <div v-if="groups.length" class="group-list">
            <article v-for="group in groups" :key="group.key" class="group-row">
              <div class="group-title">
                <div>
                  <strong>{{ group.title || group.files?.[0]?.name || group.key }}</strong>
                  <span>{{ group.source || '重复组' }} · {{ group.files?.length || 0 }} 个文件</span>
                </div>
                <span class="mm-pill">{{ group.year || '未知年份' }}</span>
              </div>

              <div class="file-list">
                <article
                  v-for="file in group.files || []"
                  :key="file.path"
                  class="file-row"
                  :class="fileRowClass(group, file)"
                >
                  <label class="file-check" title="移动">
                    <input
                      type="checkbox"
                      :checked="manualMove.has(file.path)"
                      @change="setManualAction(file.path, 'move', $event.target.checked)"
                    >
                    <span>移</span>
                  </label>
                  <label class="file-check" title="发送到字幕">
                    <input
                      type="checkbox"
                      :checked="manualSubtitle.has(file.path)"
                      @change="setManualAction(file.path, 'subtitle', $event.target.checked)"
                    >
                    <span>字</span>
                  </label>
                  <div class="file-main">
                    <strong>{{ file.name || file.path }}</strong>
                    <span>{{ file.path }}</span>
                  </div>
                  <div class="file-meta">
                    <span>{{ formatSize(file.size_bytes) }}</span>
                    <span>{{ file.resolution || '未知' }}</span>
                    <span>{{ file.subtitle_label || '无字幕' }}</span>
                    <span v-if="file.uncensored">无码</span>
                  </div>
                  <em v-if="hitLabel(group, file)" class="hit-badge">{{ hitLabel(group, file) }}</em>
                </article>
              </div>
            </article>
          </div>
          <div v-else class="empty-state">当前没有重复组数据。</div>
        </BaseCard>
      </div>
    </section>
  </section>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { api } from '../lib/api'

const scan = ref({})
const selectedPaths = ref([])
const loading = ref(false)
const running = ref(false)
const submittingAction = ref(false)
const message = ref('')
const errorMessage = ref('')
const autoRules = reactive({ move: false, subtitle: false })
const manualMove = reactive(new Set())
const manualSubtitle = reactive(new Set())

const groups = computed(() => scan.value.groups || [])
const allRows = computed(() => groups.value.flatMap((group) => group.files || []))
const percent = computed(() => Math.round(Number(scan.value.progress || 0) * 100))
const autoMoveRows = computed(() => matchedMoveRows())
const autoSubtitleRows = computed(() => matchedSubtitleRows())
const moveSelection = computed(() => uniquePaths([
  ...(autoRules.move ? autoMoveRows.value.map((row) => row.path) : []),
  ...Array.from(manualMove)
]))
const subtitleSelection = computed(() => {
  const movePaths = new Set(moveSelection.value)
  return uniquePaths([
    ...(autoRules.subtitle ? autoSubtitleRows.value.map((row) => row.path) : []),
    ...Array.from(manualSubtitle)
  ]).filter((path) => !movePaths.has(path))
})
const selectedActionCount = computed(() => moveSelection.value.length + subtitleSelection.value.length)
const statusItems = computed(() => [
  { label: '进度', value: `${percent.value}%`, detail: `${scan.value.processed_files || 0} / ${scan.value.scan_total_files || 0}` },
  { label: '重复组', value: scan.value.duplicate_groups || 0, detail: `${scan.value.total_files || 0} 个媒体文件` },
  { label: '重复文件', value: scan.value.duplicate_files || 0, detail: `${scan.value.single_files?.length || 0} 个单文件` },
  { label: '待处理', value: selectedActionCount.value, detail: `${moveSelection.value.length} 移动 / ${subtitleSelection.value.length} 字幕` }
])

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

function groupStats(group) {
  const rows = (group.files || []).filter((row) => !row.ignored)
  return {
    has4k: rows.some((row) => row.resolution === '4K'),
    hasSubtitle: rows.some((row) => row.subtitle_kind !== 'none'),
    hasUncensored: rows.some((row) => row.uncensored)
  }
}

function isLowPriority(row) {
  return !row.ignored
    && row.resolution !== '4K'
    && row.subtitle_kind === 'none'
    && !row.uncensored
}

function matchesMoveStrategy(group, row) {
  const stats = groupStats(group)
  return (stats.has4k || stats.hasSubtitle || stats.hasUncensored) && isLowPriority(row)
}

function matchesSubtitleStrategy(row) {
  return !row.ignored && row.subtitle_kind === 'none' && !row.uncensored
}

function matchedMoveRows() {
  return groups.value.flatMap((group) => (group.files || [])
    .filter((row) => matchesMoveStrategy(group, row)))
}

function matchedSubtitleRows() {
  return allRows.value.filter((row) => matchesSubtitleStrategy(row))
}

function setManualAction(path, action, checked) {
  if (!path) return
  if (action === 'move') {
    if (checked) {
      manualMove.add(path)
      manualSubtitle.delete(path)
    } else {
      manualMove.delete(path)
    }
    return
  }
  if (checked) {
    manualSubtitle.add(path)
    manualMove.delete(path)
  } else {
    manualSubtitle.delete(path)
  }
}

function toggleBatch(action) {
  if (action === 'move') {
    autoRules.move = !autoRules.move
    return
  }
  autoRules.subtitle = !autoRules.subtitle
}

function clearManualSelection() {
  manualMove.clear()
  manualSubtitle.clear()
}

function hitLabel(group, file) {
  if (!file.path) return ''
  if (moveSelection.value.includes(file.path)) return '移动'
  if (subtitleSelection.value.includes(file.path)) return '字幕'
  if (matchesMoveStrategy(group, file)) return '可移动'
  if (matchesSubtitleStrategy(file)) return '可字幕'
  return ''
}

function fileRowClass(group, file) {
  const label = hitLabel(group, file)
  return {
    'move-hit': label === '移动',
    'subtitle-hit': label === '字幕',
    'soft-hit': label === '可移动' || label === '可字幕',
    ignored: file.ignored
  }
}

function submitPaths(action, paths) {
  if (!paths.length) return
  submittingAction.value = true
  const form = document.createElement('form')
  form.method = 'POST'
  form.action = action
  form.style.display = 'none'
  paths.forEach((path) => {
    const input = document.createElement('input')
    input.type = 'hidden'
    input.name = 'paths'
    input.value = path
    form.appendChild(input)
  })
  document.body.appendChild(form)
  form.submit()
}

function uniquePaths(paths) {
  return Array.from(new Set(paths.filter(Boolean)))
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

function formatStatus(status) {
  const labels = {
    idle: '待扫描',
    running: '扫描中',
    completed: '已完成',
    failed: '失败'
  }
  return labels[status] || status || '待扫描'
}
</script>

<style scoped>
.duplicates-view {
  display: grid;
  gap: 24px;
}

.status-card,
.scan-card,
.rules-card,
.groups-card {
  min-width: 0;
}

.status-card {
  display: grid;
  gap: 18px;
}

.status-head,
.panel-head,
.groups-head,
.group-title {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.scan-card .panel-head {
  display: grid;
  grid-template-columns: 1fr;
}

.scan-card .panel-head .mm-pill {
  justify-self: start;
}

.status-head h2,
.status-head p,
.panel-head h2,
.panel-head p,
.group-title strong,
.group-title span {
  margin: 0;
}

.status-head h2,
.panel-head h2 {
  font-size: 20px;
  font-weight: 650;
}

.status-head p,
.panel-head p {
  margin-top: 8px;
  color: var(--mm-muted);
  line-height: 1.6;
}

.status-metrics {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
}

.status-metrics article {
  display: grid;
  min-height: 116px;
  align-content: space-between;
  gap: 8px;
  padding: 16px;
  border: 1px solid var(--mm-border);
  border-radius: 8px;
  background: var(--mm-surface);
}

.status-metrics span,
.status-metrics em,
.group-title span,
.file-main span,
.file-meta,
.selection-summary span {
  color: var(--mm-muted);
}

.status-metrics span {
  font-weight: 550;
}

.status-metrics strong {
  overflow: hidden;
  font-size: 30px;
  font-weight: 650;
  line-height: 1.12;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.status-metrics em {
  overflow: hidden;
  font-size: 12px;
  font-style: normal;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.progress-track {
  height: 8px;
  overflow: hidden;
  border-radius: 999px;
  background: var(--mm-surface);
}

.progress-track i {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: var(--mm-primary);
  transition: width .2s ease;
}

.workbench-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
  align-items: stretch;
}

.right-stack {
  display: grid;
  grid-column: 2 / -1;
  min-width: 0;
  gap: 16px;
  grid-template-rows: auto minmax(420px, 1fr);
}

.scan-card {
  min-height: 680px;
}

.dir-list,
.group-list,
.file-list {
  display: grid;
  gap: 12px;
}

.dir-list {
  max-height: 580px;
  margin-top: 18px;
  overflow: auto;
  padding-right: 4px;
}

.dir-row {
  display: grid;
  grid-template-columns: 22px minmax(0, 1fr);
  align-items: center;
  min-height: 48px;
  gap: 10px;
  padding: 0 14px;
  border: 1px solid var(--mm-border);
  border-radius: 8px;
  background: #fff;
}

.dir-row input,
.file-check input {
  accent-color: var(--mm-primary);
}

.dir-row span {
  overflow: hidden;
  color: var(--mm-muted);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.rules-card {
  display: grid;
  align-content: start;
  gap: 18px;
}

.batch-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
}

.groups-card {
  display: grid;
  align-content: start;
}

.groups-head {
  align-items: center;
}

.head-tools {
  display: flex;
  align-items: center;
  gap: 10px;
}

.selection-summary {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
  margin-top: 18px;
}

.selection-summary article {
  display: grid;
  gap: 6px;
  min-height: 78px;
  align-content: center;
  padding: 14px 16px;
  border: 1px solid var(--mm-border);
  border-radius: 8px;
  background: var(--mm-surface);
}

.selection-summary span {
  color: var(--mm-muted);
  font-size: 13px;
  font-weight: 550;
}

.selection-summary strong {
  font-size: 26px;
  font-weight: 650;
  line-height: 1.1;
}

.status-head .mm-pill,
.panel-head .mm-pill,
.head-tools .mm-pill {
  white-space: nowrap;
}

.group-list {
  max-height: 860px;
  margin-top: 18px;
  overflow: auto;
  padding-right: 4px;
}

.group-row {
  display: grid;
  gap: 14px;
  padding: 16px;
  border: 1px solid var(--mm-border);
  border-radius: 8px;
  background: #fff;
}

.group-title div {
  display: grid;
  min-width: 0;
  gap: 5px;
}

.group-title strong {
  overflow: hidden;
  font-size: 16px;
  font-weight: 650;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.file-row {
  display: grid;
  grid-template-columns: 40px 40px minmax(0, 1.35fr) minmax(220px, .85fr) auto;
  align-items: center;
  gap: 10px;
  min-height: 64px;
  padding: 10px 12px;
  border: 1px solid var(--mm-border);
  border-radius: 8px;
  background: var(--mm-surface);
}

.file-row.move-hit {
  border-color: #FFB3C0;
  background: #FFF5F7;
}

.file-row.subtitle-hit {
  border-color: #B7DBC1;
  background: #F3FBF5;
}

.file-row.soft-hit {
  border-color: #E9D7A8;
  background: #FFFDF4;
}

.file-row.ignored {
  opacity: .62;
}

.file-check {
  display: grid;
  place-items: center;
  gap: 4px;
  min-width: 36px;
  color: var(--mm-muted);
  font-size: 12px;
  font-weight: 650;
}

.file-check input {
  width: 18px;
  height: 18px;
}

.file-main {
  display: grid;
  min-width: 0;
  gap: 4px;
}

.file-main strong,
.file-main span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.file-main strong {
  font-weight: 600;
}

.file-main span {
  font-size: 12px;
}

.file-meta {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 6px;
  font-size: 12px;
}

.file-meta span,
.hit-badge {
  display: inline-flex;
  align-items: center;
  min-height: 24px;
  padding: 0 8px;
  border: 1px solid var(--mm-border);
  border-radius: 999px;
  background: #fff;
}

.hit-badge {
  border-color: transparent;
  background: var(--mm-text);
  color: #fff;
  font-size: 12px;
  font-style: normal;
  font-weight: 650;
  white-space: nowrap;
}

.empty-state,
.compact-empty {
  padding: 28px;
  border: 1px dashed var(--mm-border);
  border-radius: 8px;
  color: var(--mm-muted);
  text-align: center;
}

.compact-empty {
  margin-top: 18px;
}

@media (max-width: 1180px) {
  .status-metrics {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .workbench-grid {
    grid-template-columns: 1fr;
    margin-inline: 0;
  }

  .scan-card {
    min-height: auto;
  }

  .dir-list,
  .group-list {
    max-height: none;
  }

  .right-stack {
    grid-template-rows: auto;
  }

  .file-row {
    grid-template-columns: 36px 36px minmax(0, 1fr) auto;
  }

  .file-meta {
    grid-column: 3 / -1;
    justify-content: flex-start;
  }
}

@media (max-width: 760px) {
  .status-head,
  .status-metrics,
  .panel-head,
  .group-title,
  .groups-head,
  .head-tools,
  .batch-actions,
  .selection-summary {
    display: grid;
    grid-template-columns: 1fr;
    justify-content: stretch;
  }

  .status-head .mm-pill,
  .panel-head .mm-pill,
  .head-tools .mm-pill {
    justify-self: start;
  }

  .file-row {
    grid-template-columns: 36px 36px minmax(0, 1fr);
  }

  .hit-badge {
    grid-column: 3;
    justify-self: start;
  }
}
</style>
