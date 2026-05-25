<template>
  <v-app>
    <v-main class="app-shell">
      <aside class="side-shell">
        <div class="brand">
          <div class="brand-mark">M</div>
          <div>
            <h1>Media Toolbox</h1>
            <p>字幕算力控制台</p>
          </div>
        </div>

        <nav class="nav-list">
          <a href="/">重复视频</a>
          <a class="active" href="/subtitles">字幕任务</a>
          <a href="/api/scan">扫描 API</a>
        </nav>

        <div class="side-card">
          <div class="side-card-head">
            <strong>默认设置</strong>
            <v-btn size="small" variant="text" @click="computeDialog = true">修改</v-btn>
          </div>
          <p>{{ whisperSummary }}</p>
          <p>{{ activeProvider?.name || 'Google 免费翻译' }}</p>
        </div>
      </aside>

      <section class="main-panel">
        <header class="topbar">
          <div>
            <h2>任务中心</h2>
            <p>集中查看字幕生成、翻译、失败重试和历史记录。</p>
          </div>
          <div class="topbar-actions">
            <v-btn variant="outlined" prepend-icon="mdi-refresh" @click="refreshJobs">刷新</v-btn>
            <v-btn variant="outlined" href="/docs">API 文档</v-btn>
          </div>
        </header>

        <section class="status-grid service-grid">
          <button class="status-card" type="button" @click="computeDialog = true">
            <span>算力端</span>
            <strong>{{ backendOnline ? '在线' : '离线' }}</strong>
            <em :class="{ on: backendOnline }">{{ connection.subtitle_backend_url || '未配置地址' }}</em>
            <i :class="['service-dot', { online: backendOnline }]" aria-hidden="true"></i>
          </button>

          <button class="status-card" type="button" @click="translateDialog = true">
            <span>翻译后端</span>
            <strong>{{ activeProvider?.name || '未配置' }}</strong>
            <em :class="{ on: translationReady }">{{ translationReady ? '可用' : '待配置' }}</em>
            <i :class="['service-dot', { online: translationReady }]" aria-hidden="true"></i>
          </button>
        </section>

        <v-alert v-if="submitNotice" class="submit-notice" type="info" variant="tonal">
          {{ submitNotice }}
        </v-alert>

        <v-sheet class="task-console" elevation="0">
          <div class="task-console-head">
            <div>
              <h3>任务管理</h3>
              <p>每 4 秒自动刷新；选择状态查看队列，勾选任务后可批量操作。</p>
              <div class="task-metrics">
                <div class="task-metric">
                  <span>当前队列</span>
                  <strong>{{ queueCount }}</strong>
                  <em>{{ runningCount }} 运行 / {{ waitingCount }} 等待</em>
                </div>
                <div class="task-metric">
                  <span>今日完成</span>
                  <strong>{{ todayCompleted }}</strong>
                  <em>{{ failedCount }} 个失败待处理</em>
                </div>
              </div>
            </div>
          </div>
          <div class="task-toolbar">
            <v-tabs v-model="taskTab" class="task-tabs" color="primary" density="comfortable">
              <v-tab value="current">当前任务</v-tab>
              <v-tab value="history">历史任务</v-tab>
            </v-tabs>

            <div class="task-actionbar">
              <span v-if="selectedJobs.length" class="selection-count">已选择 {{ selectedJobs.length }} 个任务</span>
              <div class="bulk-actions">
                <v-btn variant="outlined" prepend-icon="mdi-checkbox-multiple-marked-outline" @click="toggleSelectVisible">
                  {{ allVisibleSelected ? '取消本页全选' : '全选本页' }}
                </v-btn>
                <v-btn
                  color="primary"
                  variant="flat"
                  prepend-icon="mdi-reload"
                  :disabled="selectedJobs.length === 0"
                  :loading="retryingSelected"
                  @click="retrySelected"
                >
                  批量重试
                </v-btn>
                <v-btn
                  variant="outlined"
                  prepend-icon="mdi-stop-circle-outline"
                  :disabled="selectedJobs.length === 0"
                  @click="unsupportedAction('批量取消')"
                >
                  批量取消
                </v-btn>
                <v-btn
                  color="error"
                  variant="tonal"
                  prepend-icon="mdi-delete-outline"
                  :disabled="selectedJobs.length === 0"
                  @click="unsupportedAction('批量删除')"
                >
                  批量删除
                </v-btn>
              </div>
            </div>
          </div>

          <v-window v-model="taskTab">
            <v-window-item value="current">
              <v-tabs v-model="taskStatusTab" class="state-tabs" color="primary" density="comfortable">
                <v-tab v-for="state in statusTabs" :key="state.key" :value="state.key">
                  <span :class="['state-dot', state.key]"></span>
                  {{ state.label }}
                  <em class="tab-count">{{ state.count }}</em>
                </v-tab>
              </v-tabs>
              <transition-group name="task-list" tag="div" class="task-card-list">
                <article v-for="job in pagedStatusJobs" :key="job.id" class="task-card">
                  <v-checkbox
                    class="task-check"
                    density="compact"
                    hide-details
                    :model-value="selectedIds.has(job.id)"
                    @update:model-value="toggleJob(job.id)"
                  />
                  <div class="task-main">
                    <div class="task-title-line">
                      <strong>{{ job.title }}</strong>
                      <span :class="['status-pill', job.statusKey]">{{ job.statusLabel }}</span>
                    </div>
                    <p>{{ job.path }}</p>
                    <div class="task-meta">
                      <span>{{ job.step }}</span>
                      <span>{{ job.createdLabel }}</span>
                      <span>{{ job.modelLabel }}</span>
                    </div>
                    <v-progress-linear
                      class="task-progress"
                      :model-value="job.percent"
                      height="6"
                      rounded
                      :color="progressColor(job.statusKey)"
                    />
                  </div>
                  <div class="task-actions">
                    <span>{{ job.percent }}%</span>
                    <v-btn
                      v-if="job.canRetry"
                      size="small"
                      variant="outlined"
                      :loading="retryingJob[job.id]"
                      @click="retryJob(job.id)"
                    >
                      重试
                    </v-btn>
                    <v-btn v-if="job.originalSrt" size="small" variant="text" :href="`/subtitles/jobs/${job.id}/files/original_srt`">
                      原文
                    </v-btn>
                    <v-btn v-if="job.resultSrt" size="small" variant="text" :href="`/subtitles/jobs/${job.id}/files/translated_srt`">
                      结果
                    </v-btn>
                  </div>
                </article>
              </transition-group>
              <div v-if="activeStatusJobs.length === 0" class="empty-line">这个状态暂时没有任务。</div>
              <v-pagination
                v-if="statusPageCount > 1"
                v-model="statusPage"
                class="task-pagination"
                :length="statusPageCount"
                :total-visible="7"
                density="comfortable"
              />
            </v-window-item>

            <v-window-item value="history">
              <div class="history-toolbar">
                <span>共 {{ historyJobs.length }} 条历史记录</span>
                <v-btn
                  v-if="failedCount"
                  color="primary"
                  variant="flat"
                  :loading="retryingFailed"
                  @click="retryFailed"
                >
                  重试全部失败 {{ failedCount }}
                </v-btn>
              </div>
              <transition-group name="task-list" tag="div" class="task-card-list">
                <article v-for="job in pagedHistoryJobs" :key="job.id" class="task-card compact">
                  <v-checkbox
                    class="task-check"
                    density="compact"
                    hide-details
                    :model-value="selectedIds.has(job.id)"
                    @update:model-value="toggleJob(job.id)"
                  />
                  <div class="task-main">
                    <div class="task-title-line">
                      <strong>{{ job.title }}</strong>
                      <span :class="['status-pill', job.statusKey]">{{ job.statusLabel }}</span>
                    </div>
                    <p>{{ job.path }}</p>
                    <div class="task-meta">
                      <span>{{ job.step }}</span>
                      <span>{{ job.createdLabel }}</span>
                    </div>
                  </div>
                  <div class="task-actions">
                    <v-btn
                      v-if="job.canRetry"
                      size="small"
                      variant="outlined"
                      :loading="retryingJob[job.id]"
                      @click="retryJob(job.id)"
                    >
                      重试
                    </v-btn>
                    <v-btn
                      v-if="job.resultSrt"
                      size="small"
                      variant="text"
                      :href="`/subtitles/jobs/${job.id}/files/translated_srt`"
                    >
                      下载
                    </v-btn>
                  </div>
                </article>
              </transition-group>
              <div v-if="historyJobs.length === 0" class="empty-line">还没有历史任务。</div>
              <v-pagination
                v-if="historyPageCount > 1"
                v-model="historyPage"
                class="task-pagination"
                :length="historyPageCount"
                :total-visible="7"
                density="comfortable"
              />
            </v-window-item>
          </v-window>
        </v-sheet>
      </section>

      <v-dialog v-model="computeDialog" max-width="920" scrim="rgba(32,31,40,.42)">
        <v-card class="config-dialog" elevation="12">
          <div class="dialog-head">
            <div>
              <div class="dialog-kicker">配置</div>
              <div class="dialog-title">Windows 算力端</div>
            </div>
            <v-btn icon="mdi-close" variant="text" size="large" color="#6e6f7c" @click="computeDialog = false" />
          </div>

          <v-card-text class="pa-6">
            <v-checkbox v-model="computeEnabled" color="primary" hide-details class="mb-5">
              <template #label><strong class="toggle-title">启用 Windows 算力端</strong></template>
            </v-checkbox>

            <v-row>
              <v-col cols="12" md="6">
                <div class="field-label">地址</div>
                <v-text-field v-model="connection.subtitle_backend_url" placeholder="http://WINDOWS-IP:18181" />
                <div class="provider-desc mt-2">格式：http://ip:port，例如 http://192.168.2.46:18181。</div>
              </v-col>
              <v-col cols="12" md="6">
                <div class="field-label">API Token</div>
                <v-text-field v-model="connection.subtitle_backend_token" placeholder="内网可留空" />
                <div class="provider-desc mt-2">只有 Windows Worker 设置了 Token 时才需要填写。</div>
              </v-col>
            </v-row>

            <v-row class="mt-4">
              <v-col cols="12" md="4">
                <div class="field-label">Whisper 模型</div>
                <v-text-field v-model="settings.whisper_model" />
              </v-col>
              <v-col cols="12" md="4">
                <div class="field-label">设备</div>
                <v-select v-model="settings.whisper_device" :items="['cuda', 'cpu']" />
              </v-col>
              <v-col cols="12" md="4">
                <div class="field-label">计算类型</div>
                <v-select v-model="settings.whisper_compute_type" :items="['float16', 'int8_float16', 'int8', 'float32']" />
              </v-col>
              <v-col cols="12" md="4">
                <div class="field-label">并发数</div>
                <v-text-field v-model.number="settings.subtitle_max_workers" type="number" min="1" max="4" />
              </v-col>
              <v-col cols="12" md="8">
                <div class="field-label">模型目录</div>
                <v-text-field v-model="settings.whisper_model_dir" placeholder="可留空，默认 data\\local-backend\\whisper-models" />
              </v-col>
              <v-col cols="12" md="6">
                <div class="field-label">默认输出目录</div>
                <v-text-field v-model="settings.subtitle_output_dir" placeholder="留空写到视频同目录" />
              </v-col>
              <v-col cols="12" md="6">
                <div class="field-label">终端 API Token</div>
                <v-text-field v-model="settings.subtitle_api_token" placeholder="内网可留空" />
              </v-col>
              <v-col cols="12">
                <div class="field-label">终端兜底路径映射</div>
                <v-textarea v-model="settings.subtitle_path_map" rows="2" placeholder="/media=\\\\192.168.2.9\\media" />
                <div class="provider-desc mt-2">扫描页提交的是容器路径，例如 /media/study3/a.mp4；这里要映射成 Windows 可打开的共享路径。</div>
              </v-col>
            </v-row>

            <v-row class="mt-4">
              <v-col cols="12" md="4">
                <div class="stat-card">
                  <div class="stat-label">CPU</div>
                  <div class="stat-value">{{ backendStatus.hardware?.cpu || '未连接' }}</div>
                </div>
              </v-col>
              <v-col cols="12" md="4">
                <div class="stat-card">
                  <div class="stat-label">内存</div>
                  <div class="stat-value">{{ memoryLabel }}</div>
                </div>
              </v-col>
              <v-col cols="12" md="4">
                <div class="stat-card">
                  <div class="stat-label">显卡</div>
                  <div class="stat-value">{{ gpuLabel }}</div>
                </div>
              </v-col>
            </v-row>

            <div class="connection-result mt-4">{{ connectionMessage }}</div>
          </v-card-text>

          <v-divider />
          <v-card-actions class="pa-6 justify-end ga-3">
            <v-btn variant="outlined" class="px-6" @click="testBackend">测试联通</v-btn>
            <v-btn color="primary" class="px-6" @click="saveComputeAll">保存设置</v-btn>
          </v-card-actions>
        </v-card>
      </v-dialog>

      <v-dialog v-model="translateDialog" max-width="1040" scrim="rgba(32,31,40,.42)">
        <v-card class="config-dialog" elevation="12">
          <div class="dialog-head">
            <div>
              <div class="dialog-kicker">配置</div>
              <div class="dialog-title">翻译后端</div>
            </div>
            <v-btn icon="mdi-close" variant="text" size="large" color="#6e6f7c" @click="translateDialog = false" />
          </div>

          <v-card-text class="pa-6">
            <div class="field-label">默认翻译后端</div>
            <div class="provider-grid">
                <v-card
                  v-for="item in providerCards"
                  :key="item.value"
                  class="provider-card pa-4"
                  :class="{ selected: settings.default_translate_backend === item.value }"
                  elevation="0"
                  @click="settings.default_translate_backend = item.value"
                >
                  <div class="provider-select">
                    <v-radio
                      class="provider-radio"
                      :model-value="settings.default_translate_backend"
                      :value="item.value"
                      color="primary"
                      density="compact"
                      hide-details
                    />
                  </div>
                  <div class="provider-copy">
                    <div class="provider-title">{{ item.name }}</div>
                    <div class="provider-desc">{{ item.desc }}</div>
                    <div
                      v-if="translateTests[item.value]?.message"
                      class="provider-test-result"
                      :class="{ ok: translateTests[item.value]?.ok, bad: translateTests[item.value]?.ok === false }"
                    >
                      {{ translateTests[item.value].message }}
                    </div>
                  </div>
                  <v-btn
                    class="provider-test-button"
                    size="small"
                    variant="outlined"
                    :loading="translateTests[item.value]?.loading"
                    @click.stop="testTranslate(item.value)"
                  >
                    测试
                  </v-btn>
                </v-card>
            </div>

            <v-row class="mt-5">
              <v-col v-for="field in activeProviderFields" :key="field.key" cols="12" md="6">
                <div class="field-label">{{ field.label }}</div>
                <v-text-field v-model="settings[field.key]" :placeholder="field.placeholder" />
                <div v-if="field.hint" class="provider-desc mt-2">{{ field.hint }}</div>
              </v-col>
            </v-row>
          </v-card-text>

          <v-divider />
          <v-card-actions class="pa-6 justify-end ga-3">
            <v-btn variant="outlined" class="px-6" @click="translateDialog = false">取消</v-btn>
            <v-btn color="primary" class="px-6" @click="saveSettings">保存设置</v-btn>
          </v-card-actions>
        </v-card>
      </v-dialog>

      <v-snackbar v-model="snackbar.show" color="primary" timeout="3600">
        {{ snackbar.message }}
      </v-snackbar>
    </v-main>
  </v-app>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'

const computeDialog = ref(false)
const translateDialog = ref(false)
const taskTab = ref('current')
const taskStatusTab = ref('running')
const statusPage = ref(1)
const historyPage = ref(1)
const PAGE_SIZE = 20
const jobs = ref([])
const backendStatus = ref({})
const computeEnabled = ref(false)
const retryingFailed = ref(false)
const retryingSelected = ref(false)
const retryingJob = reactive({})
const translateTests = reactive({})
const selectedIds = reactive(new Set())
const connectionMessage = ref('')
const snackbar = reactive({ show: false, message: '' })

const connection = reactive({
  subtitle_backend_url: '',
  subtitle_backend_token: ''
})

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
  ollama_url: '',
  ollama_model: 'qwen2.5:7b'
})

const providerCards = [
  { name: 'Google 免费翻译', value: 'google', desc: '默认优先 · 无需 API Key', logo: 'G' },
  { name: 'DeepL API', value: 'deepl', desc: 'api-free.deepl.com', logo: 'DL' },
  { name: 'DeepSeek API', value: 'deepseek', desc: 'Base URL · API Key · 模型', logo: 'DS' },
  { name: '本地 Ollama', value: 'ollama', desc: 'OLLAMA_URL · 本地模型', logo: 'OL' }
]

const providerFields = {
  google: [
    {
      key: 'google_translate_url',
      label: 'Google 免费接口',
      placeholder: 'https://translate.google.com/translate_a/single',
      hint: '默认可用，不需要 Key。'
    }
  ],
  deepl: [
    { key: 'deepl_api_key', label: 'DeepL API Key', placeholder: 'DeepL auth key' },
    { key: 'deepl_api_url', label: 'DeepL API URL', placeholder: 'https://api-free.deepl.com/v2/translate' }
  ],
  deepseek: [
    { key: 'openai_base_url', label: 'DeepSeek API Base URL', placeholder: 'https://api.deepseek.com' },
    { key: 'openai_api_key', label: 'DeepSeek API Key', placeholder: 'sk-...' },
    { key: 'openai_model', label: 'DeepSeek 模型', placeholder: 'deepseek-chat' }
  ],
  ollama: [
    { key: 'ollama_url', label: 'Ollama URL', placeholder: 'http://127.0.0.1:11434' },
    { key: 'ollama_model', label: 'Ollama 模型', placeholder: 'qwen2.5:7b' }
  ]
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

const adaptedJobs = computed(() => jobs.value.map(adaptJob))
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
const activeStatusJobs = computed(() => statusTabs.value.find((state) => state.key === taskStatusTab.value)?.items || [])
const statusPageCount = computed(() => Math.max(1, Math.ceil(activeStatusJobs.value.length / PAGE_SIZE)))
const historyPageCount = computed(() => Math.max(1, Math.ceil(historyJobs.value.length / PAGE_SIZE)))
const pagedStatusJobs = computed(() => activeStatusJobs.value.slice((statusPage.value - 1) * PAGE_SIZE, statusPage.value * PAGE_SIZE))
const pagedHistoryJobs = computed(() => historyJobs.value.slice((historyPage.value - 1) * PAGE_SIZE, historyPage.value * PAGE_SIZE))

const visibleJobs = computed(() => (taskTab.value === 'history' ? pagedHistoryJobs.value : pagedStatusJobs.value))
const selectedJobs = computed(() => adaptedJobs.value.filter((job) => selectedIds.has(job.id)))
const allVisibleSelected = computed(() => visibleJobs.value.length > 0 && visibleJobs.value.every((job) => selectedIds.has(job.id)))
const whisperSummary = computed(() => `${settings.whisper_model || 'large-v3'} / ${settings.whisper_device || 'cuda'} / ${settings.whisper_compute_type || 'float16'}`)
const memoryLabel = computed(() => {
  const memory = backendStatus.value.hardware?.memory
  return memory ? `${memory.label || ''} · ${memory.used_percent || 0}%` : '未连接'
})
const gpuLabel = computed(() => backendStatus.value.hardware?.gpus?.[0]?.label || '未检测')
const submitNotice = computed(() => {
  const params = new URLSearchParams(window.location.search)
  const submitted = Number(params.get('submitted') || 0)
  const failed = Number(params.get('failed') || 0)
  if (!submitted && !failed) return ''
  return `已提交 ${submitted} 个字幕任务${failed ? `，${failed} 个失败，请检查路径映射和算力端日志。` : '。'}`
})

function adaptJob(job) {
  const path = String(job.video_path || '')
  const normalized = path.replaceAll('\\', '/')
  const title = normalized.split('/').filter(Boolean).pop() || path || '未命名任务'
  const statusKey = String(job.status || 'queued')
  return {
    raw: job,
    id: job.id,
    title,
    path,
    statusKey,
    statusLabel: statusLabel(statusKey),
    percent: Math.round((Number(job.progress || 0)) * 100),
    step: job.error ? `${job.message || '任务失败'}：${job.error}` : (job.message || '等待处理'),
    createdLabel: formatTime(job.created_at),
    updatedAt: job.updated_at,
    finishedAt: job.finished_at,
    modelLabel: `${job.model || 'large-v3'} / ${job.source_language || 'auto'} => ${job.target_language || 'zh'}`,
    canRetry: ['failed', 'completed'].includes(statusKey),
    originalSrt: job.original_srt,
    resultSrt: job.translated_srt || job.bilingual_srt
  }
}

function statusLabel(status) {
  const labels = {
    queued: '等待中',
    running: '运行中',
    translating: '翻译中',
    failed: '失败',
    completed: '已完成'
  }
  return labels[status] || status
}

function progressColor(status) {
  if (status === 'failed') return 'error'
  if (status === 'completed') return 'success'
  if (status === 'translating') return 'secondary'
  return 'primary'
}

function formatTime(value) {
  if (!value) return '未知时间'
  const date = new Date(Number(value) * 1000)
  if (Number.isNaN(date.getTime())) return '未知时间'
  return date.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit'
  })
}

async function api(url, options = {}) {
  const response = await fetch(url, {
    headers: {
      'Content-Type': 'application/json',
      ...(options.headers || {})
    },
    ...options
  })
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new Error(payload.detail || payload.error || '请求失败')
  }
  return payload
}

async function loadConsole() {
  const payload = await api('/api/subtitle/console')
  Object.assign(connection, payload.connection || {})
  computeEnabled.value = !!connection.subtitle_backend_url
  Object.assign(settings, payload.compute_settings || {})
  backendStatus.value = payload.backend_status || {}
  jobs.value = payload.jobs || []
}

async function refreshJobs() {
  const payload = await api('/api/subtitle/jobs?limit=0')
  jobs.value = payload.jobs || []
  await refreshBackendStatus()
}

async function refreshBackendStatus() {
  backendStatus.value = await api('/api/subtitle/backend/status')
}

async function testBackend() {
  connectionMessage.value = '正在测试连接...'
  const body = new FormData()
  body.set('subtitle_backend_url', connection.subtitle_backend_url || '')
  body.set('subtitle_backend_token', connection.subtitle_backend_token || '')
  try {
    const response = await fetch('/api/subtitle/backend/test', { method: 'POST', body })
    const payload = await response.json().catch(() => ({}))
    if (!response.ok) {
      throw new Error(payload.detail || '连接失败')
    }
    backendStatus.value = payload
    connectionMessage.value = '连接成功，可以保存这个地址。'
  } catch (error) {
    connectionMessage.value = error.message || String(error)
  }
}

async function saveConnection() {
  const payload = {
    subtitle_backend_url: computeEnabled.value ? connection.subtitle_backend_url : '',
    subtitle_backend_token: computeEnabled.value ? connection.subtitle_backend_token : ''
  }
  await api('/api/subtitle/connection', {
    method: 'POST',
    body: JSON.stringify(payload)
  })
  Object.assign(connection, payload)
}

async function saveSettings() {
  const payload = await api('/api/subtitle/settings', {
    method: 'POST',
    body: JSON.stringify(settings)
  })
  Object.assign(settings, payload.settings || {})
  translateDialog.value = false
  await refreshBackendStatus()
}

async function saveComputeAll() {
  await saveConnection()
  await saveSettings()
  computeDialog.value = false
  await loadConsole()
}

function openTranslate(value) {
  settings.default_translate_backend = value
  translateDialog.value = true
}

async function testTranslate(backend) {
  translateTests[backend] = {
    loading: true,
    ok: null,
    message: '正在发送测试文本...'
  }
  try {
    const payload = await api('/api/subtitle/translate/test', {
      method: 'POST',
      body: JSON.stringify({
        backend,
        text: 'クッションがいっぱいある、かわいい',
        source_language: 'ja',
        target_language: 'zh',
        settings
      })
    })
    translateTests[backend] = {
      loading: false,
      ok: true,
      message: `可用：${payload.translated_text || ''}`
    }
  } catch (error) {
    translateTests[backend] = {
      loading: false,
      ok: false,
      message: `不可用：${error.message || error}`
    }
  }
}

function toggleJob(id) {
  if (selectedIds.has(id)) {
    selectedIds.delete(id)
  } else {
    selectedIds.add(id)
  }
}

function toggleSelectVisible() {
  if (allVisibleSelected.value) {
    visibleJobs.value.forEach((job) => selectedIds.delete(job.id))
  } else {
    visibleJobs.value.forEach((job) => selectedIds.add(job.id))
  }
}

async function retryJob(jobId) {
  retryingJob[jobId] = true
  try {
    await api(`/api/subtitle/jobs/${jobId}/retry`, {
      method: 'POST',
      body: JSON.stringify({})
    })
    await refreshJobs()
  } finally {
    retryingJob[jobId] = false
  }
}

async function retrySelected() {
  const retryable = selectedJobs.value.filter((job) => job.canRetry)
  if (!retryable.length) {
    showSnack('选中的任务里没有可重试任务。')
    return
  }
  retryingSelected.value = true
  try {
    for (const job of retryable) {
      await api(`/api/subtitle/jobs/${job.id}/retry`, {
        method: 'POST',
        body: JSON.stringify({})
      })
    }
    showSnack(`已提交 ${retryable.length} 个重试任务。`)
    await refreshJobs()
  } finally {
    retryingSelected.value = false
  }
}

async function retryFailed() {
  retryingFailed.value = true
  try {
    await api('/api/subtitle/jobs/retry-failed', {
      method: 'POST',
      body: JSON.stringify({})
    })
    await refreshJobs()
  } finally {
    retryingFailed.value = false
  }
}

function unsupportedAction(name) {
  showSnack(`${name} 需要后端提供删除/取消接口；当前版本先保留入口，不会误操作任务。`)
}

function showSnack(message) {
  snackbar.message = message
  snackbar.show = true
}

watch(taskStatusTab, () => {
  statusPage.value = 1
})

watch(taskTab, () => {
  historyPage.value = 1
  statusPage.value = 1
})

onMounted(async () => {
  await loadConsole()
  window.setInterval(refreshJobs, 4000)
})
</script>

<style>
:root {
  color: #111827;
  font-family: Inter, "Segoe UI", system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
}

body {
  margin: 0;
  background: #f6f7fc;
}

.app-shell {
  min-height: 100vh;
  padding: 20px;
  display: grid;
  grid-template-columns: 260px minmax(0, 1fr);
  gap: 20px;
  background:
    radial-gradient(circle at top left, rgba(193, 236, 231, 0.75), transparent 360px),
    linear-gradient(90deg, #eef8f6 0, #f7fafc 280px, #f8fafc 100%);
}

.side-shell,
.main-panel {
  background: rgba(255, 255, 255, 0.86);
  border: 1px solid rgba(213, 226, 236, 0.85);
  box-shadow: 0 24px 60px rgba(25, 46, 68, 0.08);
}

.side-shell {
  position: sticky;
  top: 20px;
  height: calc(100vh - 40px);
  border-radius: 22px;
  padding: 24px;
}

.brand {
  display: flex;
  gap: 14px;
  align-items: center;
}

.brand-mark {
  width: 44px;
  height: 44px;
  border-radius: 12px;
  display: grid;
  place-items: center;
  background: linear-gradient(135deg, #0f8f83, #17b7a7);
  color: white;
  font-weight: 800;
  font-size: 22px;
}

.brand h1 {
  margin: 0;
  font-size: 22px;
  line-height: 1.1;
}

.brand p,
.topbar p,
.task-console-head p,
.side-card p {
  margin: 4px 0 0;
  color: #6b768f;
}

.nav-list {
  display: grid;
  gap: 10px;
  margin-top: 42px;
}

.nav-list a {
  color: #334155;
  text-decoration: none;
  font-weight: 750;
  padding: 15px 16px;
  border-radius: 14px;
  transition: 0.18s ease;
}

.nav-list a:hover,
.nav-list a.active {
  color: #087e74;
  background: #e8fbf4;
}

.side-card {
  margin-top: 36px;
  border: 1px solid #e1eaf2;
  border-radius: 18px;
  padding: 16px;
  background: #fbfdff;
}

.side-card-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.side-card p {
  font-size: 13px;
}

.main-panel {
  border-radius: 22px;
  padding: 26px;
  min-width: 0;
}

.topbar {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 20px;
}

.topbar h2 {
  margin: 0;
  font-size: 34px;
  letter-spacing: 0;
}

.topbar-actions,
.bulk-actions,
.queue-actions {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  justify-content: flex-end;
}

.status-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
  margin: 20px 0 16px;
}

.service-grid {
  grid-template-columns: repeat(2, minmax(250px, 320px));
}

.status-card {
  position: relative;
  min-width: 0;
  text-align: left;
  border: 1px solid #dbe7f1;
  border-radius: 16px;
  padding: 14px 16px;
  background: linear-gradient(135deg, #ffffff, #f6fafc);
  color: inherit;
  transition: 0.18s ease;
}

button.status-card {
  cursor: pointer;
}

.status-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 14px 30px rgba(27, 57, 86, 0.08);
}

.status-card span,
.status-card em {
  display: block;
  color: #6b768f;
  font-size: 12px;
  font-style: normal;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.status-card strong {
  display: block;
  margin: 5px 0 4px;
  font-size: 20px;
}

.status-card em.on {
  color: #0f8f83;
  font-weight: 700;
}

.service-dot {
  position: absolute;
  top: 50%;
  right: 18px;
  width: 12px;
  height: 12px;
  transform: translateY(-50%);
  border-radius: 50%;
  background: #cbd5e1;
  box-shadow: 0 0 0 6px rgba(203, 213, 225, 0.18);
}

.service-dot.online {
  background: #20bc63;
  box-shadow: 0 0 0 6px rgba(32, 188, 99, 0.13);
}

.submit-notice {
  margin-bottom: 14px;
}

.task-console {
  border: 1px solid #dce8f2;
  border-radius: 22px;
  padding: 18px;
}

.task-console-head,
.queue-head,
.history-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
}

.task-console-head {
  display: block;
  padding-bottom: 16px;
}

.task-metrics {
  display: flex;
  gap: 10px;
  margin-top: 14px;
}

.task-metric {
  min-width: 150px;
  padding: 9px 13px;
  border: 1px solid #e2eaf2;
  border-radius: 13px;
  background: #f8fbfc;
}

.task-metric span,
.task-metric em {
  display: block;
  color: #6b768f;
  font-size: 12px;
  font-style: normal;
  white-space: nowrap;
}

.task-metric strong {
  display: block;
  margin: 2px 0;
  color: #111827;
  font-size: 22px;
  line-height: 1.2;
}

.task-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
  padding: 10px 0;
  border-top: 1px solid #e5edf4;
  border-bottom: 1px solid #e5edf4;
}

.task-actionbar {
  min-height: 46px;
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 12px;
}

.selection-count {
  color: #0f766e;
  font-size: 13px;
  font-weight: 700;
}

.task-console h3 {
  margin: 0;
  font-size: 24px;
}

.task-tabs {
  flex: none;
  height: 46px;
  padding: 4px;
  border: 1px solid #dbe6ef;
  border-radius: 12px;
  background: #f5f9fb;
}

.task-tabs .v-tab {
  min-width: 112px;
  min-height: 38px;
  border-radius: 9px;
  font-weight: 700;
  text-transform: none;
}

.task-tabs .v-tab--selected {
  color: #087e74;
  background: #e8fbf4;
  box-shadow: inset 0 0 0 1px rgba(15, 143, 131, 0.12);
}

.task-tabs .v-tab__slider {
  display: none;
}

.state-tabs {
  margin: 14px 0;
  padding: 4px;
  border: 1px solid #e2eaf2;
  border-radius: 14px;
  background: #f7fafc;
}

.state-tabs .v-tab {
  min-width: 128px;
  border-radius: 10px;
  text-transform: none;
}

.state-tabs .v-tab--selected {
  background: #fff;
  box-shadow: 0 2px 8px rgba(27, 57, 86, 0.06);
}

.state-tabs .state-dot {
  margin-right: 8px;
}

.tab-count {
  margin-left: 9px;
  min-width: 28px;
  height: 23px;
  padding: 0 7px;
  display: inline-grid;
  place-items: center;
  border-radius: 999px;
  background: #eaf3f4;
  color: #0f766e;
  font-style: normal;
  font-size: 12px;
  font-weight: 800;
}

.task-groups {
  display: grid;
  gap: 10px;
}

.task-group {
  border: 1px solid #e1eaf2;
  border-radius: 16px !important;
  overflow: hidden;
  box-shadow: none !important;
}

.group-title {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
}

.group-title em {
  margin-left: auto;
  min-width: 32px;
  height: 26px;
  display: inline-grid;
  place-items: center;
  border-radius: 999px;
  background: #edf5f7;
  color: #0f766e;
  font-style: normal;
  font-weight: 800;
}

.state-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: #94a3b8;
}

.state-dot.running,
.state-dot.translating {
  background: #0f8f83;
}

.state-dot.waiting {
  background: #3b82f6;
}

.state-dot.failed {
  background: #ef4444;
}

.state-dot.completed {
  background: #22c55e;
}

.task-card-list {
  display: grid;
  gap: 10px;
}

.task-card {
  display: grid;
  grid-template-columns: 34px minmax(0, 1fr) auto;
  gap: 12px;
  align-items: center;
  padding: 13px 14px;
  border: 1px solid #e2eaf2;
  border-radius: 16px;
  background: #fff;
  transition: 0.18s ease;
}

.task-card:hover {
  border-color: #b9dcd7;
  transform: translateY(-1px);
  box-shadow: 0 12px 26px rgba(28, 68, 88, 0.07);
}

.task-card.compact {
  grid-template-columns: 34px minmax(0, 1fr) auto;
}

.task-check {
  align-self: start;
}

.task-main {
  min-width: 0;
}

.task-title-line {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.task-title-line strong {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 15px;
}

.task-main p {
  margin: 4px 0 6px;
  color: #64748b;
  font-size: 13px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.task-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 14px;
  color: #718096;
  font-size: 12px;
}

.task-progress {
  margin-top: 8px;
}

.task-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  color: #64748b;
}

.status-pill {
  flex: none;
  padding: 4px 9px;
  border-radius: 999px;
  background: #edf5f7;
  color: #475569;
  font-size: 12px;
  font-weight: 750;
}

.status-pill.running,
.status-pill.translating {
  background: #e6fbf5;
  color: #087e74;
}

.status-pill.queued {
  background: #eff6ff;
  color: #2563eb;
}

.status-pill.failed {
  background: #ffe8ee;
  color: #d81749;
}

.status-pill.completed {
  background: #e9fbea;
  color: #16803a;
}

.history-toolbar {
  margin: 6px 0 14px;
  color: #64748b;
}

.task-pagination {
  margin-top: 16px;
}

.empty-line {
  padding: 18px;
  border: 1px dashed #cbd9e5;
  border-radius: 14px;
  color: #718096;
  background: #fbfdff;
}

.config-dialog {
  border-radius: 16px !important;
}

.dialog-head {
  min-height: 78px;
  padding: 0 24px;
  border-bottom: 1px solid #e7e9f1;
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.dialog-kicker,
.field-label,
.stat-label {
  color: #667085;
  font-size: 14px;
  font-weight: 800;
}

.dialog-title {
  font-size: 24px;
  font-weight: 800;
}

.toggle-title {
  font-size: 18px;
  color: #111827;
}

.provider-grid {
  margin-top: 8px;
  display: grid;
  grid-template-columns: repeat(2, minmax(360px, 1fr));
  gap: 12px;
}

.provider-card {
  min-width: 0;
  min-height: 92px;
  border: 1px solid #dfe7ef;
  border-radius: 12px;
  background: #fbfcff;
  cursor: pointer;
  display: grid;
  grid-template-columns: 28px minmax(0, 1fr) auto;
  align-items: center;
  column-gap: 10px;
}

.provider-card.selected {
  border-color: #85d7ca;
  background: #effcf8;
}

.provider-copy {
  min-width: 0;
}

.provider-radio {
  align-self: center;
}

.provider-select {
  display: flex;
  align-items: center;
}

.provider-title {
  font-size: 17px;
  font-weight: 800;
  white-space: normal;
  overflow-wrap: break-word;
}

.provider-desc,
.provider-test-result {
  color: #737b8f;
  font-size: 13px;
  line-height: 1.45;
}

.provider-test-result {
  margin-top: 6px;
}

.provider-test-result.ok {
  color: #0f8f83;
}

.provider-test-result.bad {
  color: #d81749;
}

.provider-test-button {
  align-self: center;
}

.stat-card {
  min-height: 98px;
  border-radius: 14px;
  background: #f7f9fc;
  padding: 16px;
}

.stat-value {
  margin-top: 8px;
  font-weight: 800;
  line-height: 1.35;
}

.connection-result {
  color: #64748b;
}

.task-list-enter-active,
.task-list-leave-active {
  transition: all 0.18s ease;
}

.task-list-enter-from,
.task-list-leave-to {
  opacity: 0;
  transform: translateY(6px);
}

@media (max-width: 1180px) {
  .app-shell {
    grid-template-columns: 1fr;
  }

  .side-shell {
    position: static;
    height: auto;
  }

  .status-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .service-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .provider-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 760px) {
  .main-panel,
  .side-shell {
    padding: 16px;
  }

  .topbar,
  .task-console-head,
  .history-toolbar {
    display: grid;
  }

  .status-grid {
    grid-template-columns: 1fr;
  }

  .task-metrics {
    width: 100%;
  }

  .task-metric {
    min-width: 0;
    flex: 1;
  }

  .task-actionbar {
    align-items: flex-start;
    justify-content: flex-start;
    flex-direction: column;
  }

  .task-toolbar {
    align-items: flex-start;
    flex-direction: column;
  }

  .state-tabs .v-slide-group__content {
    overflow-x: auto;
  }

  .task-card {
    grid-template-columns: 28px minmax(0, 1fr);
  }

  .task-actions {
    grid-column: 2;
    justify-content: flex-start;
    flex-wrap: wrap;
  }
}
</style>
