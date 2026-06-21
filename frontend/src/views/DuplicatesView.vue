<template>
  <section class="duplicates-view">
    <NoticeBanner v-if="message">{{ message }}</NoticeBanner>
    <NoticeBanner v-if="errorMessage" tone="error">{{ errorMessage }}</NoticeBanner>
    <NoticeBanner v-if="scanRecoveryMessage" :tone="scanRecoveryTone">
      <div class="scan-recovery-notice">
        <span>{{ scanRecoveryMessage }}</span>
        <BaseButton
          v-if="scan.can_cancel"
          type="button"
          size="sm"
          :disabled="cancellingScan"
          @click="cancelScanState"
        >
          {{ cancellingScan ? '终止中' : '终止扫描' }}
        </BaseButton>
      </div>
    </NoticeBanner>

    <BaseCard class="hero-card">
      <PageHeader
        kicker="媒体扫描"
        title="重复视频"
        description="扫描重复视频，批量移动或发送字幕。"
      >
        <template #actions>
          <BaseButton type="button" :disabled="loading" @click="loadScan">刷新</BaseButton>
          <BaseButton
            v-if="scan.status === 'running'"
            type="button"
            variant="danger"
            :disabled="cancellingScan"
            @click="cancelScanState"
          >
            {{ cancellingScan ? '终止中' : '终止扫描' }}
          </BaseButton>
          <BaseButton type="button" :disabled="scanRunning" @click="runScan('full')">
            全量扫描
          </BaseButton>
          <BaseButton variant="primary" type="button" :disabled="scanRunning" @click="runScan('incremental')">
            {{ scanRunning ? '扫描中' : '增量扫描' }}
          </BaseButton>
        </template>
      </PageHeader>
      <div class="status-metrics">
        <article v-for="item in statusItems" :key="item.label" :class="{ 'progress-metric': item.progress }">
          <span>{{ item.label }}</span>
          <strong>{{ item.value }}</strong>
          <em>{{ item.detail }}</em>
          <div v-if="item.progress" class="progress-track" aria-label="扫描进度">
            <i class="progress-fill" :style="{ width: `${percent}%` }"></i>
          </div>
        </article>
      </div>
    </BaseCard>

    <section class="workbench-grid" :style="{ '--scan-panel-width': `${scanPanelWidth}px` }">
      <BaseCard class="scan-card">
        <div class="panel-head">
          <div>
            <h2>扫描目录</h2>
            <p>选择扫描范围。</p>
          </div>
          <div class="scan-tools">
            <BaseButton type="button" size="sm" :disabled="!(scan.selectable_scan_dirs || []).length" @click="invertSelectedPaths">反选</BaseButton>
            <BaseButton
              type="button"
              size="sm"
              :disabled="savingSelection || !selectedPaths.length || !scanDirSelectionDirty"
              @click="saveSelectedPathDefaults"
            >
              {{ savingSelection ? '保存中' : '保存默认' }}
            </BaseButton>
            <span class="mm-pill">{{ selectedPaths.length }} / {{ selectableDirCount }} 个目录</span>
          </div>
        </div>
        <div class="scan-default-note" :class="{ dirty: scanDirSelectionDirty }">
          {{ scanDirSelectionDirty ? '当前选择尚未保存为默认范围。' : `已保存默认范围：${savedDirCount} 个目录。` }}
        </div>
        <div class="dir-list">
          <label v-for="path in scan.selectable_scan_dirs || []" :key="path" class="dir-row" :title="path">
            <input v-model="selectedPaths" type="checkbox" :value="path" @change="markSelectedPathsTouched">
            <span>{{ path }}</span>
          </label>
        </div>
        <div v-if="!(scan.selectable_scan_dirs || []).length" class="compact-empty">没有可扫描目录。</div>
      </BaseCard>

      <button
        class="resize-handle"
        type="button"
        aria-label="调整扫描目录宽度"
        title="拖动调整扫描目录宽度"
        @pointerdown="startResize"
      ></button>

      <BaseCard class="groups-card" padding="none">
        <div class="groups-toolbar">
          <div class="groups-toolbar-main">
            <div class="groups-copy">
              <h2>重复组数据</h2>
              <p>移动优先于字幕。</p>
            </div>
            <div class="batch-actions">
              <BaseButton type="button" size="sm" :variant="autoRules.move ? 'danger' : ''" @click="toggleBatch('move')">批量重复</BaseButton>
              <BaseButton type="button" size="sm" :variant="autoRules.subtitle ? 'primary' : ''" @click="toggleBatch('subtitle')">批量字幕</BaseButton>
              <BaseButton variant="danger" type="button" size="sm" :disabled="!moveSelection.length || submittingAction" @click="submitPaths('/move/jobs', moveSelection)">移动选中</BaseButton>
              <BaseButton variant="primary" type="button" size="sm" :disabled="!subtitleSelection.length || submittingAction" @click="submitPaths('/scan/subtitles', subtitleSelection)">发送到字幕</BaseButton>
              <BaseButton type="button" size="sm" :disabled="!groups.length && !singleFiles.length" @click="clearManualSelection">清空选择</BaseButton>
            </div>
          </div>
          <div class="groups-toolbar-sub">
            <div class="selection-summary">
              <span class="count-pill">待移动 <strong>{{ moveSelection.length }}</strong></span>
              <span class="count-pill">待字幕 <strong>{{ subtitleSelection.length }}</strong></span>
              <span class="count-pill">已选择 <strong>{{ selectedActionCount }}</strong></span>
              <span class="count-pill">重复 <strong>{{ groups.length }}</strong> 组</span>
              <span class="count-pill">单文件 <strong>{{ singleFiles.length }}</strong></span>
            </div>
            <div class="group-tools">
              <div class="result-filter" aria-label="筛选扫描结果">
                <button
                  type="button"
                  :class="{ active: resultFilter === 'all' }"
                  @click="resultFilter = 'all'"
                >
                  全部
                </button>
                <button
                  type="button"
                  :class="{ active: resultFilter === 'new' }"
                  @click="resultFilter = 'new'"
                >
                  新增
                </button>
              </div>
              <div class="group-search">
                <input
                  v-model.trim="searchQuery"
                  type="search"
                  placeholder="搜索文件名、路径、分组"
                  aria-label="搜索重复视频"
                >
              </div>
            </div>
          </div>
        </div>

        <div v-if="filteredGroups.length || filteredSingleFiles.length" class="group-list">
          <article v-for="group in filteredGroups" :key="group.key" class="group-row">
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
                  <strong>
                    <span>{{ file.name || file.path }}</span>
                    <em v-if="isNewFile(file)" class="new-badge">NEW</em>
                  </strong>
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

          <article v-if="filteredSingleFiles.length" class="group-row single-files-row">
            <div class="group-title">
              <div>
                <strong>单文件</strong>
                <span>未进入重复组 · {{ filteredSingleFiles.length }} 个文件</span>
              </div>
              <span class="mm-pill">非重复</span>
            </div>

            <div class="file-list">
              <article
                v-for="file in filteredSingleFiles"
                :key="file.path"
                class="file-row"
                :class="fileRowClass(null, file)"
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
                  <strong>
                    <span>{{ file.name || file.path }}</span>
                    <em v-if="isNewFile(file)" class="new-badge">NEW</em>
                  </strong>
                  <span>{{ file.path }}</span>
                </div>
                <div class="file-meta">
                  <span>{{ formatSize(file.size_bytes) }}</span>
                  <span>{{ file.resolution || '未知' }}</span>
                  <span>{{ file.subtitle_label || '无字幕' }}</span>
                  <span v-if="file.uncensored">无码</span>
                </div>
                <em v-if="hitLabel(null, file)" class="hit-badge">{{ hitLabel(null, file) }}</em>
              </article>
            </div>
          </article>
        </div>
        <div v-else class="empty-state">{{ emptyText }}</div>
      </BaseCard>
    </section>
  </section>
</template>

<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { api, postFormData } from '../lib/api'

const scan = ref({})
const selectedPaths = ref([])
const selectedPathsTouched = ref(false)
const loading = ref(false)
const running = ref(false)
const cancellingScan = ref(false)
const savingSelection = ref(false)
const submittingAction = ref(false)
const message = ref('')
const errorMessage = ref('')
const searchQuery = ref('')
const resultFilter = ref('all')
const scanPanelWidth = ref(readScanPanelWidth())
const autoRules = reactive({ move: false, subtitle: false })
const manualMove = reactive(new Set())
const manualSubtitle = reactive(new Set())
let scanPollTimer = null
let resizeState = null

const groups = computed(() => scan.value.groups || [])
const singleFiles = computed(() => scan.value.single_files || [])
const normalizedSearchQuery = computed(() => searchQuery.value.trim().toLowerCase())
const changedPathSet = computed(() => new Set(scan.value.mode === 'full' ? [] : (scan.value.changed_paths || [])))
const showOnlyNew = computed(() => resultFilter.value === 'new')
const emptyText = computed(() => {
  if (showOnlyNew.value) return '当前没有新增扫描结果。'
  if (searchQuery.value) return '没有匹配的扫描结果。'
  return '当前没有扫描结果。'
})
const filteredGroups = computed(() => {
  const query = normalizedSearchQuery.value
  const onlyNew = showOnlyNew.value
  if (!query && !onlyNew) return groups.value
  return groups.value
    .map((group) => {
      let files = group.files || []
      if (onlyNew) {
        files = files.filter((file) => isNewFile(file))
      }
      if (query) {
        files = groupTextMatches(group, query)
          ? files
          : files.filter((file) => fileTextMatches(file, query))
      }
      return files.length ? { ...group, files } : null
    })
    .filter(Boolean)
})
const filteredSingleFiles = computed(() => {
  const query = normalizedSearchQuery.value
  return singleFiles.value.filter((file) => {
    if (showOnlyNew.value && !isNewFile(file)) return false
    return !query || fileTextMatches(file, query)
  })
})
const visibleRows = computed(() => [
  ...filteredGroups.value.flatMap((group) => group.files || []),
  ...filteredSingleFiles.value
])
const percent = computed(() => Math.round(Number(scan.value.progress || 0) * 100))
const scanModeLabel = computed(() => scan.value.mode === 'full' ? '全量' : '增量')
const scanStatusLabel = computed(() => formatStatus(scan.value.status))
const scanRunning = computed(() => running.value || scan.value.status === 'running')
const selectableDirCount = computed(() => (scan.value.selectable_scan_dirs || []).length)
const savedSelectedPaths = computed(() => scan.value.selected_scan_dirs || [])
const savedDirCount = computed(() => savedSelectedPaths.value.length)
const scanDirSelectionDirty = computed(() => !samePathSelection(selectedPaths.value, savedSelectedPaths.value))
const scanRecoveryMessage = computed(() => {
  if (scan.value.scan_stale) {
    return '扫描长时间没有进度，可能卡在文件系统读取。可以终止本次扫描后重新选择目录。'
  }
  if (scan.value.status === 'interrupted') {
    return scan.value.error || '上次扫描异常中断，已保留旧结果，可以重新扫描。'
  }
  if (scan.value.status === 'cancelled') {
    return scan.value.error || '扫描已终止，可以重新选择目录扫描。'
  }
  return ''
})
const scanRecoveryTone = computed(() => scan.value.scan_stale ? 'error' : 'info')
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
  { label: '扫描进度', value: `${percent.value}%`, detail: `${scanStatusLabel.value} · ${scanModeLabel.value} · ${scan.value.processed_files || 0} / ${scan.value.scan_total_files || 0}`, progress: true },
  { label: '重复组', value: scan.value.duplicate_groups || 0, detail: `${scan.value.total_files || 0} 文件 · 复用 ${scan.value.reused_files || 0}` },
  { label: '重复文件', value: scan.value.duplicate_files || 0, detail: `${scan.value.single_files?.length || 0} 个单文件` },
  { label: '待处理', value: selectedActionCount.value, detail: `${moveSelection.value.length} 移动 / ${subtitleSelection.value.length} 字幕 · 变更 ${scan.value.changed_files || 0}` }
])

onMounted(loadScan)
onUnmounted(() => {
  clearScanPoll()
  stopResize()
})

async function loadScan() {
  clearScanPoll()
  loading.value = true
  errorMessage.value = ''
  try {
    scan.value = await api('/api/scan')
    const available = new Set(scan.value.selectable_scan_dirs || [])
    const savedPaths = (scan.value.selected_scan_dirs || []).filter((path) => available.has(path))
    const defaultPaths = savedPaths.length ? savedPaths : (scan.value.selectable_scan_dirs || [])
    if (!selectedPathsTouched.value) {
      selectedPaths.value = [...defaultPaths]
    } else {
      selectedPaths.value = selectedPaths.value.filter((path) => available.has(path))
    }
  } catch (error) {
    errorMessage.value = error.message || '读取扫描状态失败'
  } finally {
    loading.value = false
    scheduleScanPoll()
  }
}

async function runScan(mode = 'incremental') {
  running.value = true
  message.value = ''
  errorMessage.value = ''
  try {
    const form = new FormData()
    form.append('mode', mode)
    for (const path of selectedPaths.value) form.append('paths', path)
    const payload = await postFormData('/api/scan/run', form)
    message.value = `${payload.mode === 'full' ? '全量扫描' : '增量扫描'}已启动：${payload.scan_dirs?.length || 0} 个目录`
    selectedPathsTouched.value = false
    await loadScan()
  } catch (error) {
    errorMessage.value = error.message || '启动扫描失败'
  } finally {
    running.value = false
  }
}

async function saveSelectedPathDefaults() {
  savingSelection.value = true
  message.value = ''
  errorMessage.value = ''
  try {
    const form = new FormData()
    for (const path of selectedPaths.value) form.append('paths', path)
    const payload = await postFormData('/api/scan/selection', form)
    const selected = payload.selected_scan_dirs || []
    scan.value = {
      ...scan.value,
      selected_scan_dirs: selected
    }
    selectedPaths.value = [...selected]
    selectedPathsTouched.value = false
    message.value = `已保存默认扫描范围：${selected.length} 个目录`
  } catch (error) {
    errorMessage.value = error.message || '保存默认扫描范围失败'
  } finally {
    savingSelection.value = false
  }
}

async function cancelScanState() {
  cancellingScan.value = true
  message.value = ''
  errorMessage.value = ''
  try {
    const payload = await api('/api/scan/cancel', { method: 'POST' })
    message.value = payload.cancelled ? '扫描已终止，可以重新选择目录扫描。' : '当前没有运行中的扫描任务。'
    await loadScan()
  } catch (error) {
    errorMessage.value = error.message || '终止扫描失败'
  } finally {
    cancellingScan.value = false
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
  if (!group) return false
  const stats = groupStats(group)
  return (stats.has4k || stats.hasSubtitle || stats.hasUncensored) && isLowPriority(row)
}

function matchesSubtitleStrategy(row) {
  return !row.ignored && row.subtitle_kind === 'none' && !row.uncensored
}

function matchedMoveRows() {
  return filteredGroups.value.flatMap((group) => {
    const sourceGroup = groups.value.find((item) => item.key === group.key) || group
    return (group.files || []).filter((row) => matchesMoveStrategy(sourceGroup, row))
  })
}

function matchedSubtitleRows() {
  return visibleRows.value.filter((row) => matchesSubtitleStrategy(row))
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

function groupTextMatches(group, query) {
  return [
    group?.title,
    group?.year,
    group?.source,
    group?.key
  ].some((value) => String(value || '').toLowerCase().includes(query))
}

function fileTextMatches(file, query) {
  return [
    file?.name,
    file?.path,
    file?.title,
    file?.year,
    file?.catalog_number,
    file?.resolution,
    file?.subtitle_label,
    file?.subtitle_kind,
    file?.source_tag,
    file?.group_key,
    file?.group_source
  ].some((value) => String(value || '').toLowerCase().includes(query))
}

function invertSelectedPaths() {
  selectedPathsTouched.value = true
  const paths = scan.value.selectable_scan_dirs || []
  const selected = new Set(selectedPaths.value)
  selectedPaths.value = paths.filter((path) => !selected.has(path))
}

function markSelectedPathsTouched() {
  selectedPathsTouched.value = true
}

function samePathSelection(left, right) {
  if (!Array.isArray(left) || !Array.isArray(right)) return false
  if (left.length !== right.length) return false
  const rightSet = new Set(right)
  return left.every((path) => rightSet.has(path))
}

function clearScanPoll() {
  if (scanPollTimer) {
    clearTimeout(scanPollTimer)
    scanPollTimer = null
  }
}

function readScanPanelWidth() {
  if (typeof localStorage === 'undefined') return 360
  const value = Number(localStorage.getItem('duplicatesScanPanelWidth') || 360)
  return clampScanPanelWidth(value)
}

function clampScanPanelWidth(value) {
  const viewport = typeof window === 'undefined' ? 1440 : window.innerWidth
  const max = Math.max(320, Math.min(620, Math.round(viewport * 0.42)))
  return Math.min(max, Math.max(260, Math.round(Number(value) || 360)))
}

function startResize(event) {
  if (typeof window === 'undefined') return
  event.preventDefault()
  resizeState = {
    startX: event.clientX,
    startWidth: scanPanelWidth.value
  }
  event.currentTarget?.setPointerCapture?.(event.pointerId)
  document.body.classList.add('is-resizing-scan-panel')
  window.addEventListener('pointermove', resizePanel)
  window.addEventListener('pointerup', stopResize)
  window.addEventListener('pointercancel', stopResize)
}

function resizePanel(event) {
  if (!resizeState) return
  scanPanelWidth.value = clampScanPanelWidth(resizeState.startWidth + event.clientX - resizeState.startX)
}

function stopResize() {
  if (!resizeState) return
  resizeState = null
  document.body.classList.remove('is-resizing-scan-panel')
  window.removeEventListener('pointermove', resizePanel)
  window.removeEventListener('pointerup', stopResize)
  window.removeEventListener('pointercancel', stopResize)
  if (typeof localStorage !== 'undefined') {
    localStorage.setItem('duplicatesScanPanelWidth', String(scanPanelWidth.value))
  }
}

function scheduleScanPoll() {
  if (scan.value.status !== 'running') return
  scanPollTimer = setTimeout(() => {
    scanPollTimer = null
    loadScan()
  }, 2000)
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

function isNewFile(file) {
  return Boolean(file?.path && changedPathSet.value.has(file.path))
}

function fileRowClass(group, file) {
  const label = hitLabel(group, file)
  return {
    'move-hit': label === '移动',
    'subtitle-hit': label === '字幕',
    'soft-hit': label === '可移动' || label === '可字幕',
    'new-file': isNewFile(file),
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
    failed: '失败',
    interrupted: '已中断',
    cancelled: '已终止'
  }
  return labels[status] || status || '待扫描'
}
</script>

<style scoped>
.duplicates-view {
  display: grid;
  gap: 12px;
}

.hero-card,
.scan-card,
.groups-card {
  min-width: 0;
}

.hero-card {
  display: grid;
  gap: 18px;
  border-radius: 16px;
}

.scan-recovery-notice {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.scan-recovery-notice span {
  min-width: 0;
}

.hero-card :deep(.mm-page-head) {
  align-items: flex-start;
  margin: 0;
}

.hero-card :deep(.mm-page-description) {
  margin-top: 6px;
}

.status-metrics {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
}

.status-metrics article {
  display: grid;
  min-height: 88px;
  align-content: start;
  gap: 4px;
  padding: 14px 16px 12px;
  border: 1px solid var(--mm-border);
  border-radius: 8px;
  background: var(--mm-surface);
}

.status-metrics article.progress-metric {
  grid-template-rows: auto auto auto 8px;
}

.status-metrics span,
.status-metrics em,
.panel-head p,
.groups-copy p,
.group-title span,
.file-main span,
.file-meta,
.selection-summary {
  color: var(--mm-muted);
}

.status-metrics span {
  font-size: 13px;
  font-weight: 550;
}

.status-metrics strong {
  overflow: hidden;
  font-size: 28px;
  font-weight: 650;
  line-height: 1.05;
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
  width: 100%;
  height: 8px;
  overflow: hidden;
  align-self: end;
  margin-top: 2px;
  border-radius: 999px;
  background: #e9e9e9;
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
  grid-template-columns: minmax(260px, var(--scan-panel-width, 360px)) 10px minmax(0, 1fr);
  gap: 6px;
  align-items: stretch;
}

.scan-card {
  display: grid;
  align-content: start;
  min-height: 640px;
  border-radius: 14px;
}

.panel-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.scan-card .panel-head {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
}

.scan-tools {
  display: flex;
  align-items: center;
  gap: 8px;
  white-space: nowrap;
}

.scan-default-note {
  margin: 10px 0 14px;
  color: var(--mm-muted);
  font-size: 12px;
  line-height: 1.4;
}

.scan-default-note.dirty {
  color: var(--mm-primary);
}

.panel-head h2,
.panel-head p,
.groups-copy h2,
.groups-copy p,
.group-title strong,
.group-title span {
  margin: 0;
}

.panel-head h2,
.groups-copy h2 {
  font-size: 18px;
  font-weight: 650;
}

.panel-head p,
.groups-copy p {
  margin-top: 6px;
  font-size: 13px;
  line-height: 1.4;
}

.dir-list,
.group-list,
.file-list {
  display: grid;
  gap: 8px;
}

.dir-list {
  max-height: 560px;
  margin-top: 14px;
  overflow: auto;
  padding-right: 2px;
}

.dir-row {
  display: grid;
  grid-template-columns: 20px minmax(0, 1fr);
  align-items: center;
  min-height: 38px;
  gap: 8px;
  padding: 0 10px;
  border: 1px solid var(--mm-border);
  border-radius: 8px;
  background: var(--mm-control-bg);
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

.resize-handle {
  align-self: stretch;
  width: 10px;
  min-height: 100%;
  padding: 0;
  border: 0;
  border-radius: 999px;
  background: transparent;
  cursor: col-resize;
  touch-action: none;
  position: relative;
  z-index: 1;
}

.resize-handle::before {
  content: "";
  position: absolute;
  inset: 16px 3px;
  border-radius: 999px;
  background: var(--mm-border);
  transition: background .16s ease, box-shadow .16s ease;
}

.resize-handle:hover::before,
.resize-handle:focus-visible::before,
:global(body.is-resizing-scan-panel) .resize-handle::before {
  background: var(--mm-primary);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--mm-primary) 18%, transparent);
}

:global(body.is-resizing-scan-panel) {
  cursor: col-resize;
  user-select: none;
}

.groups-card {
  display: grid;
  grid-template-rows: auto minmax(0, 1fr);
  min-height: 640px;
  overflow: hidden;
  border-radius: 14px;
}

.groups-toolbar {
  display: grid;
  gap: 12px;
  padding: 20px 20px 14px;
  border-bottom: 1px solid var(--mm-border);
}

.groups-toolbar-main,
.groups-toolbar-sub {
  display: flex;
  min-width: 0;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.groups-toolbar-sub {
  align-items: flex-end;
}

.groups-copy {
  display: grid;
  min-width: 0;
}

.selection-summary {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  align-self: end;
  gap: 8px;
  font-size: 13px;
}

.count-pill,
.hit-badge,
.file-meta span {
  display: inline-flex;
  align-items: center;
  min-height: 24px;
  padding: 0 8px;
  border: 1px solid var(--mm-border);
  border-radius: 999px;
  background: var(--mm-control-bg);
  white-space: nowrap;
}

.count-pill {
  gap: 4px;
  background: var(--mm-surface);
  border-color: transparent;
}

.count-pill strong {
  color: var(--mm-text);
  font-weight: 650;
}

.batch-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: flex-end;
  justify-self: end;
  gap: 8px;
  min-width: 0;
}

.group-tools {
  flex: 0 1 500px;
  display: flex;
  align-items: center;
  justify-content: flex-end;
  min-width: 0;
  gap: 8px;
}

.result-filter {
  display: inline-flex;
  flex: 0 0 auto;
  align-items: center;
  min-height: 38px;
  padding: 3px;
  border: 1px solid var(--mm-border);
  border-radius: 8px;
  background: var(--mm-control-bg);
}

.result-filter button {
  min-width: 48px;
  min-height: 30px;
  padding: 0 10px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--mm-muted);
  font: inherit;
  font-size: 13px;
  font-weight: 650;
  cursor: pointer;
}

.result-filter button.active {
  background: var(--mm-card-bg);
  color: var(--mm-text);
  box-shadow: 0 1px 2px color-mix(in srgb, #000 12%, transparent);
}

.result-filter button:disabled {
  cursor: not-allowed;
  opacity: .42;
}

.group-search {
  flex: 1 1 280px;
  min-width: 260px;
  max-width: 320px;
}

.group-search input {
  width: 100%;
  min-height: 38px;
  padding: 0 12px;
  border: 1px solid var(--mm-border);
  border-radius: 8px;
  background: var(--mm-control-bg);
  color: var(--mm-text);
  font: inherit;
  outline: none;
}

.group-search input:focus {
  border-color: var(--mm-primary);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--mm-primary) 18%, transparent);
}

.group-search input::placeholder {
  color: var(--mm-muted);
}

.group-list {
  max-height: 640px;
  overflow: auto;
  padding: 12px 20px 20px;
}

.group-row {
  display: grid;
  gap: 10px;
  padding: 14px;
  border: 1px solid var(--mm-border);
  border-radius: 8px;
  background: var(--mm-card-bg);
}

.group-title {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.group-title div {
  display: grid;
  min-width: 0;
  gap: 4px;
}

.group-title strong {
  overflow: hidden;
  font-size: 15px;
  font-weight: 650;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.file-row {
  display: grid;
  grid-template-columns: 34px 34px minmax(0, 1fr) minmax(240px, auto) auto;
  align-items: center;
  gap: 8px;
  min-height: 42px;
  padding: 7px 10px;
  border: 1px solid var(--mm-border);
  border-radius: 8px;
  background: var(--mm-surface);
}

.file-row.move-hit {
  border-color: var(--mm-danger-border);
  background: var(--mm-notice-bg);
}

.file-row.subtitle-hit {
  border-color: var(--mm-success-border);
  background: var(--mm-success-soft);
}

.file-row.soft-hit {
  border-color: var(--mm-warning-border);
  background: var(--mm-warning-soft);
}

.file-row.new-file:not(.move-hit):not(.subtitle-hit):not(.soft-hit) {
  border-color: color-mix(in srgb, var(--mm-primary) 38%, var(--mm-border));
  background: color-mix(in srgb, var(--mm-primary) 8%, var(--mm-surface));
}

.file-row.ignored {
  opacity: .62;
}

.file-check {
  display: grid;
  place-items: center;
  min-width: 30px;
  color: var(--mm-muted);
  font-size: 11px;
  font-weight: 650;
  line-height: 1.05;
}

.file-check input {
  width: 16px;
  height: 16px;
  margin: 0 0 2px;
}

.file-main {
  display: grid;
  grid-template-columns: minmax(160px, .7fr) minmax(180px, 1fr);
  align-items: center;
  min-width: 0;
  gap: 12px;
}

.file-main strong,
.file-main span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.file-main strong {
  display: inline-flex;
  min-width: 0;
  align-items: center;
  gap: 8px;
  font-weight: 600;
}

.file-main strong > span {
  min-width: 0;
}

.new-badge {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  min-height: 20px;
  padding: 0 7px;
  border: 1px solid color-mix(in srgb, var(--mm-primary) 36%, var(--mm-border));
  border-radius: 999px;
  background: color-mix(in srgb, var(--mm-primary) 14%, var(--mm-card-bg));
  color: var(--mm-primary);
  font-size: 11px;
  font-style: normal;
  font-weight: 700;
  line-height: 1;
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

.hit-badge {
  border-color: var(--mm-warning-border);
  background: color-mix(in srgb, var(--mm-warning) 18%, var(--mm-card-bg));
  color: var(--mm-warning);
  font-size: 12px;
  font-style: normal;
  font-weight: 650;
}

.file-row.move-hit .hit-badge {
  border-color: var(--mm-danger-border);
  background: color-mix(in srgb, var(--mm-danger) 18%, var(--mm-card-bg));
  color: var(--mm-danger);
}

.file-row.subtitle-hit .hit-badge {
  border-color: var(--mm-success-border);
  background: color-mix(in srgb, var(--mm-success) 18%, var(--mm-card-bg));
  color: var(--mm-success);
}

.empty-state,
.compact-empty {
  padding: 24px;
  border: 1px dashed var(--mm-border);
  border-radius: 8px;
  color: var(--mm-muted);
  text-align: center;
}

.compact-empty {
  margin-top: 14px;
}

.groups-card > .empty-state {
  align-self: start;
  margin: 12px 20px 20px;
}

@media (max-width: 1180px) {
  .status-metrics {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .workbench-grid {
    grid-template-columns: 1fr;
    gap: 12px;
  }

  .resize-handle {
    display: none;
  }

  .scan-card,
  .groups-card {
    min-height: auto;
  }

  .dir-list,
  .group-list {
    max-height: none;
  }

  .groups-toolbar {
    display: grid;
  }

  .groups-toolbar-main,
  .groups-toolbar-sub {
    display: grid;
    grid-template-columns: 1fr;
    justify-content: stretch;
  }

  .batch-actions {
    justify-content: flex-start;
  }

  .group-search {
    justify-self: stretch;
    min-width: 0;
    max-width: none;
    width: 100%;
  }

  .group-tools {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    justify-content: stretch;
    width: 100%;
  }

  .file-row {
    grid-template-columns: 34px 34px minmax(0, 1fr) auto;
  }

  .file-main {
    grid-template-columns: 1fr;
    gap: 2px;
  }

  .file-meta {
    grid-column: 3 / -1;
    justify-content: flex-start;
  }
}

@media (max-width: 760px) {
  .hero-card :deep(.mm-page-head),
  .status-metrics,
  .panel-head,
  .group-title,
  .selection-summary,
  .batch-actions {
    display: grid;
    grid-template-columns: 1fr;
    justify-content: stretch;
  }

  .group-tools {
    grid-template-columns: 1fr;
  }

  .result-filter {
    justify-content: stretch;
    width: 100%;
  }

  .result-filter button {
    flex: 1;
  }

  .status-metrics {
    grid-template-columns: 1fr;
  }

  .file-row {
    grid-template-columns: 32px 32px minmax(0, 1fr);
  }

  .hit-badge {
    grid-column: 3;
    justify-self: start;
  }
}
</style>
