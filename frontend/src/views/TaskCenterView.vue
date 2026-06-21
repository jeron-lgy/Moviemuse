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

    <BaseCard class="task-panel" padding="none">
      <div class="panel-head">
        <div>
          <h2>任务管理</h2>
          <p>每 4 秒自动刷新；选择状态查看队列，勾选任务后可批量重试。</p>
        </div>
        <div class="bulk-actions">
          <span v-if="selectedJobs.length">已选择 {{ selectedJobs.length }} 个任务</span>
          <BaseButton  type="button" @click="toggleSelectVisible">{{ allVisibleSelected ? '取消本页全选' : '全选本页' }}</BaseButton>
          <BaseButton type="button" :disabled="!runnableSelectedJobs.length || runningSelected" @click="runSelected">
            {{ runningSelected ? '入队中' : '批量运行' }}
          </BaseButton>
          <BaseButton variant="primary"  type="button" :disabled="!selectedJobs.length || retryingSelected" @click="retrySelected">
            {{ retryingSelected ? '重试中' : '批量重试' }}
          </BaseButton>
          <BaseButton variant="danger" type="button" :disabled="!selectedJobs.length || deletingSelected" @click="deleteSelected">
            {{ deletingSelected ? '删除中' : '批量删除' }}
          </BaseButton>
        </div>
      </div>

      <div class="toolbar">
        <div class="segmented">
          <button type="button" :class="{ active: taskTab === 'current' }" @click="taskTab = 'current'">当前任务</button>
          <button type="button" :class="{ active: taskTab === 'history' }" @click="taskTab = 'history'">历史任务</button>
        </div>
        <div v-if="taskTab === 'current'" class="state-tabs">
          <button v-for="state in statusTabs" :key="state.key" type="button" :class="{ active: taskStatusTab === state.key }" @click="taskStatusTab = state.key">
            <span :class="['state-dot', state.key]"></span>{{ state.label }} <em>{{ state.count }}</em>
          </button>
        </div>
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
      <div class="compute-settings">
        <section class="settings-section">
          <BaseSwitch v-model="computeEnabled" label="启用 Windows 算力端" />
          <div class="form-grid compact-grid">
            <FormField label="算力端地址">
              <input v-model.trim="connection.subtitle_backend_url" placeholder="http://WINDOWS-IP:18181">
            </FormField>
            <FormField label="回调地址">
              <input v-model.trim="settings.console_public_url" placeholder="http://unraid-host.local:18188">
            </FormField>
            <FormField label="API Token" wide>
              <SecretInput v-model.trim="connection.subtitle_backend_token" autocomplete="off" placeholder="未设置可留空" />
            </FormField>
          </div>
        </section>

        <section class="settings-section">
          <div class="compute-section-head">
            <strong>Whisper 参数</strong>
          </div>
          <div class="form-grid compact-grid">
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
            <FormField label="模型目录" wide>
              <input v-model.trim="settings.whisper_model_dir">
            </FormField>
          </div>
        </section>

        <section class="settings-section">
          <div class="compute-section-head">
            <strong>路径映射</strong>
          </div>
          <FormField label="映射规则" wide>
            <textarea v-model="settings.subtitle_path_map" rows="3" placeholder="/media=\\NAS\media"></textarea>
          </FormField>
        </section>

        <section class="settings-section hardware-section">
          <div class="compute-section-head">
            <strong>硬件状态</strong>
          </div>
          <div class="hardware-grid">
            <div><span>CPU</span><strong>{{ backendStatus.hardware?.cpu || '未连接' }}</strong></div>
            <div><span>内存</span><strong>{{ memoryLabel }}</strong></div>
            <div><span>显卡</span><strong>{{ gpuLabel }}</strong></div>
          </div>
        </section>
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
          <SecretInput v-if="field.secret" v-model.trim="settings[field.key]" autocomplete="off" :placeholder="field.placeholder" />
          <input v-else v-model.trim="settings[field.key]" :placeholder="field.placeholder">
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
        <BaseButton as="RouterLink" to="/subtitles/compare" type="button" @click="translateDialog = false">翻译效果对比</BaseButton>
        <BaseButton  type="button" @click="testTranslate(settings.default_translate_backend)">测试</BaseButton>
        <BaseButton variant="primary"  type="button" :disabled="savingSettings" @click="saveSettings()">{{ savingSettings ? '保存中' : '保存设置' }}</BaseButton>
      </template>
    </TaskDialog>

    <TaskDialog v-if="transcodeDialog" title="转码设置" @close="transcodeDialog = false">
      <div class="form-grid">
        <FormField label="编码方案">
          <select :value="activeEncodingKey" @change="selectEncodingPreset($event.target.value)">
            <option value="">自定义当前参数</option>
            <option v-for="preset in allEncodingPresets" :key="preset.key" :value="preset.key">
              {{ preset.name }}
            </option>
          </select>
        </FormField>
        <FormField label="编码器">
          <input v-model.trim="postprocessSettings.target_encoder">
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
        <FormField label="下载目录">
          <input v-model.trim="postprocessSettings.download_dir" placeholder="/media/你的下载目录">
        </FormField>
        <FormField label="输出目录">
          <input v-model.trim="postprocessSettings.output_dir" placeholder="/media/你的输出目录">
        </FormField>
      </div>
      <div class="ffmpeg-send-panel">
        <div class="section-head">
          <h3>FFmpeg 设置来源</h3>
          <p>选择保存哪一组 FFmpeg 设置。选中的卡片会高亮，点击保存设置后同步到算力端。</p>
        </div>
        <div class="ffmpeg-mode-cards">
          <button type="button" class="choice-card" :class="{ active: ffmpegMode === 'standard' }" @click="setFfmpegMode('standard')">
            <strong>标准编码设置</strong>
            <span>使用上面选中的编码方案、Preset、CQ/CRF 自动生成命令。</span>
          </button>
          <button type="button" class="choice-card" :class="{ active: ffmpegMode === 'custom' }" @click="setFfmpegMode('custom')">
            <strong>自定义 FFmpeg 模板</strong>
            <span>直接保存一段标准 FFmpeg 命令模板，支持 {input}、{output}、{encoder} 等变量。</span>
          </button>
        </div>
        <textarea
          v-if="ffmpegMode === 'custom'"
          v-model.trim="postprocessSettings.ffmpeg_custom_template"
          class="ffmpeg-template"
          rows="3"
          placeholder='ffmpeg -hide_banner -nostdin -i "{input}" -c:v {encoder} {preset_flag} {preset} {quality_flag} {quality} -c:a copy "{output}" -y'
        ></textarea>
      </div>
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
import { api, deleteJson, postJson } from '../lib/api'

const isCompareView = window.location.pathname === '/subtitles/compare'
const PAGE_SIZE = 20
const ACTIVE_POLL_MS = 4000
const IDLE_POLL_MS = 15000
const HIDDEN_POLL_MS = 30000
const loading = ref(false)
const notice = ref('')
const errorMessage = ref('')
const taskTab = ref('current')
const taskStatusTab = ref('all')
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
const runningSelected = ref(false)
const deletingSelected = ref(false)
const retryingJob = reactive({})
const selectedIds = reactive(new Set())
let refreshTimer = 0
let refreshGeneration = 0

const connection = reactive({ subtitle_backend_url: '', subtitle_backend_token: '' })
const settings = reactive({
  whisper_model: 'large-v3',
  whisper_model_dir: '',
  whisper_device: 'cuda',
  whisper_compute_type: 'float16',
  subtitle_max_workers: 1,
  subtitle_output_dir: '',
  subtitle_path_map: '',
  console_public_url: '',
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
  download_dir: '/media/study3',
  output_dir: '/media/压制',
  target_codec: 'av1',
  target_encoder: 'av1_nvenc',
  crf: 36,
  preset: 'p1',
  preset_flag: '-preset',
  ffmpeg_mode: 'standard',
  ffmpeg_standard_enabled: true,
  ffmpeg_custom_enabled: false,
  ffmpeg_custom_template: '',
  custom_encoding_presets: [],
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
const computeCallbackWarning = computed(() => {
  if (!computeEnabled.value) return ''
  const value = String(settings.console_public_url || '').trim()
  if (!value) return '请填写 Unraid 回调地址，例如 http://unraid-host.local:18188。否则 Windows 算力端转码完成后无法通知控制端。'
  try {
    const parsed = new URL(value)
    const host = parsed.hostname.toLowerCase()
    if (['unraid-ip', 'windows-ip', 'localhost', '127.0.0.1', '0.0.0.0', '[::1]', '::1'].includes(host)) {
      return 'Unraid 回调地址不能使用占位符或本机地址，请改成 Windows 能访问的 Unraid 控制台地址。'
    }
    if (!['http:', 'https:'].includes(parsed.protocol)) {
      return 'Unraid 回调地址需要以 http:// 或 https:// 开头。'
    }
  } catch {
    return 'Unraid 回调地址格式不正确，请填写类似 http://unraid-host.local:18188 的完整地址。'
  }
  return ''
})
const translationReady = computed(() => {
  if (settings.default_translate_backend === 'google') return true
  if (settings.default_translate_backend === 'deepl') return !!settings.deepl_api_key
  if (settings.default_translate_backend === 'deepseek') return !!settings.openai_base_url && !!settings.openai_api_key
  if (settings.default_translate_backend === 'ollama') return !!settings.ollama_url
  return false
})

const adaptedSubtitleJobs = computed(() => jobs.value.map(adaptSubtitleJob))
const transcodeJobLookup = computed(() => {
  const lookup = new Map()
  const items = backendStatus.value?.transcode_jobs?.items || []
  items.forEach((item) => {
    if (item?.id) lookup.set(String(item.id), item)
    if (item?.task_id) lookup.set(String(item.task_id), item)
  })
  return lookup
})
const adaptedPostprocessJobs = computed(() => postprocessTasks.value.map(adaptPostprocessJob))
const adaptedJobs = computed(() => [...adaptedPostprocessJobs.value, ...adaptedSubtitleJobs.value].sort((a, b) => Number(b.updatedAt || b.createdAt || 0) - Number(a.updatedAt || a.createdAt || 0)))
const runningJobs = computed(() => adaptedJobs.value.filter((job) => ['running', 'translating'].includes(job.statusKey)))
const waitingJobs = computed(() => adaptedJobs.value.filter((job) => job.statusKey === 'queued'))
const failedJobs = computed(() => adaptedJobs.value.filter((job) => job.statusKey === 'failed'))
const detachedJobs = computed(() => adaptedJobs.value.filter((job) => job.statusKey === 'detached'))
const completedJobs = computed(() => adaptedJobs.value.filter((job) => job.statusKey === 'completed'))
const activeJobs = computed(() => adaptedJobs.value.filter((job) => ['queued', 'running', 'translating', 'detached'].includes(job.statusKey)))
const historyJobs = computed(() => adaptedJobs.value.filter((job) => ['completed', 'failed', 'detached'].includes(job.statusKey)))
const runningCount = computed(() => runningJobs.value.length)
const waitingCount = computed(() => waitingJobs.value.length)
const failedCount = computed(() => failedJobs.value.length)
const detachedCount = computed(() => detachedJobs.value.length)
const statusTabs = computed(() => [
  { key: 'all', label: '全部', count: activeJobs.value.length, items: activeJobs.value },
  { key: 'running', label: '运行中', count: runningCount.value, items: runningJobs.value },
  { key: 'waiting', label: '等待中', count: waitingCount.value, items: waitingJobs.value },
  { key: 'detached', label: '已清理', count: detachedCount.value, items: detachedJobs.value },
  { key: 'failed', label: '失败', count: failedCount.value, items: failedJobs.value },
  { key: 'completed', label: '已完成', count: completedJobs.value.length, items: completedJobs.value }
])
const visibleJobs = computed(() => taskTab.value === 'history' ? historyJobs.value : (statusTabs.value.find((state) => state.key === taskStatusTab.value)?.items || []))
const pageCount = computed(() => Math.max(1, Math.ceil(visibleJobs.value.length / PAGE_SIZE)))
const visiblePagedJobs = computed(() => visibleJobs.value.slice((page.value - 1) * PAGE_SIZE, page.value * PAGE_SIZE))
const selectedJobs = computed(() => adaptedJobs.value.filter((job) => selectedIds.has(job.id)))
const runnableSelectedJobs = computed(() => selectedJobs.value.filter((job) => job.canRun))
const allVisibleSelected = computed(() => visiblePagedJobs.value.length > 0 && visiblePagedJobs.value.every((job) => selectedIds.has(job.id)))
const memoryLabel = computed(() => {
  const memory = backendStatus.value.hardware?.memory
  return memory ? `${memory.label || ''} · ${memory.used_percent || 0}%` : '未连接'
})
const gpuLabel = computed(() => backendStatus.value.hardware?.gpus?.[0]?.label || '未检测')
const transcodeSummary = computed(() => transcodeFormatLabel(postprocessSettings.target_codec))
const allEncodingPresets = computed(() => [
  ...standardEncodingPresets,
  ...(postprocessSettings.custom_encoding_presets || []).map((preset, index) => ({
    key: `custom-${index}-${preset.name}`,
    name: preset.name,
    desc: `${preset.encoder} · ${preset.preset || 'preset'} · CQ/CRF ${preset.quality || 36}`,
    codec: preset.codec,
    encoder: preset.encoder,
    preset: preset.preset,
    preset_flag: preset.preset_flag || '-preset',
    quality: preset.quality || preset.crf || 36
  }))
])
const activeEncodingKey = computed(() => {
  const match = allEncodingPresets.value.find((preset) => (
    preset.codec === postprocessSettings.target_codec
    && preset.encoder === postprocessSettings.target_encoder
    && String(preset.preset) === String(postprocessSettings.preset)
    && Number(preset.quality) === Number(postprocessSettings.crf)
  ))
  return match?.key || ''
})
const ffmpegMode = computed(() => postprocessSettings.ffmpeg_mode || (postprocessSettings.ffmpeg_custom_enabled ? 'custom' : 'standard'))
const ffmpegPreview = computed(() => {
  const encoder = postprocessSettings.target_encoder || (String(postprocessSettings.target_codec || 'av1') === 'av1' ? 'av1_nvenc' : 'libx265')
  const qualityFlag = qualityFlagForEncoder(encoder)
  const presetFlag = postprocessSettings.preset_flag || '-preset'
  return `ffmpeg -hide_banner -nostdin -i "<输入文件>" -c:v ${encoder} ${presetFlag} ${postprocessSettings.preset || 'p1'} ${qualityFlag} ${postprocessSettings.crf || 36} -c:a copy "<输出文件>" -y`
})
const mergedQbCategories = computed(() => mergeUnique(qbOptions.categories, postprocessSettings.allowed_categories))
const mergedQbTags = computed(() => mergeUnique(qbOptions.tags, postprocessSettings.required_tags))

async function loadConsole() {
  const generation = ++refreshGeneration
  loading.value = true
  errorMessage.value = ''
  try {
    const [consolePayload, postprocessPayload] = await Promise.all([
      api('/api/subtitle/console'),
      api('/api/postprocess/tasks?limit=200')
    ])
    if (generation !== refreshGeneration) return
    Object.assign(connection, consolePayload.connection || {})
    computeEnabled.value = !!connection.subtitle_backend_url
    Object.assign(settings, consolePayload.compute_settings || {})
    jobs.value = consolePayload.jobs || []
    postprocessTasks.value = postprocessPayload.tasks || []
    applyPostprocessPayload(postprocessPayload)
    backendStatus.value = postprocessPayload.worker_status || consolePayload.backend_status || {}
  } catch (error) {
    errorMessage.value = error.message || '读取任务中心失败'
  } finally {
    loading.value = false
  }
}

const standardEncodingPresets = [
  { key: 'av1-nvenc-balanced', name: 'AV1 NVENC · 均衡', desc: 'RTX 40/50 系列常用，速度快，体积小。', codec: 'av1', encoder: 'av1_nvenc', preset: 'p4', preset_flag: '-preset', quality: 32 },
  { key: 'av1-nvenc-fast', name: 'AV1 NVENC · 快速', desc: '适合批量转码，速度优先。', codec: 'av1', encoder: 'av1_nvenc', preset: 'p1', preset_flag: '-preset', quality: 36 },
  { key: 'av1-qsv', name: 'AV1 QSV', desc: 'Intel 核显/独显硬件编码。', codec: 'av1', encoder: 'av1_qsv', preset: 'medium', preset_flag: '-preset', quality: 34 },
  { key: 'av1-svt', name: 'SVT-AV1', desc: 'CPU AV1，质量好但速度较慢。', codec: 'av1', encoder: 'libsvtav1', preset: '8', preset_flag: '-preset', quality: 34 },
  { key: 'av1-aom', name: 'libaom-av1', desc: 'CPU AV1，高压缩率，速度最慢。', codec: 'av1', encoder: 'libaom-av1', preset: '6', preset_flag: '-cpu-used', quality: 34 },
  { key: 'h265-nvenc-balanced', name: 'H.265 NVENC · 均衡', desc: 'NVIDIA 硬件 H.265，兼容性更高。', codec: 'h265', encoder: 'hevc_nvenc', preset: 'p4', preset_flag: '-preset', quality: 28 },
  { key: 'h265-nvenc-fast', name: 'H.265 NVENC · 快速', desc: '转码速度优先。', codec: 'h265', encoder: 'hevc_nvenc', preset: 'p1', preset_flag: '-preset', quality: 30 },
  { key: 'h265-qsv', name: 'H.265 QSV', desc: 'Intel Quick Sync H.265。', codec: 'h265', encoder: 'hevc_qsv', preset: 'medium', preset_flag: '-preset', quality: 28 },
  { key: 'h265-x265', name: 'libx265', desc: 'CPU H.265，画质稳定，速度较慢。', codec: 'h265', encoder: 'libx265', preset: 'medium', preset_flag: '-preset', quality: 24 }
]

async function refreshAll() {
  if (isCompareView) return
  const generation = ++refreshGeneration
  const [subtitlePayload, postprocessPayload] = await Promise.all([
    api('/api/subtitle/jobs?limit=0'),
    api('/api/postprocess/tasks?limit=200')
  ])
  if (generation !== refreshGeneration) return
  jobs.value = subtitlePayload.jobs || []
  postprocessTasks.value = postprocessPayload.tasks || []
  applyPostprocessPayload(postprocessPayload)
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
    refreshGeneration += 1
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
  errorMessage.value = ''
  try {
    if (computeCallbackWarning.value) {
      throw new Error(computeCallbackWarning.value)
    }
    refreshGeneration += 1
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
  if (ffmpegMode.value === 'custom' && !String(postprocessSettings.ffmpeg_custom_template || '').trim()) {
    errorMessage.value = '请填写自定义 FFmpeg 模板，或选择标准编码设置。'
    return
  }
  savingTranscode.value = true
  try {
    refreshGeneration += 1
    const payload = await postJson('/api/postprocess/ffmpeg-settings/apply', {
      ...postprocessSettings,
      ffmpeg_standard_command: ffmpegPreview.value
    })
    Object.assign(postprocessSettings, normalizePostprocessSettings(payload.settings || {}))
    transcodeDialog.value = false
    notice.value = payload.warning || '转码设置已保存。'
    await loadPostprocessSettings()
  } catch (error) {
    errorMessage.value = error.message || '保存转码设置失败'
  } finally {
    savingTranscode.value = false
  }
}

async function loadPostprocessSettings() {
  const generation = ++refreshGeneration
  const payload = await api('/api/postprocess/settings')
  if (generation !== refreshGeneration) return
  Object.assign(postprocessSettings, normalizePostprocessSettings(payload.settings || {}))
}

function applyEncodingPreset(preset) {
  postprocessSettings.target_codec = preset.codec || 'av1'
  postprocessSettings.target_encoder = preset.encoder || ''
  postprocessSettings.preset = String(preset.preset || 'p1')
  postprocessSettings.preset_flag = preset.preset_flag || '-preset'
  postprocessSettings.crf = Number(preset.quality || preset.crf || 36)
}

function selectEncodingPreset(key) {
  const preset = allEncodingPresets.value.find((item) => item.key === key)
  if (preset) applyEncodingPreset(preset)
}

function setFfmpegMode(mode) {
  postprocessSettings.ffmpeg_mode = mode === 'custom' ? 'custom' : 'standard'
  postprocessSettings.ffmpeg_standard_enabled = postprocessSettings.ffmpeg_mode === 'standard'
  postprocessSettings.ffmpeg_custom_enabled = postprocessSettings.ffmpeg_mode === 'custom'
}

function qualityFlagForEncoder(encoder) {
  if (['av1_qsv', 'hevc_qsv'].includes(String(encoder))) return '-global_quality'
  return String(encoder || '').endsWith('_nvenc') ? '-cq' : '-crf'
}

function transcodeFormatLabel(codec) {
  return String(codec || 'av1').toLowerCase() === 'h265' ? 'H.265' : 'AV1'
}

function applyPostprocessPayload(payload = {}) {
  postprocessTasks.value = payload.tasks || []
  if (!transcodeDialog.value && !savingTranscode.value) {
    Object.assign(postprocessSettings, normalizePostprocessSettings(payload.settings || {}))
  }
}

async function runPostprocessQueue() {
  try {
    await postJson('/api/subscriptions/tasks/postprocess_qb/run', {})
    notice.value = '后处理链路已触发。'
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

async function runSelected() {
  const runnable = runnableSelectedJobs.value
  if (!runnable.length) {
    notice.value = '选中的任务里没有可运行的等待任务。'
    return
  }
  runningSelected.value = true
  try {
    const payload = await postJson('/api/postprocess/tasks/run-selected', {
      task_ids: runnable.map((job) => job.rawId)
    })
    const updated = Number(payload.updated || 0)
    const deferred = Number(payload.deferred || payload.queued || payload.waiting || 0)
    notice.value = `已处理 ${runnable.length} 个选中任务：派发 ${updated}，留队 ${deferred}。`
    selectedIds.clear()
    await refreshAll()
  } catch (error) {
    errorMessage.value = error.message || '批量运行失败'
  } finally {
    runningSelected.value = false
  }
}

async function deleteSelected() {
  const targets = selectedJobs.value
  if (!targets.length) {
    notice.value = '请先选择要删除的任务。'
    return
  }
  const activeCount = targets.filter((job) => ['queued', 'running', 'translating'].includes(job.statusKey)).length
  const names = targets.slice(0, 5).map((job) => job.title).join('、')
  const activeLabel = activeCount > 0 ? `\n其中 ${activeCount} 个运行中/等待中的任务会先终止，再删除记录。` : ''
  const ok = window.confirm(`将删除 ${targets.length} 个任务记录${names ? `：${names}` : ''}。不会删除媒体文件或字幕结果。${activeLabel}\n继续吗？`)
  if (!ok) return
  deletingSelected.value = true
  errorMessage.value = ''
  try {
    let deleted = 0
    for (const job of targets) {
      if (job.sourceType === 'postprocess') {
        if (['queued', 'running', 'translating'].includes(job.statusKey)) {
          await postJson(`/api/postprocess/tasks/${job.rawId}/cancel`, {})
        }
        await deleteJson(`/api/postprocess/tasks/${job.rawId}`)
      } else {
        if (['queued', 'running', 'translating'].includes(job.statusKey)) {
          await postJson(`/api/subtitle/jobs/${job.fileId}/cancel`, {})
        }
        await deleteJson(`/api/subtitle/jobs/${job.fileId}`)
      }
      selectedIds.delete(job.id)
      deleted += 1
    }
    notice.value = `已删除 ${deleted} 个任务记录。`
    await refreshAll()
  } catch (error) {
    errorMessage.value = error.message || '批量删除失败'
  } finally {
    deletingSelected.value = false
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
    progressTone: progressTone(statusKey),
    createdLabel: formatTime(job.created_at),
    createdAt: job.created_at,
    updatedAt: job.updated_at,
    finishedAt: job.finished_at,
    modelLabel: `${job.model || 'large-v3'} / ${job.source_language || 'auto'} => ${job.target_language || 'zh'}`,
    canRetry: ['failed', 'completed'].includes(statusKey),
    canDelete: true,
    canCancel: false,
    resultSrt: job.translated_srt || job.bilingual_srt
  }
}

function adaptPostprocessJob(task) {
  const phase = postprocessPhase(task.status)
  const qbIssue = postprocessQbIssue(task)
  const statusKey = qbIssue ? qbIssue.statusKey : postprocessStatusKey(task.status)
  const avId = task.av_id || task.id || '后处理任务'
  const workerJob = findTranscodeJob(task)
  const progressInfo = postprocessProgressInfo(task, phase, statusKey, workerJob, qbIssue)
  const waitingDetail = postprocessWaitingDetail(task, statusKey, qbIssue)
  const displayPath = postprocessDisplayPath(task, waitingDetail, qbIssue)
  return {
    raw: task,
    id: `postprocess:${task.id}`,
    rawId: task.id,
    sourceType: 'postprocess',
    phase,
    phaseLabel: postprocessPhaseLabel(task, phase),
    title: phase === 'subtitle' ? `${avId} · 生成字幕` : phase === 'transcode' ? `${avId} · 转码` : avId,
    path: displayPath,
    statusKey,
    statusLabel: postprocessStatusLabel(task.status, phase, statusKey, qbIssue),
    progressTone: postprocessProgressTone(task.status, statusKey, qbIssue),
    createdLabel: formatTime(task.created_at),
    createdAt: task.created_at,
    updatedAt: task.updated_at || task.created_at,
    finishedAt: task.finished_at,
    modelLabel: `${task.target_codec || postprocessSettings.target_codec || 'av1'} / ${task.task_type || '后处理'}`,
    ...progressInfo,
    progressDetail: progressInfo.progressDetail || waitingDetail,
    canRun: ['waiting_worker', 'ready_to_run'].includes(String(task.status || '')),
    canRetry: ['failed', 'ignored', 'conflict', 'expired'].includes(String(task.status || '')),
    canDelete: true,
    canCancel: !['completed', 'ignored'].includes(String(task.status || '')),
    resultSrt: ''
  }
}

function findTranscodeJob(task) {
  const data = task?.data || {}
  const workerJobId = data.worker_job_id || data.worker_result?.job_id || data.worker_result?.id
  return transcodeJobLookup.value.get(String(workerJobId || '')) || transcodeJobLookup.value.get(String(task?.id || '')) || null
}

function postprocessQbIssue(task) {
  const state = String(task?.data?.qb_state || '').trim()
  if (state === 'missingFiles') {
    if (postprocessQbMissingIsExpected(task)) {
      return {
        type: 'qb_source_trashed',
        statusKey: 'detached',
        label: '源文件已清理',
        path: task?.output_path || task?.input_path || task?.data?.content_path || '源文件已移动到 trash',
        detail: '源文件已移动到 trash，qB 显示文件丢失是正常现象。'
      }
    }
    return {
      type: 'qb_missing_files',
      statusKey: 'failed',
      label: '文件丢失',
      path: task?.data?.content_path || task?.input_path || task?.output_path || 'qB 文件丢失',
      detail: 'qB 标记文件丢失，请在 qB 中重新校验或重新下载。'
    }
  }
  return null
}

function postprocessQbMissingIsExpected(task) {
  const data = task?.data || {}
  const sourceTrash = data.source_trash || {}
  const status = String(task?.status || '')
  return status === 'completed'
    || sourceTrash.status === 'moved'
    || sourceTrash.status === 'skipped'
    || !!sourceTrash.target
    || !!data.version_id
    || !!data.activation
}

function postprocessProgressInfo(task, phase, statusKey, workerJob, qbIssue = null) {
  if (qbIssue) {
    const completed = statusKey === 'detached'
    return {
      showProgress: completed,
      progressPercent: completed ? 100 : 0,
      progressLabel: completed ? '100%' : '',
      progressDetail: qbIssue.detail
    }
  }
  const status = String(task?.status || '')
  if (status === 'downloading') {
    const progress = clampPercent(Number(task?.data?.qb_progress || 0))
    const percent = Math.round(progress * 100)
    return {
      showProgress: percent > 0,
      progressPercent: percent,
      progressLabel: percent > 0 ? `${percent}%` : '',
      progressDetail: percent > 0 ? `qB 下载中 ${percent}%` : 'qB 下载中'
    }
  }
  if (phase !== 'transcode' && statusKey !== 'translating') {
    const completed = statusKey === 'completed'
    return {
      showProgress: completed,
      progressPercent: completed ? 100 : 0,
      progressLabel: completed ? '100%' : '',
      progressDetail: ''
    }
  }
  const subtitleStatus = task?.data?.subtitle_status || {}
  const rawProgress = Number(workerJob?.progress ?? subtitleStatus.progress ?? task?.data?.worker_result?.progress ?? task?.data?.transcode_progress)
  const fallbackProgress = statusKey === 'completed'
    ? 1
    : String(task?.status || '') === 'worker_done' || String(task?.status || '') === 'transcode_validating'
      ? 0.98
      : statusKey === 'running'
        ? 0.02
        : 0
  const progress = clampPercent(Number.isFinite(rawProgress) ? rawProgress : fallbackProgress)
  const percent = Math.round(progress * 100)
  const workerStatus = String(workerJob?.status || '')
  const detail = transcodeProgressDetail(workerJob)
    || (subtitleStatus.message ? String(subtitleStatus.message) : '')
    || (workerStatus === 'queued' ? '算力端排队中' : '')
    || (workerStatus === 'failed' ? (workerJob?.error || '算力端转码失败') : '')
    || (statusKey === 'running' ? '等待算力端回报进度' : '')
  return {
    showProgress: statusKey === 'running' || statusKey === 'translating' || percent > 0,
    progressPercent: percent,
    progressLabel: `${percent}%`,
    progressDetail: detail
  }
}

function postprocessPhaseLabel(task, phase) {
  const taskType = String(task?.task_type || '')
  if (phase === 'transcode') return '转码'
  if (phase === 'subtitle') return '字幕'
  if (taskType.startsWith('wash_')) return '洗版'
  if (taskType === 'subscription') return '订阅'
  if (taskType === 'external_qb') return '外部 qB'
  return '后处理'
}

function transcodeProgressDetail(job) {
  if (!job) return ''
  if (job.message) return job.message
  const parts = []
  const processed = Number(job.processed_seconds || 0)
  const duration = Number(job.duration || 0)
  if (processed && duration) parts.push(`${formatDuration(processed)} / ${formatDuration(duration)}`)
  if (job.fps) parts.push(`${job.fps} fps`)
  if (job.speed) parts.push(job.speed)
  if (job.frame) parts.push(`frame ${job.frame}`)
  return parts.join(' · ') || job.last_progress_line || ''
}

function clampPercent(value) {
  return Math.max(0, Math.min(1, value))
}

function postprocessPhase(status) {
  const value = String(status || '')
  if (['subtitle_processing', 'subtitle_validating', 'transcode_done'].includes(value)) return 'subtitle'
  if (['sent_to_worker', 'transcoding', 'worker_done', 'transcode_validating'].includes(value)) return 'transcode'
  return 'postprocess'
}

function postprocessStatusKey(status) {
  const value = String(status || '')
  if (['waiting_worker', 'waiting_input', 'ready_to_run', 'created'].includes(value)) return 'queued'
  if (['dispatching', 'sent_to_worker', 'transcoding', 'worker_done', 'transcode_validating'].includes(value)) return 'running'
  if (['subtitle_processing', 'subtitle_validating', 'transcode_done'].includes(value)) return 'translating'
  if (value === 'completed') return 'completed'
  if (['failed', 'ignored', 'conflict', 'expired'].includes(value)) return 'failed'
  return 'queued'
}

function postprocessStatusLabel(status, phase, statusKey, qbIssue = null) {
  if (qbIssue) return qbIssue.label
  if (String(status || '') === 'dispatching') return '派发中'
  if (statusKey === 'running' && phase === 'transcode') return '转码中'
  if (statusKey === 'translating' && phase === 'subtitle') return '生成字幕中'
  if (statusKey === 'queued') {
    const value = String(status || '')
    if (value === 'waiting_worker') return '等待算力端'
    if (value === 'waiting_input') return '等待输入文件'
    if (value === 'ready_to_run') return '等待队列'
    if (['torrent_pushed', 'downloading'].includes(value)) return '等待下载'
    if (value === 'created') return '未接管'
    return '等待中'
  }
  if (statusKey === 'failed') return '失败'
  if (statusKey === 'completed') return '已完成'
  if (statusKey === 'detached') return '源文件已清理'
  return status || statusLabel(statusKey)
}

function postprocessWaitingDetail(task, statusKey, qbIssue = null) {
  if (qbIssue) return qbIssue.detail
  if (statusKey !== 'queued') return ''
  const status = String(task?.status || '')
  if (status === 'waiting_worker') return '算力端离线或未就绪'
  if (status === 'waiting_input') return '等待下载完成或路径回写'
  if (status === 'ready_to_run') {
    if (task?.input_path) return '等待队列执行'
    return '等待链路写入输入文件'
  }
  if (status === 'torrent_pushed') return '等待 qB 下载完成并回写路径'
  if (status === 'downloading') {
    const progress = Number(task?.data?.qb_progress)
    return Number.isFinite(progress) && progress > 0 ? `qB 下载中 ${Math.round(progress * 100)}%` : 'qB 下载中'
  }
  if (status === 'created') return '未匹配到 qB 的 moviemuse 标签下载；如果文件已在媒体库，请手动触发后处理。'
  return task?.error_message || ''
}

function postprocessDisplayPath(task, waitingDetail, qbIssue = null) {
  if (qbIssue) return qbIssue.path
  const status = String(task?.status || '')
  if (task?.input_path) return task.input_path
  if (task?.output_path) return task.output_path
  if (status === 'created') return '未接管下载文件'
  if (status === 'ready_to_run') return '等待输入文件'
  return task?.error_message || waitingDetail || '等待链路写入输入文件'
}

function progressTone(statusKey) {
  if (statusKey === 'completed') return 'completed'
  if (statusKey === 'detached') return 'detached'
  if (statusKey === 'failed') return 'failed'
  if (['running', 'translating'].includes(statusKey)) return 'running'
  return 'queued'
}

function postprocessProgressTone(status, statusKey, qbIssue = null) {
  if (qbIssue) return qbIssue.statusKey === 'detached' ? 'detached' : 'failed'
  if (String(status || '') === 'created') return 'idle'
  return progressTone(statusKey)
}

function statusLabel(status) {
  return { queued: '等待中', running: '运行中', translating: '翻译中', failed: '失败', completed: '已完成', detached: '源文件已清理' }[status] || status
}

function normalizePostprocessSettings(payload = {}) {
  return {
    auto_transcode_enabled: !!payload.auto_transcode_enabled,
    auto_subtitle_enabled: !!payload.auto_subtitle_enabled,
    worker_auto_run: !!payload.worker_auto_run,
    download_dir: payload.download_dir || '/media/study3',
    output_dir: payload.output_dir || '/media/压制',
    target_codec: payload.target_codec || 'av1',
    target_encoder: payload.target_encoder || (payload.target_codec === 'h265' ? 'libx265' : 'av1_nvenc'),
    crf: Number(payload.crf || 36),
    preset: payload.preset || 'p1',
    preset_flag: payload.preset_flag || '-preset',
    ffmpeg_mode: payload.ffmpeg_mode || (payload.ffmpeg_custom_enabled ? 'custom' : 'standard'),
    ffmpeg_standard_enabled: payload.ffmpeg_mode ? payload.ffmpeg_mode === 'standard' : payload.ffmpeg_standard_enabled !== false,
    ffmpeg_custom_enabled: payload.ffmpeg_mode ? payload.ffmpeg_mode === 'custom' : !!payload.ffmpeg_custom_enabled,
    ffmpeg_custom_template: payload.ffmpeg_custom_template || '',
    custom_encoding_presets: Array.isArray(payload.custom_encoding_presets) ? payload.custom_encoding_presets : [],
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

function formatDuration(value) {
  const total = Math.max(0, Math.floor(Number(value || 0)))
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const seconds = total % 60
  return [hours, minutes, seconds].map((part) => String(part).padStart(2, '0')).join(':')
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
  display: grid;
  align-content: start;
  min-height: 148px;
  padding: 22px;
  border-color: var(--mm-border);
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.service-card span {
  color: var(--mm-muted);
  font-size: 14px;
  line-height: 1.35;
}

.service-card strong {
  display: block;
  margin-top: 12px;
  font-size: 28px;
  font-weight: 650;
  line-height: 1.16;
}

.service-card em {
  display: block;
  margin-top: 8px;
  color: var(--mm-primary);
  font-size: 15px;
  font-style: normal;
  font-weight: 500;
  line-height: 1.35;
}

.service-card em.on {
  color: var(--mm-primary);
}

.service-card i {
  position: absolute;
  top: 28px;
  right: 26px;
  width: 14px;
  height: 14px;
  border-radius: 999px;
  background: var(--mm-primary);
  box-shadow: 0 0 0 10px var(--mm-primary-soft);
}

.service-card i.on {
  background: var(--mm-primary);
  box-shadow: 0 0 0 10px var(--mm-primary-soft);
}

.task-panel {
  --task-panel-gutter: 24px;
  overflow: hidden;
  padding: 0;
}

.panel-head {
  align-items: flex-start;
  padding: var(--task-panel-gutter);
}

.toolbar {
  align-items: center;
  padding: 16px var(--task-panel-gutter);
  border-top: 1px solid var(--mm-border);
}

.task-panel :deep(.task-table) {
  width: calc(100% - (var(--task-panel-gutter) * 2));
  margin: 0 var(--task-panel-gutter) var(--task-panel-gutter);
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
  background: var(--mm-card-bg);
  color: var(--mm-primary);
  box-shadow: var(--mm-shadow);
}

.state-tabs {
  margin: 0;
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

.state-dot.all {
  background: var(--mm-primary);
}

.state-dot.running,
.state-dot.failed {
  background: var(--mm-primary);
}

.state-dot.completed {
  background: var(--mm-success);
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
  padding: 18px 24px 24px;
  border-top: 1px solid var(--mm-border);
}

.form-grid,
.hardware-grid,
.provider-grid,
.chip-columns {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}

.config-warning {
  margin-bottom: 16px;
  padding: 14px 16px;
  border: 1px solid rgba(255, 47, 92, 0.36);
  border-radius: 8px;
  background: rgba(255, 47, 92, 0.08);
  color: var(--mm-primary);
  font-weight: 600;
  line-height: 1.6;
}

.compute-settings {
  display: grid;
  gap: 16px;
}

.settings-section {
  display: grid;
  gap: 12px;
  padding: 14px;
  border: 1px solid var(--mm-border);
  border-radius: 8px;
  background: var(--mm-surface);
}

.compute-section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.compute-section-head strong {
  font-size: 18px;
  font-weight: 650;
}

.compute-toggle {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  justify-self: start;
  min-height: 34px;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--mm-text);
  font-weight: 650;
  white-space: nowrap;
}

.compute-toggle span {
  color: var(--mm-text);
  font-size: 15px;
}

.compute-toggle input {
  width: 22px;
  height: 22px;
  min-height: 22px;
  padding: 0;
  accent-color: var(--mm-primary);
}

.compact-grid {
  gap: 14px 20px;
}

.settings-section :deep(.mm-field) {
  min-width: 0;
}

.settings-section textarea {
  min-height: 64px;
}

.hardware-section .hardware-grid div {
  min-height: 82px;
  padding: 14px;
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
  background: var(--mm-control-bg);
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
  background: var(--mm-control-bg);
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
  background: var(--mm-primary-soft);
}

.ffmpeg-send-panel {
  display: grid;
  gap: 12px;
  padding: 14px;
  border: 1px solid var(--mm-border);
  border-radius: 8px;
  background: var(--mm-surface);
}

.section-head {
  display: grid;
  gap: 6px;
}

.section-head.compact {
  margin-bottom: 10px;
}

.section-head p {
  margin-top: 6px;
  color: var(--mm-muted);
  line-height: 1.6;
}

.ffmpeg-mode-cards {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}

.choice-card {
  display: grid;
  gap: 6px;
  min-height: 92px;
  padding: 16px;
  border: 1px solid var(--mm-border);
  border-radius: 8px;
  background: var(--mm-control-bg);
  color: var(--mm-text);
  text-align: left;
  cursor: pointer;
}

.choice-card.active {
  border-color: var(--mm-primary);
  background: var(--mm-primary-soft);
}

.choice-card strong {
  font-size: 16px;
  font-weight: var(--mm-font-weight-semibold);
  line-height: 1.25;
}

.choice-card span {
  color: var(--mm-muted);
  font-size: 13px;
  line-height: 1.45;
}

.ffmpeg-template {
  font-family: ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace;
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
  background: var(--mm-control-bg);
}

@media (max-width: 1100px) {
  .service-grid,
  .form-grid,
  .hardware-grid,
  .provider-grid,
  .ffmpeg-mode-cards,
  .chip-columns {
    grid-template-columns: 1fr;
  }

  .panel-head,
  .toolbar,
  .qb-option-head {
    display: grid;
  }

  .task-panel {
    --task-panel-gutter: 16px;
  }
}
</style>
