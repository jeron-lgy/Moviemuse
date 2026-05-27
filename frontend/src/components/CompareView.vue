<template>
  <v-main class="compare-shell">
    <header class="compare-head">
      <div>
        <a class="back-link" href="/subtitles"><v-icon size="18">mdi-arrow-left</v-icon> 字幕任务</a>
        <h1>翻译效果对比</h1>
        <p>选取原文 SRT 的连续片段，用两组 DeepSeek 参数试译。不会写入文件，也不会进入任务队列。</p>
      </div>
    </header>

    <section class="setup-grid">
      <v-sheet class="sample-panel" elevation="0">
        <div class="section-heading">
          <div>
            <h2>1. 选择字幕样本</h2>
            <p>填写 Unraid 控制台可读取的媒体目录路径，例如 <code>/media/study3/ABF-302.srt</code>。</p>
          </div>
        </div>
        <div class="sample-controls">
          <v-text-field v-model="sample.path" label="原文 SRT 路径" placeholder="/media/study3/movie.srt" />
          <v-text-field v-model.number="sample.startNumber" class="start-field" label="从第几段开始" type="number" min="1" />
          <v-btn-toggle v-model="sample.count" class="count-toggle" mandatory color="primary">
            <v-btn :value="20">20 段</v-btn>
            <v-btn :value="40">40 段</v-btn>
          </v-btn-toggle>
          <v-btn color="primary" :loading="loadingSample" @click="loadSample">载入字幕</v-btn>
        </div>
        <div class="source-mode">
          <span>送译内容</span>
          <v-btn-toggle v-model="sample.textMode" mandatory color="primary" @update:model-value="reloadLoadedSample">
            <v-btn value="auto">自动提取日文原文</v-btn>
            <v-btn value="full">使用完整文本</v-btn>
          </v-btn-toggle>
          <p>双语 SRT 默认只把原文发送给 DeepSeek，已有中文仅作为对照显示。</p>
        </div>
        <v-alert v-if="sampleError" type="error" variant="tonal" density="compact">{{ sampleError }}</v-alert>
        <div v-if="sample.segments.length" class="sample-summary">
          已载入 {{ sample.path }}，共 {{ sample.total }} 段；当前选择第 {{ sample.startNumber }} 至
          {{ sample.startNumber + sample.segments.length - 1 }} 段。
          <strong v-if="sample.extractedCount">已从 {{ sample.extractedCount }} 段双语字幕提取日文原文。</strong>
        </div>
      </v-sheet>

      <v-sheet class="variants-panel" elevation="0">
        <div class="section-heading with-action">
          <div>
            <h2>2. 设置对比方案</h2>
            <p>A 沿用当前配置；B 用来试验新的语气与上下文。</p>
          </div>
          <v-btn size="small" variant="text" prepend-icon="mdi-content-copy" @click="copyAToB">复制 A 到 B</v-btn>
        </div>
        <v-row>
          <v-col cols="12" md="6">
            <DeepseekVariantEditor v-model="variantA" eyebrow="方案 A" title="当前设置" />
          </v-col>
          <v-col cols="12" md="6">
            <DeepseekVariantEditor v-model="variantB" eyebrow="方案 B" title="试验设置" accent />
          </v-col>
        </v-row>
        <div class="execute-bar">
          <p>仅调用 DeepSeek 试译这段样本，不生成字幕文件。</p>
          <v-btn color="primary" prepend-icon="mdi-compare" :disabled="!sample.segments.length" :loading="comparing" @click="compare">
            开始对比翻译
          </v-btn>
        </div>
      </v-sheet>
    </section>

    <v-sheet v-if="results.length" class="result-panel" elevation="0">
      <div class="section-heading with-action">
        <div>
          <h2>3. 对比结果</h2>
          <p>
            A 耗时 {{ resultTime('a') }}；B 耗时 {{ resultTime('b') }}。满意后可将方案 B 保存为正式默认设置。
          </p>
        </div>
        <v-btn color="primary" variant="outlined" :loading="savingVariant" @click="saveVariantB">应用方案 B 为默认</v-btn>
      </div>
      <div class="compare-table">
        <div class="compare-row table-head">
          <span>时间 / 原文</span>
          <span>方案 A</span>
          <span>方案 B</span>
        </div>
        <div v-for="(segment, index) in sample.segments" :key="segment.index" class="compare-row">
          <div class="source-cell">
            <em>#{{ segment.index }} · {{ timeLabel(segment.start) }}</em>
            <p>{{ segment.display_text || segment.text }}</p>
            <div v-if="segment.source_extracted" class="sent-source">
              <span>送译原文</span>
              {{ segment.text }}
            </div>
          </div>
          <p>{{ resultTranslation('a', index) }}</p>
          <p>{{ resultTranslation('b', index) }}</p>
        </div>
      </div>
    </v-sheet>

    <v-snackbar v-model="snackbar.show" :color="snackbar.color" timeout="5000">{{ snackbar.message }}</v-snackbar>
  </v-main>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import DeepseekVariantEditor from './DeepseekVariantEditor.vue'

const sample = reactive({ path: '', startNumber: 1, count: 20, textMode: 'auto', total: 0, extractedCount: 0, segments: [] })
const baseSettings = reactive({})
const variantA = ref(defaultVariant())
const variantB = ref({ ...defaultVariant(), openai_translation_style: 'seductive' })
const results = ref([])
const loadingSample = ref(false)
const comparing = ref(false)
const savingVariant = ref(false)
const sampleError = ref('')
const snackbar = reactive({ show: false, message: '', color: 'primary' })

function defaultVariant() {
  return {
    openai_translation_style: 'adult_natural',
    openai_style_intensity: 'medium',
    openai_context_lines: 2,
    openai_glossary: ''
  }
}

function selectVariant(settings) {
  return {
    openai_translation_style: settings.openai_translation_style || 'adult_natural',
    openai_style_intensity: settings.openai_style_intensity || 'medium',
    openai_context_lines: Number(settings.openai_context_lines ?? 2),
    openai_glossary: settings.openai_glossary || ''
  }
}

async function api(url, options = {}) {
  const response = await fetch(url, { headers: { 'Content-Type': 'application/json' }, ...options })
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(payload.detail || '请求失败')
  return payload
}

async function loadSettings() {
  const payload = await api('/api/subtitle/console')
  Object.assign(baseSettings, payload.compute_settings || {})
  variantA.value = selectVariant(baseSettings)
  variantB.value = { ...variantA.value, openai_translation_style: 'seductive' }
}

async function loadSample() {
  loadingSample.value = true
  sampleError.value = ''
  results.value = []
  try {
    const payload = await api('/api/subtitle/compare/sample', {
      method: 'POST',
      body: JSON.stringify({
        path: sample.path,
        start: Math.max(0, Number(sample.startNumber || 1) - 1),
        count: sample.count,
        text_mode: sample.textMode,
        source_language: 'ja',
        target_language: 'zh'
      })
    })
    sample.path = payload.path
    sample.total = payload.total
    sample.extractedCount = payload.extracted_count || 0
    sample.segments = payload.segments || []
  } catch (error) {
    sample.segments = []
    sample.extractedCount = 0
    sampleError.value = error.message || String(error)
  } finally {
    loadingSample.value = false
  }
}

function reloadLoadedSample() {
  if (sample.segments.length) loadSample()
}

function copyAToB() {
  variantB.value = { ...variantA.value }
}

async function compare() {
  comparing.value = true
  results.value = []
  try {
    const payload = await api('/api/subtitle/translate/compare', {
      method: 'POST',
      body: JSON.stringify({
        source_language: 'ja',
        target_language: 'zh',
        segments: sample.segments,
        variants: [
          { id: 'a', label: '方案 A', settings: { ...baseSettings, ...variantA.value } },
          { id: 'b', label: '方案 B', settings: { ...baseSettings, ...variantB.value } }
        ]
      })
    })
    results.value = payload.variants || []
  } catch (error) {
    notify(`对比失败：${error.message || error}`, 'error')
  } finally {
    comparing.value = false
  }
}

async function saveVariantB() {
  savingVariant.value = true
  try {
    const payload = await api('/api/subtitle/settings', {
      method: 'POST',
      body: JSON.stringify({ ...baseSettings, ...variantB.value, default_translate_backend: 'deepseek' })
    })
    Object.assign(baseSettings, payload.settings || variantB.value)
    variantA.value = selectVariant(baseSettings)
    notify(payload.warning || '方案 B 已保存为默认 DeepSeek 翻译设置。', payload.warning ? 'warning' : 'primary')
  } catch (error) {
    notify(`保存失败：${error.message || error}`, 'error')
  } finally {
    savingVariant.value = false
  }
}

function result(id) {
  return results.value.find((item) => item.id === id)
}

function resultTranslation(id, index) {
  return result(id)?.translations?.[index] || '未返回'
}

function resultTime(id) {
  const milliseconds = result(id)?.elapsed_ms
  return milliseconds == null ? '-' : `${(milliseconds / 1000).toFixed(1)} 秒`
}

function timeLabel(seconds) {
  const total = Math.max(0, Number(seconds || 0))
  const hours = String(Math.floor(total / 3600)).padStart(2, '0')
  const minutes = String(Math.floor((total % 3600) / 60)).padStart(2, '0')
  const secs = String(Math.floor(total % 60)).padStart(2, '0')
  return `${hours}:${minutes}:${secs}`
}

function notify(message, color = 'primary') {
  snackbar.message = message
  snackbar.color = color
  snackbar.show = true
}

onMounted(loadSettings)
</script>

<style scoped>
.compare-shell {
  min-height: 100vh;
  padding: 28px max(22px, calc((100vw - 1520px) / 2));
  color: #111827;
  background:
    radial-gradient(circle at top left, rgba(193, 236, 231, 0.72), transparent 340px),
    #f6f8fb;
}

.compare-head {
  margin-bottom: 22px;
}

.compare-head h1 {
  margin: 12px 0 5px;
  font-size: 32px;
}

.compare-head p,
.section-heading p,
.execute-bar p {
  margin: 0;
  color: #6b768f;
}

.back-link {
  color: #087e74;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-weight: 700;
  text-decoration: none;
}

.setup-grid {
  display: grid;
  gap: 16px;
}

.sample-panel,
.variants-panel,
.result-panel {
  padding: 20px;
  border: 1px solid #dce8f2;
  border-radius: 8px;
  background: #fff;
}

.section-heading {
  margin-bottom: 16px;
}

.section-heading h2 {
  margin: 0 0 5px;
  font-size: 20px;
}

.section-heading code {
  padding: 2px 5px;
  background: #eff5f6;
  border-radius: 4px;
}

.with-action {
  display: flex;
  justify-content: space-between;
  align-items: start;
  gap: 18px;
}

.sample-controls {
  display: grid;
  grid-template-columns: minmax(300px, 1fr) 160px auto auto;
  align-items: start;
  gap: 12px;
}

.count-toggle {
  min-height: 48px;
}

.sample-summary {
  margin-top: 12px;
  padding: 10px 12px;
  border-radius: 8px;
  color: #087e74;
  background: #eafaf5;
  font-size: 13px;
}

.sample-summary strong {
  margin-left: 8px;
}

.source-mode {
  margin: 2px 0 14px;
  display: flex;
  align-items: center;
  gap: 12px;
  color: #667085;
  font-size: 13px;
  font-weight: 700;
}

.source-mode p {
  margin: 0;
  color: #718096;
  font-weight: 500;
}

.execute-bar {
  margin-top: 15px;
  padding-top: 15px;
  border-top: 1px solid #e5edf4;
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
}

.result-panel {
  margin-top: 16px;
}

.compare-table {
  border: 1px solid #e1eaf2;
  border-radius: 8px;
  overflow: hidden;
}

.compare-row {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
}

.compare-row > * {
  margin: 0;
  padding: 12px 14px;
  border-top: 1px solid #edf1f5;
  white-space: pre-wrap;
  line-height: 1.55;
}

.compare-row > * + * {
  border-left: 1px solid #edf1f5;
}

.compare-row.table-head > * {
  border-top: 0;
  color: #667085;
  background: #f7fafc;
  font-size: 13px;
  font-weight: 800;
}

.source-cell em {
  display: block;
  margin-bottom: 6px;
  color: #718096;
  font-size: 12px;
  font-style: normal;
}

.source-cell p {
  margin: 0;
}

.sent-source {
  margin-top: 9px;
  padding: 7px 9px;
  border-radius: 6px;
  color: #17685f;
  background: #ecfaf6;
  font-size: 13px;
  line-height: 1.5;
}

.sent-source span {
  margin-right: 8px;
  color: #087e74;
  font-size: 11px;
  font-weight: 800;
}

@media (max-width: 980px) {
  .sample-controls {
    grid-template-columns: 1fr;
  }

  .compare-row {
    grid-template-columns: 1fr;
  }

  .compare-row > * + * {
    border-left: 0;
  }

  .with-action,
  .execute-bar,
  .source-mode {
    display: block;
  }

  .source-mode p {
    margin-top: 8px;
  }
}
</style>
