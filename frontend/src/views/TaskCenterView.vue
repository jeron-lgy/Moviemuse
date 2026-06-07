<template>
  <CompareView v-if="isCompareView" />
  <section v-else class="task-center">
    <PageHeader kicker="任务中心" title="任务中心" description="集中查看字幕生成、翻译、转码、失败重试和历史记录。">
      <template #actions>
        <BaseButton type="button" :disabled="loading" @click="refreshAll">刷新</BaseButton>
        <BaseButton as="a" href="/docs" target="_blank" rel="noreferrer">API 文档</BaseButton>
      </template>
    </PageHeader>

    <section class="service-grid">
      <BaseCard as="button" class="service-card" type="button" @click="computeDialog = true">
        <span>算力端</span>
        <strong>{{ backendOnline ? '在线' : '离线' }}</strong>
        <em :class="{ on: backendOnline }">{{ connection.subtitle_backend_url || '本机模式 / 未配置地址' }}</em>
        <i :class="{ on: backendOnline }"></i>
      </BaseCard>
      <BaseCard as="button" class="service-card" type="button" @click="translateDialog = true">
        <span>翻译后端</span>
        <strong>{{ activeProvider?.name || '未配置' }}</strong>
        <em :class="{ on: translationReady }">{{ translationReady ? '可用' : '待配置' }}</em>
        <i :class="{ on: translationReady }"></i>
      </BaseCard>
      <BaseCard as="button" class="service-card" type="button" @click="transcodeDialog = true">
        <span>转码设置</span>
        <strong>{{ transcodeSummary }}</strong>
        <em :class="{ on: postprocessSettings.auto_transcode_enabled }">
          {{ postprocessSettings.auto_transcode_enabled ? '自动转码已开启' : '自动转码未开启' }}
        </em>
        <i :class="{ on: postprocessSettings.auto_transcode_enabled }"></i>
      </BaseCard>
    </section>

    <NoticeBanner v-if="notice" >{{ notice }}</NoticeBanner>
    <NoticeBanner v-if="errorMessage" tone="error">{{ errorMessage }}</NoticeBanner>

    <BaseCard class="task-panel" >
      <div class="panel-head">
        <div>
          <h2>任务管理</h2>
          <p>每 4 秒自动刷新；选择状态查看队列，勾选任务后可批量重试。</p>
        </div>
        <div class="metric-strip">
          <div><span>当前队列</span><strong>{{ queueCount }}</strong><em>{{ runningCount }} 运行 / {{ waitingCount }} 等待</em></div>
          <div><span>今日完成</span><strong>{{ todayCompleted }}</strong><em>{{ failedCount }} 个失败待处理</em></div>
          <div><span>转码任务</span><strong>{{ postprocessCount }}</strong><em>{{ transcodeRunningCount }} 转码中 / {{ subtitleProcessingCount }} 生成字幕中</em></div>
        </div>
      </div>

      <div class="toolbar">
        <div class="segmented">
          <button type="button" :class="{ active: taskTab === 'current' }" @click="taskTab = 'current'">当前任务</button>
          <button type="button" :class="{ active: taskTab === 'history' }" @click="taskTab = 'history'">历史任务</button>
        </div>
        <div class="bulk-actions">
          <span v-if="selectedJobs.length">已选择 {{ selectedJobs.length }} 个任务</span>
          <BaseButton  type="button" @click="toggleSelectVisible">{{ allVisibleSelected ? '取消本页全选' : '全选本页' }}</BaseButton>
          <BaseButton variant="primary"  type="button" :disabled="!selectedJobs.length || retryingSelected" @click="retrySelected">
            {{ retryingSelected ? '重试中' : '批量重试' }}
          </BaseButton>
        </div>
      </div>

      <div v-if="taskTab === 'current'" class="state-tabs">
        <button v-for="state in statusTabs" :key="state.key" type="button" :class="{ active: taskStatusTab === state.key }" @click="taskStatusTab = state.key">
          <span :class="['state-dot', state.key]"></span>{{ state.label }} <em>{{ state.count }}</em>
        </button>
      </div>

      <TaskTable
        :jobs="visiblePagedJobs"
        :selected-ids="selectedIds"
        :retrying="retryingJob"
        @toggle="toggleJob"
        @retry="retryJob"
        @cancel="cancelJob"
      />

      <div v-if="!visibleJobs.length" class="empty">这个状态暂时没有任务。</div>
      <div v-if="pageCount > 1" class="pagination">
        <BaseButton  type="button" :disabled="page <= 1" @click="page -= 1">上一页</BaseButton>
        <span>{{ page }} / {{ pageCount }}</span>
        <BaseButton  type="button" :disabled="page >= pageCount" @click="page += 1">下一页</BaseButton>
      </div>
    </BaseCard>

    <TaskDialog v-if="computeDialog" title="Windows 算力端" @close="computeDialog = false">
      <div class="form-grid">
        <FormField label="启用 Windows 算力端">
          <input v-model="computeEnabled" type="checkbox">
        </FormField>
        <FormField label="地址">
          <input v-model.trim="connection.subtitle_backend_url" placeholder="http://WINDOWS-IP:18181">
        </FormField>
        <FormField label="API Token">
          <input v-model.trim="connection.subtitle_backend_token">
        </FormField>
        <FormField label="Whisper 模型">
          <input v-model.trim="settings.whisper_model">
        </FormField>
        <FormField label="设备">
          <select v-model="settings.whisper_device"><option>cuda</option><option>cpu</option></select>
        </FormField>
        <FormField label="计算类型">
          <select v-model="settings.whisper_compute_type"><option>float16</option><option>int8_float16</option><option>int8</option><option>float32</option></select>
        </FormField>
        <FormField label="并发数">
          <input v-model.number="settings.subtitle_max_workers" type="number" min="1" max="4">
        </FormField>
        <FormField label="模型目录">
          <input v-model.trim="settings.whisper_model_dir">
        </FormField>
        <FormField label="路径映射" wide>
          <textarea v-model="settings.subtitle_path_map" rows="3"></textarea>
        </FormField>
      </div>
      <div class="hardware-grid">
        <div><span>CPU</span><strong>{{ backendStatus.hardware?.cpu || '未连接' }}</strong></div>
        <div><span>内存</span><strong>{{ memoryLabel }}</strong></div>
        <div><span>显卡</span><strong>{{ gpuLabel }}</strong></div>
      </div>
      <template #actions>
        <BaseButton  type="button" @click="testBackend">测试联通</BaseButton>
        <BaseButton variant="primary"  type="button" :disabled="savingCompute" @click="saveComputeAll">{{ savingCompute ? '保存中' : '保存设置' }}</BaseButton>
      </template>
    </TaskDialog>

    <TaskDialog v-if="translateDialog" title="翻译后端" @close="translateDialog = false">
      <div class="provider-grid">
        <button v-for="provider in providerCards" :key="provider.value" type="button" :class="{ active: settings.default_translate_backend === provider.value }" @click="settings.default_translate_backend = provider.value">
          <strong>{{ provider.name }}</strong>
          <span>{{ provider.desc }}</span>
        </button>
      </div>
      <div class="form-grid">
        <FormField v-for="field in activeProviderFields" :key="field.key" :label="field.label" :hint="field.hint">
          <input v-model.trim="settings[field.key]" :placeholder="field.placeholder" :type="field.secret ? 'password' : 'text'">
        </FormField>
        <template v-if="settings.default_translate_backend === 'deepseek'">
          <FormField label="翻译风格">
          <select v-model="settings.openai_translation_style"><option value="faithful">忠实直译</option><option value="adult_natural">成人自然</option><option value="seductive">挑逗润色</option></select>
        </FormField>
          <FormField label="语气强度">
          <select v-model="settings.openai_style_intensity"><option value="restrained">克制</option><option value="medium">中等</option><option value="strong">明显</option></select>
        </FormField>
          <FormField label="上下文参考">
          <input v-model.number="settings.openai_context_lines" type="number" min="0" max="6">
        </FormField>
        </template>
      </div>
      <template #actions>
        <BaseButton  type="button" @click="testTranslate(settings.default_translate_backend)">测试</BaseButton>
        <BaseButton variant="primary"  type="button" :disabled="savingSettings" @click="saveSettings()">{{ savingSettings ? '保存中' : '保存设置' }}</BaseButton>
      </template>
    </TaskDialog>

    <TaskDialog v-if="transcodeDialog" title="转码设置" @close="transcodeDialog = false">
      <div class="form-grid">
        <FormField label="启用自动转码">
          <input v-model="postprocessSettings.auto_transcode_enabled" type="checkbox">
        </FormField>
        <FormField label="启用自动字幕">
          <input v-model="postprocessSettings.auto_subtitle_enabled" type="checkbox">
        </FormField>
        <FormField label="算力端上线后自动执行队列">
          <input v-model="postprocessSettings.worker_auto_run" type="checkbox">
        </FormField>
        <FormField label="目标编码">
          <select v-model="postprocessSettings.target_codec"><option value="av1">AV1 · av1_nvenc</option><option value="h265">H.265</option></select>
        </FormField>
        <FormField label="Preset">
          <input v-model.trim="postprocessSettings.preset">
        </FormField>
        <FormField label="CQ / CRF">
          <input v-model.number="postprocessSettings.crf" type="number" min="12" max="51">
        </FormField>
        <FormField label="最大并发">
          <input v-model.number="postprocessSettings.max_concurrency" type="number" min="1" max="8">
        </FormField>
      </div>
      <code class="ffmpeg-preview">{{ ffmpegPreview }}</code>
      <div class="qb-option-head">
        <p>读取 qB 分类和标签后勾选，避免误接管其它下载。</p>
        <BaseButton  type="button" :disabled="loadingQbOptions" @click="loadQbOptions">{{ loadingQbOptions ? '读取中' : '读取 qB' }}</BaseButton>
      </div>
      <div class="chip-columns">
        <div>
          <h3>下载分类</h3>
          <label v-for="item in mergedQbCategories" :key="item" class="option-chip">
            <input v-model="postprocessSettings.allowed_categories" type="checkbox" :value="item">{{ item }}
          </label>
        </div>
        <div>
          <h3>种子标签</h3>
          <label v-for="item in mergedQbTags" :key="item" class="option-chip">
            <input v-model="postprocessSettings.required_tags" type="checkbox" :value="item">{{ item }}
          </label>
        </div>
      </div>
      <p v-if="qbOptionState" class="hint">{{ qbOptionState }}</p>
      <template #actions>
        <BaseButton  type="button" @click="runPostprocessQueue">立即执行队列</BaseButton>
        <BaseButton variant="primary"  type="button" :disabled="savingTranscode" @click="saveTranscodeSettings">{{ savingTranscode ? '保存中' : '保存设置' }}</BaseButton>
      </template>
    </TaskDialog>
  </section>
</template>

<script setup>
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import CompareView from '../components/CompareView.vue'
import TaskDialog from '../components/TaskDialog.vue'
import TaskTable from '../components/TaskTable.vue'
import { api, postJson } from '../lib/api'

const isCompareView = window.location.pathname === '/subtitles/compare'
const PAGE_SIZE = 20
const ACTIVE_POLL_MS = 4000
const IDLE_POLL_MS = 15000
const HIDDEN_POLL_MS = 30000
const loading = ref(false)
const notice = ref('')
const errorMessage = ref('')
const taskTab = ref('current')
const taskStatusTab = ref('running')
const page = ref(1)
const jobs = ref([])
const postprocessTasks = ref([])
const backendStatus = ref({})
const computeDialog = ref(false)
const translateDialog = ref(false)
const transcodeDialog = ref(false)
const computeEnabled = ref(false)
const savingCompute = ref(false)
const savingSettings = ref(false)
const savingTranscode = ref(false)
const loadingQbOptions = ref(false)
const qbOptionState = ref('')
const retryingSelected = ref(false)
const retryingJob = reactive({})
const selectedIds = reactive(new Set())
let refreshTimer = 0

const connection = reactive({ subtitle_backend_url: '', subtitle_backend_token: '' })
const settings = reactive({
  whisper_model: 'large-v3',
  whisper_model_dir: '',
  whisper_device: 'cuda',
  whisper_compute_type: 'float16',
  subtitle_max_workers: 1,
  subtitle_output_dir: '',
  subtitle_path_map: '',
  subtitle_api_token: '',
  default_translate_backend: 'google',
  google_translate_url: 'https://translate.google.com/translate_a/single',
  deepl_api_url: 'https://api-free.deepl.com/v2/translate',
  deepl_api_key: '',
  openai_base_url: 'https://api.deepseek.com',
  openai_api_key: '',
  openai_model: 'deepseek-chat',
  openai_batch_size: 12,
  openai_max_concurrency: 2,
  openai_translation_style: 'adult_natural',
  openai_style_intensity: 'medium',
  openai_context_lines: 2,
  openai_glossary: '',
  ollama_url: '',
  ollama_model: 'qwen2.5:7b'
})
const postprocessSettings = reactive({
  auto_transcode_enabled: false,
  auto_subtitle_enabled: false,
  worker_auto_run: false,
  target_codec: 'av1',
  crf: 36,
  preset: 'p1',
  max_concurrency: 1,
  allowed_categories: [],
  required_tags: []
})
const qbOptions = reactive({ categories: [], tags: [] })

const providerCards = [
  { name: 'Google 免费翻译', value: 'google', desc: '默认优先 · 无需 API Key' },
  { name: 'DeepL API', value: 'deepl', desc: 'api-free.deepl.com' },
  { name: 'DeepSeek API', value: 'deepseek', desc: 'Base URL · API Key · 模型' },
  { name: '本地 Ollama', value: 'ollama', desc: 'OLLAMA_URL · 本地模型' }
]
const providerFields = {
  google: [{ key: 'google_translate_url', label: 'Google 免费接口', placeholder: 'https://translate.google.com/translate_a/single', hint: '默认可用，不需要 Key。' }],
  deepl: [{ key: 'deepl_api_key', label: 'DeepL API Key', placeholder: 'DeepL auth key', secret: true }, { key: 'deepl_api_url', label: 'DeepL API URL', placeholder: 'https://api-free.deepl.com/v2/translate' }],
  deepseek: [{ key: 'openai_base_url', label: 'DeepSeek API Base URL', placeholder: 'https://api.deepseek.com' }, { key: 'openai_api_key', label: 'DeepSeek API Key', placeholder: 'sk-...', secret: true }, { key: 'openai_model', label: 'DeepSeek 模型', placeholder: 'deepseek-chat' }],
  ollama: [{ key: 'ollama_url', label: 'Ollama URL', placeholder: 'http://127.0.0.1:11434' }, { key: 'ollama_model', label: 'Ollama 模型', placeholder: 'qwen2.5:7b' }]
}

const activeProvider = computed(() => providerCards.find((item) => item.value === settings.default_translate_backend))
const activeProviderFields = computed(() => providerFields[settings.default_translate_backend] || providerFields.google)
const backendOnline = computed(() => !!backendStatus.value.online)
const translationReady = computed(() => {
  if (settings.default_translate_backend === 'google') return true
  if (settings.default_translate_backend === 'deepl') return !!settings.deepl_api_key
  if (settings.default_translate_backend === 'deepseek') return !!settings.openai_base_url && !!settings.openai_api_key
  if (settings.default_translate_backend === 'ollama') return !!settings.ollama_url
  return false
})

const adaptedSubtitleJobs = computed(() => jobs.value.map(adaptSubtitleJob))
const adaptedPostprocessJobs = computed(() => postprocessTasks.value.map(adaptPostprocessJob))
const adaptedJobs = computed(() => [...adaptedPostprocessJobs.value, ...adaptedSubtitleJobs.value].sort((a, b) => Number(b.updatedAt || b.createdAt || 0) - Number(a.updatedAt || a.createdAt || 0)))
const runningJobs = computed(() => adaptedJobs.value.filter((job) => ['running', 'translating'].includes(job.statusKey)))
const waitingJobs = computed(() => adaptedJobs.value.filter((job) => job.statusKey === 'queued'))
const failedJobs = computed(() => adaptedJobs.value.filter((job) => job.statusKey === 'failed'))
const completedJobs = computed(() => adaptedJobs.value.filter((job) => job.statusKey === 'completed'))
const activeJobs = computed(() => adaptedJobs.value.filter((job) => ['queued', 'running', 'translating'].includes(job.statusKey)))
const historyJobs = computed(() => adaptedJobs.value.filter((job) => ['completed', 'failed'].includes(job.statusKey)))
const runningCount = computed(() => runningJobs.value.length)
const waitingCount = computed(() => waitingJobs.value.length)
const failedCount = computed(() => failedJobs.value.length)
const queueCount = computed(() => activeJobs.value.length)
const postprocessCount = computed(() => adaptedPostprocessJobs.value.length)
const transcodeRunningCount = computed(() => adaptedPostprocessJobs.value.filter((job) => job.phase === 'transcode' && job.statusKey === 'running').length)
const subtitleProcessingCount = computed(() => adaptedPostprocessJobs.value.filter((job) => job.phase === 'subtitle' && ['running', 'translating'].includes(job.statusKey)).length)
const todayCompleted = computed(() => {
  const start = new Date()
  start.setHours(0, 0, 0, 0)
  return completedJobs.value.filter((job) => Number(job.finishedAt || job.updatedAt || 0) * 1000 >= start.getTime()).length
})
const statusTabs = computed(() => [
  { key: 'running', label: '运行中', count: runningCount.value, items: runningJobs.value },
  { key: 'waiting', label: '等待中', count: waitingCount.value, items: waitingJobs.value },
  { key: 'failed', label: '失败', count: failedCount.value, items: failedJobs.value },
  { key: 'completed', label: '已完成', count: completedJobs.value.length, items: completedJobs.value }
])
const visibleJobs = computed(() => taskTab.value === 'history' ? historyJobs.value : (statusTabs.value.find((state) => state.key === taskStatusTab.value)?.items || []))
const pageCount = computed(() => Math.max(1, Math.ceil(visibleJobs.value.length / PAGE_SIZE)))
const visiblePagedJobs = computed(() => visibleJobs.value.slice((page.value - 1) * PAGE_SIZE, page.value * PAGE_SIZE))
const selectedJobs = computed(() => adaptedJobs.value.filter((job) => selectedIds.has(job.id)))
const allVisibleSelected = computed(() => visiblePagedJobs.value.length > 0 && visiblePagedJobs.value.every((job) => selectedIds.has(job.id)))
const memoryLabel = computed(() => {
  const memory = backendStatus.value.hardware?.memory
  return memory ? `${memory.label || ''} · ${memory.used_percent || 0}%` : '未连接'
})
const gpuLabel = computed(() => backendStatus.value.hardware?.gpus?.[0]?.label || '未检测')
const transcodeSummary = computed(() => `${String(postprocessSettings.target_codec || 'av1').toUpperCase()} · ${postprocessSettings.preset || 'p1'} · CQ/CRF ${postprocessSettings.crf || 36}`)
const ffmpegPreview = computed(() => {
  const encoder = String(postprocessSettings.target_codec || 'av1') === 'av1' ? 'av1_nvenc' : 'libx265'
  const qualityFlag = encoder.endsWith('_nvenc') ? '-cq' : '-crf'
  return `ffmpeg -hide_banner -nostdin -i "<输入文件>" -c:v ${encoder} -preset ${postprocessSettings.preset || 'p1'} ${qualityFlag} ${postprocessSettings.crf || 36} -c:a copy "<输出文件>" -y`
})
const mergedQbCategories = computed(() => mergeUnique(qbOptions.categories, postprocessSettings.allowed_categories))
const mergedQbTags = computed(() => mergeUnique(qbOptions.tags, postprocessSettings.required_tags))

async function loadConsole() {
  loading.value = true
  errorMessage.value = ''
  try {
    const [consolePayload, postprocessPayload] = await Promise.all([
      api('/api/subtitle/console'),
      api('/api/postprocess/tasks?limit=200')
    ])
    Object.assign(connection, consolePayload.connection || {})
    computeEnabled.value = !!connection.subtitle_backend_url
    Object.assign(settings, consolePayload.compute_settings || {})
    jobs.value = consolePayload.jobs || []
    postprocessTasks.value = postprocessPayload.tasks || []
    Object.assign(postprocessSettings, normalizePostprocessSettings(postprocessPayload.settings || {}))
    backendStatus.value = postprocessPayload.worker_status || consolePayload.backend_status || {}
  } catch (error) {
    errorMessage.value = error.message || '读取任务中心失败'
  } finally {
    loading.value = false
  }
}

async function refreshAll() {
  if (isCompareView) return
  const [subtitlePayload, postprocessPayload] = await Promise.all([
    api('/api/subtitle/jobs?limit=0'),
    api('/api/postprocess/tasks?limit=200')
  ])
  jobs.value = subtitlePayload.jobs || []
  postprocessTasks.value = postprocessPayload.tasks || []
  Object.assign(postprocessSettings, normalizePostprocessSettings(postprocessPayload.settings || {}))
  backendStatus.value = postprocessPayload.worker_status || backendStatus.value
}

async function testBackend() {
  notice.value = '正在测试算力端连接...'
  const body = new FormData()
  body.set('subtitle_backend_url', connection.subtitle_backend_url || '')
  body.set('subtitle_backend_token', connection.subtitle_backend_token || '')
  try {
    const response = await fetch('/api/subtitle/backend/test', { method: 'POST', body })
    const payload = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(payload.detail || '连接失败')
    backendStatus.value = payload
    notice.value = '连接成功，可以保存这个地址。'
  } catch (error) {
    errorMessage.value = error.message || String(error)
    notice.value = ''
  }
}

async function saveConnection() {
  const result = await postJson('/api/subtitle/connection', {
    subtitle_backend_url: computeEnabled.value ? connection.subtitle_backend_url : '',
    subtitle_backend_token: computeEnabled.value ? connection.subtitle_backend_token : ''
  })
  Object.assign(connection, result.connection || {})
  backendStatus.value = result.backend_status || backendStatus.value
}

async function saveSettings(closeDialog = true) {
  savingSettings.value = true
  errorMessage.value = ''
  try {
    const payload = await postJson('/api/subtitle/settings', { ...settings })
    Object.assign(settings, payload.settings || {})
    backendStatus.value = payload.backend_status || backendStatus.value
    notice.value = payload.warning || '翻译后端设置已保存。'
    if (closeDialog) translateDialog.value = false
  } catch (error) {
    errorMessage.value = error.message || '保存翻译设置失败'
  } finally {
    savingSettings.value = false
  }
}

async function saveComputeAll() {
  savingCompute.value = true
  try {
    await saveConnection()
    await saveSettings(false)
    computeDialog.value = false
    await loadConsole()
    notice.value = '算力端设置已保存。'
  } finally {
    savingCompute.value = false
  }
}

async function testTranslate(backend) {
  try {
    const payload = await postJson('/api/subtitle/translate/test', {
      backend,
      text: 'クッションがいっぱいある、かわいい',
      source_language: 'ja',
      target_language: 'zh',
      settings
    })
    notice.value = `翻译测试可用：${payload.translated_text || payload.status || 'ok'}`
  } catch (error) {
    errorMessage.value = `翻译测试失败：${error.message || error}`
  }
}

async function loadQbOptions() {
  loadingQbOptions.value = true
  qbOptionState.value = '读取中...'
  try {
    const payload = await api('/api/integrations/qbittorrent/options')
    qbOptions.categories = payload.categories || []
    qbOptions.tags = payload.tags || []
    qbOptionState.value = payload.status === 'ok' ? '已读取 qB 可选分类和标签。' : (payload.message || '读取失败，已保留当前配置。')
  } catch (error) {
    qbOptionState.value = error.message || String(error)
  } finally {
    loadingQbOptions.value = false
  }
}

async function saveTranscodeSettings() {
  savingTranscode.value = true
  try {
    const payload = await postJson('/api/postprocess/settings', { ...postprocessSettings })
    Object.assign(postprocessSettings, normalizePostprocessSettings(payload.settings || {}))
    transcodeDialog.value = false
    notice.value = '转码设置已保存。'
    await refreshAll()
  } catch (error) {
    errorMessage.value = error.message || '保存转码设置失败'
  } finally {
    savingTranscode.value = false
  }
}

async function runPostprocessQueue() {
  try {
    await postJson('/api/postprocess/queue/run', {})
    notice.value = '后处理队列已触发。'
    await refreshAll()
  } catch (error) {
    errorMessage.value = error.message || '执行队列失败'
  }
}

function toggleJob(id) {
  selectedIds.has(id) ? selectedIds.delete(id) : selectedIds.add(id)
}

function toggleSelectVisible() {
  if (allVisibleSelected.value) visiblePagedJobs.value.forEach((job) => selectedIds.delete(job.id))
  else visiblePagedJobs.value.forEach((job) => selectedIds.add(job.id))
}

async function retryJob(job) {
  retryingJob[job.id] = true
  try {
    if (job.sourceType === 'postprocess') await postJson(`/api/postprocess/tasks/${job.rawId}/retry`, {})
    else await postJson(`/api/subtitle/jobs/${job.fileId}/retry`, {})
    await refreshAll()
  } finally {
    retryingJob[job.id] = false
  }
}

async function retrySelected() {
  const retryable = selectedJobs.value.filter((job) => job.canRetry)
  if (!retryable.length) {
    notice.value = '选中的任务里没有可重试任务。'
    return
  }
  retryingSelected.value = true
  try {
    for (const job of retryable) await retryJob(job)
    notice.value = `已提交 ${retryable.length} 个重试任务。`
  } finally {
    retryingSelected.value = false
  }
}

async function cancelJob(job) {
  if (job.sourceType !== 'postprocess') return
  await postJson(`/api/postprocess/tasks/${job.rawId}/cancel`, {})
  notice.value = '任务已取消。'
  await refreshAll()
}

function adaptSubtitleJob(job) {
  const path = String(job.video_path || '')
  const title = path.replaceAll('\\', '/').split('/').filter(Boolean).pop() || path || '未命名任务'
  const statusKey = String(job.status || 'queued')
  return {
    raw: job,
    id: `subtitle:${job.id}`,
    fileId: job.id,
    sourceType: 'subtitle',
    phase: 'subtitle',
    phaseLabel: statusKey === 'translating' ? '翻译' : '字幕',
    title,
    path,
    statusKey,
    statusLabel: statusLabel(statusKey),
    createdLabel: formatTime(job.created_at),
    createdAt: job.created_at,
    updatedAt: job.updated_at,
    finishedAt: job.finished_at,
    modelLabel: `${job.model || 'large-v3'} / ${job.source_language || 'auto'} => ${job.target_language || 'zh'}`,
    canRetry: ['failed', 'completed'].includes(statusKey),
    canCancel: false,
    resultSrt: job.translated_srt || job.bilingual_srt
  }
}

function adaptPostprocessJob(task) {
  const phase = postprocessPhase(task.status)
  const statusKey = postprocessStatusKey(task.status)
  const avId = task.av_id || task.id || '后处理任务'
  return {
    raw: task,
    id: `postprocess:${task.id}`,
    rawId: task.id,
    sourceType: 'postprocess',
    phase,
    phaseLabel: phase === 'transcode' ? '转码' : phase === 'subtitle' ? '字幕' : '后处理',
    title: phase === 'subtitle' ? `${avId} · 生成字幕` : phase === 'transcode' ? `${avId} · 转码` : avId,
    path: task.input_path || task.output_path || task.error_message || '等待链路写入输入文件',
    statusKey,
    statusLabel: postprocessStatusLabel(task.status, phase, statusKey),
    createdLabel: formatTime(task.created_at),
    createdAt: task.created_at,
    updatedAt: task.updated_at || task.created_at,
    finishedAt: task.finished_at,
    modelLabel: `${task.target_codec || postprocessSettings.target_codec || 'av1'} / ${task.task_type || '后处理'}`,
    canRetry: ['failed', 'ignored', 'conflict', 'expired'].includes(String(task.status || '')),
    canCancel: !['completed', 'ignored'].includes(String(task.status || '')),
    resultSrt: ''
  }
}

function postprocessPhase(status) {
  const value = String(status || '')
  if (['subtitle_processing', 'subtitle_validating', 'transcode_done'].includes(value)) return 'subtitle'
  if (['sent_to_worker', 'transcoding', 'worker_done', 'transcode_validating'].includes(value)) return 'transcode'
  return 'postprocess'
}

function postprocessStatusKey(status) {
  const value = String(status || '')
  if (['waiting_worker', 'ready_to_run', 'created'].includes(value)) return 'queued'
  if (['sent_to_worker', 'transcoding', 'worker_done', 'transcode_validating'].includes(value)) return 'running'
  if (['subtitle_processing', 'subtitle_validating', 'transcode_done'].includes(value)) return 'translating'
  if (value === 'completed') return 'completed'
  if (['failed', 'ignored', 'conflict', 'expired'].includes(value)) return 'failed'
  return 'queued'
}

function postprocessStatusLabel(status, phase, statusKey) {
  if (statusKey === 'running' && phase === 'transcode') return '转码中'
  if (statusKey === 'translating' && phase === 'subtitle') return '生成字幕中'
  if (statusKey === 'queued') return '等待中'
  if (statusKey === 'failed') return '失败'
  if (statusKey === 'completed') return '已完成'
  return status || statusLabel(statusKey)
}

function statusLabel(status) {
  return { queued: '等待中', running: '运行中', translating: '翻译中', failed: '失败', completed: '已完成' }[status] || status
}

function normalizePostprocessSettings(payload = {}) {
  return {
    auto_transcode_enabled: !!payload.auto_transcode_enabled,
    auto_subtitle_enabled: !!payload.auto_subtitle_enabled,
    worker_auto_run: !!payload.worker_auto_run,
    target_codec: payload.target_codec || 'av1',
    crf: Number(payload.crf || 36),
    preset: payload.preset || 'p1',
    max_concurrency: Number(payload.max_concurrency || 1),
    allowed_categories: Array.isArray(payload.allowed_categories) ? payload.allowed_categories : [],
    required_tags: Array.isArray(payload.required_tags) ? payload.required_tags : []
  }
}

function mergeUnique(primary = [], secondary = []) {
  return Array.from(new Set([...(primary || []), ...(secondary || [])].map((item) => String(item || '').trim()).filter(Boolean)))
}

function formatTime(value) {
  if (!value) return '未知时间'
  const date = new Date(Number(value) * 1000)
  return Number.isNaN(date.getTime()) ? '未知时间' : date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

function clearRefreshTimer() {
  if (!refreshTimer) return
  window.clearTimeout(refreshTimer)
  refreshTimer = 0
}

function nextRefreshDelay() {
  if (document.hidden) return HIDDEN_POLL_MS
  return activeJobs.value.length ? ACTIVE_POLL_MS : IDLE_POLL_MS
}

function scheduleRefresh() {
  if (isCompareView) return
  clearRefreshTimer()
  refreshTimer = window.setTimeout(async () => {
    try {
      await refreshAll()
    } catch {
      // Polling stays quiet; explicit refresh/load still surfaces errors.
    } finally {
      scheduleRefresh()
    }
  }, nextRefreshDelay())
}

function handleVisibilityChange() {
  scheduleRefresh()
}

watch([taskTab, taskStatusTab], () => { page.value = 1 })

onMounted(async () => {
  if (isCompareView) return
  await loadConsole()
  document.addEventListener('visibilitychange', handleVisibilityChange)
  scheduleRefresh()
})

onUnmounted(() => {
  clearRefreshTimer()
  document.removeEventListener('visibilitychange', handleVisibilityChange)
})
</script>

<style scoped>
.task-center {
  display: grid;
  gap: 24px;
}

.panel-head,
.toolbar,
.qb-option-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 24px;
}

.eyebrow {
  margin: 0 0 6px;
  color: var(--mm-primary);
  font-size: 13px;
  font-weight: 600;
}

h1,
h2,
h3,
p {
  margin: 0;
}

h1 {
  font-size: 34px;
  font-weight: 650;
  letter-spacing: -0.3px;
}
.panel-head p,
.hint {
  margin-top: 8px;
  color: var(--mm-muted);
  line-height: 1.7;
}

.page-actions,
.bulk-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}

.service-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 16px;
}

.service-card {
  position: relative;
  min-height: 148px;
  padding: 22px;
  border-color: var(--mm-border);
  text-align: left;
  cursor: pointer;
}

.service-card span,
.metric-strip span {
  color: var(--mm-muted);
  font-size: 14px;
}

.service-card strong,
.metric-strip strong {
  display: block;
  margin-top: 12px;
  font-size: 28px;
  font-weight: 650;
}

.service-card em,
.metric-strip em {
  display: block;
  margin-top: 8px;
  color: var(--mm-primary);
  font-style: normal;
  font-weight: 500;
}

.service-card em.on {
  color: #087e74;
}

.service-card i {
  position: absolute;
  top: 28px;
  right: 26px;
  width: 14px;
  height: 14px;
  border-radius: 999px;
  background: var(--mm-primary);
  box-shadow: 0 0 0 10px #fff0f3;
}

.service-card i.on {
  background: #16a34a;
  box-shadow: 0 0 0 10px #e9fbea;
}

.task-panel {
  padding: 24px;
}

.metric-strip {
  display: grid;
  grid-template-columns: repeat(3, minmax(150px, 1fr));
  gap: 12px;
  min-width: min(680px, 100%);
}

.metric-strip div {
  min-height: 112px;
  padding: 16px;
  border: 1px solid var(--mm-border);
  border-radius: 14px;
}

.toolbar {
  align-items: center;
  margin-top: 22px;
  padding-top: 18px;
  border-top: 1px solid var(--mm-border);
}

.segmented,
.state-tabs {
  display: inline-flex;
  gap: 4px;
  padding: 4px;
  border: 1px solid var(--mm-border);
  border-radius: 12px;
  background: var(--mm-surface);
}

.segmented button,
.state-tabs button {
  min-height: 40px;
  padding: 0 16px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--mm-muted);
  font-weight: 600;
}

.segmented button.active,
.state-tabs button.active {
  background: #fff;
  color: var(--mm-primary);
  box-shadow: var(--mm-shadow);
}

.state-tabs {
  margin: 18px 0 14px;
}

.state-tabs em {
  margin-left: 6px;
  font-style: normal;
}

.state-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  margin-right: 8px;
  border-radius: 999px;
  background: var(--mm-muted);
}

.state-dot.running,
.state-dot.failed {
  background: var(--mm-primary);
}

.state-dot.completed {
  background: #16a34a;
}

.empty {
  padding: 40px;
  color: var(--mm-muted);
  text-align: center;
}

.pagination {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
  margin-top: 18px;
}

.form-grid,
.hardware-grid,
.provider-grid,
.chip-columns {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}

.hardware-grid {
  grid-template-columns: repeat(3, minmax(0, 1fr));
}

input,
select,
textarea {
  min-height: 44px;
  width: 100%;
  padding: 0 14px;
  border: 1px solid var(--mm-border);
  border-radius: 8px;
  background: #fff;
  color: var(--mm-text);
}

textarea {
  padding-top: 10px;
}

input[type="checkbox"] {
  width: 22px;
  min-height: 22px;
  padding: 0;
}

.hardware-grid div,
.provider-grid button {
  min-height: 96px;
  padding: 16px;
  border: 1px solid var(--mm-border);
  border-radius: 14px;
  background: #fff;
  text-align: left;
}

.hardware-grid span,
.provider-grid span {
  display: block;
  color: var(--mm-muted);
  font-size: 13px;
}

.hardware-grid strong,
.provider-grid strong {
  display: block;
  margin-top: 8px;
  font-weight: 650;
}

.provider-grid button.active {
  border-color: var(--mm-primary);
  background: #fff5f7;
}

.ffmpeg-preview {
  display: block;
  padding: 14px;
  overflow: auto;
  border-radius: 8px;
  background: #111;
  color: #fff;
  white-space: nowrap;
}

.option-chip {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-height: 34px;
  margin: 8px 8px 0 0;
  padding: 0 12px;
  border: 1px solid var(--mm-border);
  border-radius: 999px;
  background: #fff;
}

@media (max-width: 1100px) {
  .service-grid,
  .form-grid,
  .hardware-grid,
  .provider-grid,
  .chip-columns {
    grid-template-columns: 1fr;
  }

    .panel-head,
  .toolbar,
  .qb-option-head {
    display: grid;
  }
}
</style>
