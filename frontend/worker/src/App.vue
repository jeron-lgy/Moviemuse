<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import {
  Activity, AlertCircle, Box, CheckCircle2, CircleMinus, Copy, Download, ExternalLink,
  Cpu, FileText, FolderOpen, HardDrive, Home, Info, LockKeyhole,
  KeyRound, Pause, Play, Power, PowerOff, RefreshCw, ShieldCheck, Trash2, Video, X
} from '@lucide/vue'
import { demoModels, demoStatus } from './demo'
import logoUrl from './assets/moviemuse-worker-logo.png'

// Demo fixtures are available only under the Vite development server. Production
// Worker builds always use the real local API, even if a demo query is supplied.
const demo = import.meta.env.DEV && new URLSearchParams(location.search).get('demo') === '1'
const page = ref(location.hash.replace('#/', '') || 'overview')
const status = ref(demo ? demoStatus : null)
const modelData = ref(demo ? demoModels : null)
const loading = ref(!demo)
const connected = ref(demo)
const notice = ref('')
const actionKey = ref('')
const runtimeBusy = ref(false)
const versionBusy = ref(false)
const readinessBusy = ref(false)
const filter = ref('all')
const expanded = ref(demo ? 'a6' : '')
let timer

const nav = [
  { id: 'overview', label: '概览', icon: Home },
  { id: 'activity', label: '活动', icon: Activity },
  { id: 'models', label: '模型', icon: Box }
]

const completed = new Set(['completed', 'done', 'success'])
const failed = new Set(['failed', 'error', 'cancelled'])
const waiting = new Set(['queued', 'waiting'])
const running = new Set(['running', 'translating'])

function go(next) {
  page.value = next
  location.hash = `/${next}`
  if (next === 'models') checkVersions(false, true)
}

async function request(url, options) {
  const response = await fetch(url, { headers: { 'Content-Type': 'application/json' }, ...options })
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(body.detail || `请求失败 (${response.status})`)
  }
  return response.json()
}

async function refresh() {
  if (demo) return
  try {
    const [nextStatus, nextModels] = await Promise.all([
      request('/api/worker/status'), request('/api/worker/models')
    ])
    status.value = nextStatus
    modelData.value = nextModels
    connected.value = true
  } catch (error) {
    connected.value = false
    notice.value = error.message
  } finally {
    loading.value = false
  }
}

async function modelAction(model, action) {
  if (demo) {
    notice.value = '演示模式：操作不会修改本机文件'
    return
  }
  const key = `${model.id}:${action}`
  if (actionKey.value) return
  try {
    if (action === 'update' && !confirm(`确定重新下载并更新 ${model.label} 吗？更新完成前不会替换现有模型。`)) return
    if (action === 'remove' && !confirm(`确定删除未启用模型 ${model.label} 的本机文件吗？`)) return
    actionKey.value = key
    if (action === 'download') await request(`/api/worker/models/${model.id}/download`, { method: 'POST' })
    if (action === 'update') await request(`/api/worker/models/${model.id}/update`, { method: 'POST' })
    if (action === 'verify') await request(`/api/worker/models/${model.id}/verify`, { method: 'POST' })
    if (action === 'open') await request(`/api/worker/models/${model.id}/open-folder`, { method: 'POST' })
    if (action === 'remove') await request(`/api/worker/models/${model.id}`, { method: 'DELETE' })
    notice.value = action === 'download' ? '模型下载或修复已开始' : action === 'update' ? '模型更新已开始' : '操作完成'
    await refresh()
  } catch (error) { notice.value = error.message }
  finally { actionKey.value = '' }
}

async function downloadAction(action) {
  const job = activeDownload.value
  const target = job || visibleDownload.value
  if (!target) return
  if (demo) { notice.value = '演示模式：下载控制未实际执行'; return }
  if (action === 'cancel' && !confirm('确定取消下载并清理尚未完成的临时文件吗？')) return
  try {
    await request(`/api/worker/model-downloads/${target.id}/${action}`, { method: 'POST' })
    notice.value = action === 'pause' ? '正在暂停下载' : action === 'resume' ? '下载已继续' : '正在取消下载'
    await refresh()
  } catch (error) { notice.value = error.message }
}

async function copyText(value, success = '已复制') {
  try {
    await navigator.clipboard.writeText(String(value || ''))
    notice.value = success
  } catch (error) {
    notice.value = `复制失败：${error.message}`
  }
}

async function copyDiagnostics() {
  try {
    const data = demo ? status.value : await request('/api/worker/diagnostics')
    await copyText(JSON.stringify(data, null, 2), '诊断信息已复制')
  } catch (error) { notice.value = `复制失败：${error.message}` }
}

async function checkVersions(force = true, silent = false) {
  if (demo) {
    if (!silent) notice.value = '演示模式：版本信息已刷新'
    return
  }
  if (versionBusy.value) return
  versionBusy.value = true
  try {
    const suffix = `?force=${force ? 'true' : 'false'}`
    const [models, software] = await Promise.all([
      request(`/api/worker/models/check-updates${suffix}`, { method: 'POST' }),
      request(`/api/worker/software-update/check${suffix}`, { method: 'POST' })
    ])
    modelData.value = { ...modelData.value, models: models.models || modelData.value?.models || [] }
    status.value = { ...status.value, software_update: software }
    if (!silent) notice.value = '模型和 Worker 软件版本检查完成'
  } catch (error) {
    if (!silent) notice.value = error.message
  } finally {
    versionBusy.value = false
  }
}

const activities = computed(() => status.value?.activities || [])
const computeEnabled = computed(() => status.value?.compute_enabled !== false)
const softwareUpdate = computed(() => status.value?.software_update || {})
const gpuRuntime = computed(() => status.value?.gpu_runtime || {})
const modelRecommendation = computed(() => modelData.value?.recommendation || status.value?.model_recommendation || {})
const readiness = computed(() => status.value?.readiness || {})
const pairing = computed(() => status.value?.pairing || {})
const readinessChecks = computed(() => readiness.value?.checks || [])
const currentJobs = computed(() => activities.value.filter(item => running.has(item.status) || waiting.has(item.status)).slice(0, 3))
const activeDownload = computed(() => (modelData.value?.downloads || []).find(item => !['completed', 'failed', 'cancelled'].includes(item.state)))
const visibleDownload = computed(() => {
  if (activeDownload.value) return activeDownload.value
  const latest = modelData.value?.downloads?.[0]
  return latest?.state === 'failed' ? latest : null
})

async function toggleRuntime() {
  const enable = !computeEnabled.value
  if (demo) {
    status.value = { ...status.value, compute_enabled: enable, runtime_changed_at: Date.now() / 1000 }
    notice.value = enable ? '算力已启动，可以接收新任务' : '算力已关闭，不再接收新任务'
    return
  }
  if (runtimeBusy.value || !connected.value) return
  runtimeBusy.value = true
  try {
    const result = await request(`/api/worker/runtime/${enable ? 'start' : 'stop'}`, { method: 'POST' })
    status.value = { ...status.value, ...result }
    notice.value = enable
      ? '算力已启动，可以接收新任务'
      : '算力已关闭；当前任务继续完成，新任务将被拒绝'
    await refresh()
  } catch (error) {
    notice.value = error.message
  } finally {
    runtimeBusy.value = false
  }
}

async function installGpuRuntime() {
  if (demo) { notice.value = '演示模式：不会下载 GPU 运行环境'; return }
  if (gpuRuntime.value.status === 'installing') return
  const downloadSize = bytes(gpuRuntime.value.estimated_download_bytes || 1240400000)
  if (!confirm(`将下载约 ${downloadSize} 的 NVIDIA GPU 运行环境，安装后需要重启 MovieMuse Worker。是否继续？`)) return
  try {
    const result = await request('/api/worker/gpu-runtime/install', { method: 'POST' })
    status.value = { ...status.value, gpu_runtime: result }
    notice.value = 'GPU 运行环境已开始下载，可以留在此页面查看状态'
  } catch (error) {
    notice.value = error.message
  }
}

async function runReadiness(silent = false) {
  if (demo) {
    if (!silent) notice.value = '演示模式：自动体检已刷新'
    return
  }
  if (readinessBusy.value || !connected.value) return
  readinessBusy.value = true
  try {
    const result = await request('/api/worker/readiness/scan', { method: 'POST' })
    status.value = { ...status.value, readiness: result }
    if (!silent) notice.value = result.summary || '自动体检完成'
  } catch (error) {
    if (!silent) notice.value = error.message
  } finally {
    readinessBusy.value = false
  }
}

async function runReadinessAction() {
  const action = readiness.value?.next_action?.id
  if (!action || action === 'scan' || action === 'none') return runReadiness()
  if (action === 'start') {
    await toggleRuntime()
    return runReadiness(true)
  }
  if (action === 'gpu_runtime') return installGpuRuntime()
  if (action === 'models') {
    const recommended = (modelData.value?.models || []).find(item => item.id === modelRecommendation.value?.recommended_model)
    if (recommended && !recommended.installed) return modelAction(recommended, 'download')
    return go('models')
  }
  if (action === 'controller') notice.value = '请在 MovieMuse 控制端检查算力端地址与路径映射，然后重新保存。'
  else if (action === 'driver') notice.value = '请安装或更新 NVIDIA 驱动，完成后重新体检。'
  else if (action === 'reinstall') notice.value = '当前 Worker 组件不完整，请使用新版安装包覆盖安装。'
}

function readinessStatusText(value) {
  return ({ pass: '正常', warning: '需确认', fail: '未通过' })[value] || '待检测'
}

function gpuRuntimeSource(source) {
  return ({ local: 'Worker 本地运行库', bundled: '安装包内置运行库', system: 'Windows 系统运行库', mixed: '可用运行库组合' })[source] || '尚未安装'
}

function gpuRuntimeTitle(runtime) {
  if (runtime.status === 'ready') return 'GPU 运行环境已就绪'
  if (runtime.status === 'installing') return '正在安装 GPU 运行环境'
  if (runtime.status === 'installed_restart_required') return '安装完成，需要重启'
  if (runtime.status === 'failed') return 'GPU 运行环境安装失败'
  return '需要安装 GPU 运行环境'
}

function gpuRuntimeMessage(runtime) {
  if (runtime.status === 'ready') return `${gpuRuntimeSource(runtime.source)} · CUDA 12 / cuDNN 9 可用`
  if (runtime.status === 'installing') return runtime.job?.message || '正在下载 cuBLAS 与 cuDNN'
  if (runtime.status === 'installed_restart_required') return '请关闭当前窗口，再重新打开 MovieMuseWorker.exe'
  if (runtime.status === 'failed') return runtime.job?.error || '请检查网络后重新尝试'
  return `核心安装包不包含大型 NVIDIA 运行库，首次使用 GPU 识别前需下载约 ${bytes(runtime.estimated_download_bytes || 1240400000)}`
}
const filteredActivities = computed(() => activities.value.filter(item => {
  if (filter.value === 'all') return true
  if (filter.value === 'running') return running.has(item.status)
  if (filter.value === 'waiting') return waiting.has(item.status)
  if (filter.value === 'completed') return completed.has(item.status)
  if (filter.value === 'failed') return failed.has(item.status)
  return true
}))

function bytes(value, decimals = 1) {
  const n = Number(value || 0)
  if (!n) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  const unit = Math.min(Math.floor(Math.log(n) / Math.log(1024)), units.length - 1)
  return `${(n / 1024 ** unit).toFixed(decimals)} ${units[unit]}`
}
function modelSize(value) {
  const n = Number(value || 0)
  return n >= 1024 ** 3 ? `${(n / 1024 ** 3).toFixed(2)} GB` : `${(n / 1024 ** 2).toFixed(1)} MB`
}
function percent(value) { return `${Math.round(Number(value || 0))}%` }
function dateTime(value) { return value ? new Date(Number(value) * 1000).toLocaleString('zh-CN', { hour12: false }).replaceAll('/', '-') : '—' }
function elapsed(item) {
  const start = Number(item.started_at || item.created_at || 0)
  const end = Number(item.finished_at || Date.now() / 1000)
  if (!start) return '—'
  const seconds = Math.max(0, Math.round(end - start))
  return `${String(Math.floor(seconds / 3600)).padStart(2, '0')}:${String(Math.floor(seconds % 3600 / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`
}
function uptime(value) {
  const hours = Math.floor(Number(value || 0) / 3600)
  return `${Math.floor(hours / 24)} 天 ${hours % 24} 小时`
}
function duration(value) {
  const seconds = Math.max(0, Math.round(Number(value || 0)))
  if (!seconds) return '计算中...'
  if (seconds < 60) return `约 ${seconds} 秒`
  if (seconds < 3600) return `约 ${Math.ceil(seconds / 60)} 分钟`
  return `约 ${Math.floor(seconds / 3600)} 小时 ${Math.ceil(seconds % 3600 / 60)} 分钟`
}
function modelJob(modelId) { return (modelData.value?.downloads || []).find(item => item.model_id === modelId && !['completed', 'cancelled'].includes(item.state)) }
function modelStateText(model) {
  const state = modelJob(model.id)?.state
  if (state === 'queued') return '等待下载'
  if (state === 'downloading') return '下载中'
  if (state === 'pausing') return '正在暂停'
  if (state === 'paused') return '已暂停'
  if (state === 'cancelling') return '正在取消'
  if (state === 'failed') return '下载失败'
  if (model.installed && !model.verified) return '需要修复'
  return model.installed ? '已安装' : '可下载'
}
function shortRevision(value) { return value ? String(value).slice(0, 8) : '—' }
function modelVersionText(model) {
  const labels = {
    not_installed: '—', not_checked: '未检查', local_version_unknown: '本地版本未知',
    up_to_date: '已是最新', update_available: '有可用更新', check_failed: '检查失败'
  }
  return labels[model.version_status] || '未检查'
}
function modelVersionClass(model) {
  if (model.version_status === 'update_available') return 'update'
  if (model.version_status === 'up_to_date') return 'latest'
  if (model.version_status === 'check_failed') return 'error'
  return ''
}
function softwareVersionText(value) {
  return ({ not_checked: '尚未检查', development: '开发版本', up_to_date: '已是最新版本', update_available: `发现 ${softwareUpdate.value.latest_version || '新版本'}`, check_failed: '检查失败' })[value] || '尚未检查'
}
function downloadStateText(state) {
  return ({ queued: '等待下载', downloading: '正在下载', pausing: '正在暂停', paused: '已暂停', cancelling: '正在取消', failed: '下载失败' })[state] || state
}
function statusText(value) {
  if (running.has(value)) return '运行中'
  if (waiting.has(value)) return '等待中'
  if (completed.has(value)) return '已完成'
  if (failed.has(value)) return '失败'
  return value || '未知'
}
function statusClass(value) {
  if (running.has(value)) return 'blue'
  if (waiting.has(value)) return 'amber'
  if (completed.has(value)) return 'green'
  if (failed.has(value)) return 'red'
  return 'muted'
}
function typeIcon(type) { return type === 'transcode' ? Video : type === 'translation' ? Activity : FileText }

onMounted(() => {
  refresh().then(() => Promise.all([checkVersions(false, true), runReadiness(true)]))
  timer = window.setInterval(refresh, 4000)
  window.addEventListener('hashchange', () => { page.value = location.hash.replace('#/', '') || 'overview' })
})
onBeforeUnmount(() => window.clearInterval(timer))
</script>

<template>
  <div class="shell">
    <header class="topbar">
      <div class="brand">
        <img :src="logoUrl" alt="MovieMuse" />
        <strong>MovieMuse</strong><span>Worker</span>
      </div>
      <div class="top-divider" />
      <div class="online" :class="{ offline: !connected && !loading, connecting: loading }"><i /> {{ loading ? '正在连接' : connected ? '在线' : '连接异常' }}</div>
      <div class="top-divider" />
      <div class="hostname">主机名： {{ status?.hostname || '正在连接...' }}</div>
      <button class="runtime-toggle" :class="{ stopped: !computeEnabled }" :disabled="runtimeBusy || !connected" :title="computeEnabled ? '关闭后不再接收新任务，当前任务会继续完成' : '启动后恢复接收新任务'" @click="toggleRuntime">
        <component :is="computeEnabled ? PowerOff : Power" :size="16" />
        {{ runtimeBusy ? '处理中' : computeEnabled ? '关闭算力' : '启动算力' }}
      </button>
      <div class="managed"><LockKeyhole :size="17" /> 配置由 MovieMuse 控制端管理（只读）</div>
    </header>

    <aside class="sidebar">
      <button v-for="item in nav" :key="item.id" :class="{ active: page === item.id }" @click="go(item.id)">
        <component :is="item.icon" :size="21" /><span>{{ item.label }}</span>
      </button>
    </aside>

    <main class="content" :class="{ loading }">
      <div v-if="notice" class="toast" @click="notice = ''">{{ notice }}<X :size="15" /></div>

      <section v-if="page === 'overview'" class="page overview-page">
        <div class="page-title page-title-actions"><div><h1>算力端概览</h1><p>先确认环境是否就绪，再交给 MovieMuse 控制端调度</p></div><button class="secondary compact" :disabled="readinessBusy || !connected" @click="runReadiness()"><RefreshCw :size="16" :class="{ spinning: readinessBusy }" />{{ readinessBusy ? '检测中' : '重新体检' }}</button></div>
        <div v-if="!computeEnabled" class="info-strip runtime-stopped"><span><PowerOff :size="18" />算力执行已关闭：管理页面和模型维护仍可使用，新任务会被拒绝</span><button @click="toggleRuntime"><Power :size="16" />立即启动</button></div>
        <article class="card readiness-hero" :class="`readiness-${readiness.status || 'not_checked'}`">
          <span class="readiness-hero-icon"><RefreshCw v-if="readinessBusy || readiness.status === 'checking'" class="spinning" :size="27" /><ShieldCheck v-else-if="readiness.ready" :size="29" /><AlertCircle v-else :size="29" /></span>
          <div class="readiness-copy"><label>{{ readiness.ready ? '可以接收任务' : readiness.status === 'not_checked' ? '等待自动体检' : '暂时不能接收任务' }}</label><h2>{{ readiness.summary || '正在读取算力环境' }}</h2><p>控制端配置 {{ status?.controller_synced_at ? '已同步' : '未同步' }} · 最近检测 {{ dateTime(readiness.checked_at) }}</p></div>
          <button v-if="readiness.next_action?.id !== 'none'" class="primary" :disabled="readinessBusy" @click="runReadinessAction"><component :is="readiness.next_action?.id === 'start' ? Power : readiness.next_action?.id === 'gpu_runtime' || readiness.next_action?.id === 'models' ? Download : RefreshCw" :size="18" />{{ readiness.next_action?.label || '立即处理' }}</button>
          <span v-else class="readiness-complete"><CheckCircle2 :size="18" />无需处理</span>
        </article>

        <div class="readiness-grid">
          <article v-for="item in readinessChecks" :key="item.id" class="card readiness-check" :class="`check-${item.status}`" :title="item.detail || item.summary">
            <span class="check-icon"><CheckCircle2 v-if="item.status === 'pass'" :size="18" /><AlertCircle v-else-if="item.status === 'fail'" :size="18" /><CircleMinus v-else :size="18" /></span>
            <div><label>{{ item.label }}</label><strong>{{ item.summary }}</strong></div>
            <small>{{ readinessStatusText(item.status) }}</small>
          </article>
          <article v-if="!readinessChecks.length" class="card readiness-check check-pending"><span class="check-icon"><RefreshCw :size="18" /></span><div><label>环境体检</label><strong>正在准备检测项目</strong></div><small>待检测</small></article>
        </div>

        <article class="card recommendation-card overview-recommendation">
          <span class="recommendation-icon"><Cpu :size="22" /></span>
          <div><label>显卡与推荐模型</label><strong>{{ modelRecommendation.gpu_name || '未检测到 NVIDIA GPU' }}<small v-if="modelRecommendation.memory_total_mb"> · {{ modelSize(modelRecommendation.memory_total_mb * 1048576) }} 显存</small></strong><p>{{ modelRecommendation.reason || '正在检测显卡和显存信息' }}</p></div>
          <div class="recommendation-model"><label>建议下载</label><strong>{{ modelRecommendation.recommended_label || '—' }}</strong><small>实际启用仍由控制端决定</small></div>
          <button class="secondary compact" @click="go('models')"><Box :size="17" />模型中心</button>
        </article>

        <article class="card pairing-card">
          <span class="pairing-icon"><KeyRound :size="22" /></span>
          <div class="pairing-copy"><label>MovieMuse 安全配对</label><strong>{{ pairing.paired ? '已配对，可以接收控制端任务' : '等待 MovieMuse 控制端配对' }}</strong><p>在控制端点击“自动扫描”，然后输入本机显示的六位配对码；不需要手动复制 API Token。</p></div>
          <div class="pairing-code"><label>一次性配对码</label><button type="button" title="复制配对码" @click="copyText(pairing.code, '配对码已复制')">{{ pairing.code || '------' }}<Copy :size="15" /></button><small>{{ pairing.expires_at ? `${dateTime(pairing.expires_at)} 前有效` : '正在生成' }}</small></div>
        </article>

        <article class="card work-card">
          <h2>当前工作</h2>
          <div class="work-head"><span>任务</span><span>状态</span><span>进度</span><span>详情</span></div>
          <div v-for="job in currentJobs" :key="job.id" class="work-row">
            <div class="task-name"><span class="type-icon" :class="job.type"><component :is="typeIcon(job.type)" :size="21" /></span><span><strong>{{ job.type_label }}</strong><small>{{ job.model || job.name }}</small></span></div>
            <span class="status"><i :class="statusClass(job.status)" />{{ statusText(job.status) }}</span>
            <div class="inline-progress"><strong v-if="!waiting.has(job.status)">{{ percent(job.progress) }}</strong><strong v-else>—</strong><div v-if="!waiting.has(job.status)" class="progress"><i :style="{ width: percent(job.progress) }" /></div></div>
            <span class="detail">{{ job.message || '—' }}</span>
          </div>
          <div v-if="!currentJobs.length" class="empty">当前没有运行中的任务</div>
        </article>

        <article class="card overview-footer">
          <div><label>当前模型</label><strong>{{ status?.effective_config?.model || '—' }}</strong><small>由 MovieMuse 控制端启用</small></div>
          <div><label>Worker 版本</label><strong>{{ status?.build_version || 'dev' }}</strong><small :class="{ update: softwareUpdate.update_available, error: softwareUpdate.version_status === 'check_failed' }">{{ softwareVersionText(softwareUpdate.version_status) }}</small></div>
          <div><label>最近错误</label><strong :class="{ error: status?.last_error }">{{ status?.last_error ? '有异常，请查看活动' : '无异常' }}</strong><small>{{ status?.last_error ? String(status.last_error).slice(0, 72) : `已运行 ${uptime(status?.uptime_seconds)}` }}</small></div>
          <div class="actions"><button class="mini" @click="copyDiagnostics"><Copy :size="16" />复制诊断</button><button class="secondary compact" @click="go('activity')"><Activity :size="18" />查看活动</button></div>
        </article>
      </section>

      <section v-else-if="page === 'models'" class="page models-page">
        <div class="page-title page-title-actions"><h1>Whisper 模型</h1><button class="secondary compact" :disabled="versionBusy" @click="checkVersions(true)"><RefreshCw :size="16" :class="{ spinning: versionBusy }" />{{ versionBusy ? '检查中' : '检查模型与软件更新' }}</button></div>
        <div class="storage-line"><HardDrive :size="18" /><span>存储空间</span><b>总计 {{ bytes(modelData?.storage?.total_bytes) }}</b><i /><b>已用 {{ bytes(modelData?.storage?.used_bytes) }} ({{ percent((modelData?.storage?.used_bytes || 0) / (modelData?.storage?.total_bytes || 1) * 100) }})</b><i /><b>可用 {{ bytes(modelData?.storage?.free_bytes) }}</b></div>
        <article class="card gpu-runtime-card" :class="`runtime-${gpuRuntime.status || 'missing'}`">
          <span class="gpu-runtime-icon"><Cpu v-if="gpuRuntime.status !== 'installing'" :size="22" /><RefreshCw v-else class="spinning" :size="22" /></span>
          <div class="gpu-runtime-copy"><label>NVIDIA 运行环境</label><strong>{{ gpuRuntimeTitle(gpuRuntime) }}</strong><p>{{ gpuRuntimeMessage(gpuRuntime) }}</p></div>
          <div class="gpu-runtime-meta">
            <label>{{ gpuRuntime.status === 'ready' ? '运行库来源' : '安装后占用' }}</label>
            <strong>{{ gpuRuntime.status === 'ready' ? gpuRuntimeSource(gpuRuntime.source) : bytes(gpuRuntime.estimated_installed_bytes || 1826900000) }}</strong>
          </div>
          <button v-if="['missing','failed'].includes(gpuRuntime.status || 'missing')" class="primary compact" :disabled="!gpuRuntime.installable" @click="installGpuRuntime"><Download :size="17" />{{ gpuRuntime.status === 'failed' ? '重新安装' : '安装运行环境' }}</button>
          <span v-else-if="gpuRuntime.status === 'installing'" class="runtime-state">请勿关闭窗口</span>
          <span v-else-if="gpuRuntime.status === 'installed_restart_required'" class="runtime-state restart">重新打开后生效</span>
          <span v-else class="runtime-state ready"><CheckCircle2 :size="17" />可用于 Whisper GPU 识别</span>
        </article>
        <article class="card recommendation-card">
          <span class="recommendation-icon"><Cpu :size="22" /></span>
          <div><label>显卡推荐</label><strong>{{ modelRecommendation.gpu_name || '未检测到 NVIDIA GPU' }}<small v-if="modelRecommendation.memory_total_mb"> · {{ modelSize(modelRecommendation.memory_total_mb * 1048576) }} 显存</small></strong><p>{{ modelRecommendation.reason || '正在检测显卡和显存信息' }}</p></div>
          <div class="recommendation-model"><label>推荐模型</label><strong>{{ modelRecommendation.recommended_label || '—' }}</strong><small>仅作建议，模型仍由控制端启用</small></div>
        </article>
        <h2 class="section-title">已安装的模型</h2>
        <article class="card model-table">
          <div class="model-head"><span>模型名称</span><span>状态</span><span>大小</span><span>验证状态</span><span>版本状态</span><span>操作</span></div>
          <div v-for="model in modelData?.models || []" :key="model.id" class="model-row">
            <div class="model-name"><strong>{{ model.label }}</strong><em v-if="model.active">当前使用</em><em v-else-if="model.id === modelRecommendation.recommended_model" class="recommended">显卡推荐</em><small v-if="model.active">由控制端选择</small></div>
            <span class="status"><i :class="modelJob(model.id)?.state === 'failed' ? 'red' : modelJob(model.id) ? 'blue' : model.installed && model.verified ? 'green' : model.available ? 'blue' : 'muted'" />{{ modelStateText(model) }}</span>
            <span>{{ modelSize(model.actual_size_bytes || model.size_bytes) }}</span>
            <span class="verify"><CheckCircle2 v-if="model.verified" class="verified-icon" :size="19" /><CircleMinus v-else :size="19" />{{ model.verified ? '已验证' : '未验证' }}<small v-if="model.modified_at">{{ dateTime(model.modified_at) }}</small></span>
            <span class="version-state" :class="modelVersionClass(model)" :title="model.version_error || ''"><strong>{{ modelVersionText(model) }}</strong><small v-if="model.installed">{{ shortRevision(model.local_revision) }}<template v-if="model.version_status === 'update_available'"> → {{ shortRevision(model.latest_revision) }}</template></small><small v-if="model.active && model.version_status === 'update_available'">需先在控制端切换</small></span>
            <div class="model-actions">
              <button v-if="!model.installed && !modelJob(model.id)" class="primary compact" :disabled="!!actionKey" @click="modelAction(model, 'download')"><Download :size="17" />下载模型</button>
              <button v-if="model.installed && !model.verified && !modelJob(model.id)" class="primary compact" :disabled="!!actionKey" @click="modelAction(model, 'download')"><RefreshCw :size="16" />修复模型</button>
              <button v-if="model.installed && model.verified && !model.active && !modelJob(model.id) && model.version_status === 'update_available'" class="primary compact" :disabled="!!actionKey" @click="modelAction(model, 'update')"><RefreshCw :size="16" />更新</button>
              <button v-if="model.installed && !model.verified" class="mini" :disabled="!!actionKey" @click="modelAction(model, 'verify')">验证</button>
              <button class="mini" :disabled="!!actionKey" @click="modelAction(model, 'open')"><FolderOpen :size="17" />打开文件夹</button>
              <button v-if="model.installed && !model.active && !modelJob(model.id)" class="icon-button" title="删除本机模型" :disabled="!!actionKey" @click="modelAction(model, 'remove')"><Trash2 :size="18" /></button>
            </div>
          </div>
        </article>

        <article v-if="visibleDownload" class="card download-card" :class="{ 'download-failed': visibleDownload.state === 'failed' }">
          <div class="download-title"><h2>{{ downloadStateText(visibleDownload.state) }}：{{ visibleDownload.model_id }}</h2><a href="https://huggingface.co/docs/huggingface_hub/guides/download" target="_blank" rel="noreferrer"> <ExternalLink :size="16" />手动安装指引</a></div>
          <div class="download-body">
            <template v-if="visibleDownload.state !== 'failed'">
              <label>下载进度</label>
              <div class="download-progress"><div class="progress"><i :style="{ width: percent(visibleDownload.progress) }" /></div><strong>{{ percent(visibleDownload.progress) }}</strong></div>
              <span>{{ bytes(visibleDownload.downloaded_bytes) }} / {{ bytes(visibleDownload.total_bytes) }}</span>
              <div class="download-meta"><div><label>下载速度</label><strong>{{ visibleDownload.speed_bytes_per_second ? `${bytes(visibleDownload.speed_bytes_per_second)}/s` : visibleDownload.state === 'paused' ? '已暂停' : '准备中...' }}</strong></div><div><label>剩余时间</label><strong>{{ duration(visibleDownload.eta_seconds) }}</strong></div><div><label>当前文件</label><strong :title="visibleDownload.current_file">{{ visibleDownload.current_file || `${visibleDownload.files_completed || 0} / ${visibleDownload.files_total || 0} 个文件` }}</strong></div></div>
              <div class="download-actions"><button class="secondary" :disabled="['pausing','cancelling','queued'].includes(visibleDownload.state)" @click="downloadAction(visibleDownload.state === 'paused' ? 'resume' : 'pause')"><component :is="visibleDownload.state === 'paused' ? Play : Pause" :size="20" />{{ visibleDownload.state === 'paused' ? '继续' : visibleDownload.state === 'pausing' ? '暂停中' : '暂停' }}</button><button class="primary" :disabled="visibleDownload.state === 'cancelling'" @click="downloadAction('cancel')"><X :size="21" />{{ visibleDownload.state === 'cancelling' ? '取消中' : '取消' }}</button></div>
            </template>
            <template v-else>
              <div class="download-error"><AlertCircle :size="22" /><div><strong>模型下载没有完成</strong><p>{{ visibleDownload.error || '未知下载错误' }}</p></div></div>
              <div class="download-actions"><button class="secondary" @click="downloadAction('resume')"><RefreshCw :size="18" />重新尝试</button><button class="danger-outline" @click="downloadAction('cancel')"><Trash2 :size="18" />清理临时文件</button></div>
            </template>
          </div>
        </article>
        <div class="info-strip bottom"><span><Info :size="19" />下载完成后，请在 MovieMuse 控制端启用模型。</span></div>
      </section>

      <section v-else class="page activity-page">
        <div class="page-title"><h1>任务活动</h1><p>查看算力端当前处理和最近完成的任务</p></div>
        <div class="count-line"><span>运行中 <b class="blue-text">{{ status?.counts?.running || 0 }}</b></span><i /><span>等待中 <b class="amber-text">{{ status?.counts?.waiting || 0 }}</b></span><i /><span>今日已完成 <b class="green-text">{{ status?.counts?.completed_today || 0 }}</b></span><i /><span>今日失败 <b class="red-text">{{ status?.counts?.failed_today || 0 }}</b></span></div>
        <div class="filters"><button v-for="item in [['all','全部'],['running','运行中'],['waiting','等待中'],['completed','已完成'],['failed','失败']]" :key="item[0]" :class="{ active: filter === item[0] }" @click="filter = item[0]">{{ item[1] }}</button></div>
        <article class="card activity-table">
          <div class="activity-head"><span>任务</span><span>类型</span><span>状态</span><span>进度</span><span>耗时</span><span>更新时间</span></div>
          <template v-for="item in filteredActivities" :key="item.id">
            <button class="activity-row" :class="{ failed: failed.has(item.status), selected: expanded === item.id }" @click="expanded = expanded === item.id ? '' : item.id">
              <span class="file-cell"><component :is="item.type === 'transcode' ? Video : FileText" :size="19" /><span>{{ item.name }}</span></span>
              <span>{{ item.type_label }}</span>
              <span class="status"><i :class="statusClass(item.status)" />{{ statusText(item.status) }}</span>
              <span class="table-progress"><b>{{ waiting.has(item.status) ? '—' : percent(item.progress) }}</b><span v-if="!waiting.has(item.status)" class="progress"><i :class="statusClass(item.status)" :style="{ width: percent(item.progress) }" /></span></span>
              <span>{{ elapsed(item) }}</span><span>{{ dateTime(item.updated_at) }}</span>
            </button>
            <div v-if="expanded === item.id" class="activity-detail">
              <dl><dt>任务 ID</dt><dd>{{ item.id }}</dd><dt>输入路径</dt><dd>{{ item.path || '—' }}</dd><dt>当前阶段</dt><dd>{{ item.stage || item.message || '—' }}</dd><dt>错误摘要</dt><dd :class="{ error: item.error }">{{ item.error || '无' }}</dd><dt>开始时间</dt><dd>{{ dateTime(item.started_at) }}</dd><dt>结束时间</dt><dd>{{ dateTime(item.finished_at) }}</dd></dl>
              <div class="detail-actions"><button class="secondary" @click.stop="copyText(item.error || item.message || '', '错误信息已复制')"><Copy :size="17" />复制错误信息</button><button class="danger-outline" @click.stop="copyDiagnostics"><Copy :size="17" />复制诊断</button></div>
            </div>
          </template>
          <div v-if="!filteredActivities.length" class="empty">暂无符合条件的任务</div>
        </article>
        <div class="info-strip bottom"><span><Info :size="19" />任务由 MovieMuse 控制端调度，算力端仅展示执行状态</span></div>
      </section>
    </main>
  </div>
</template>
