<template>
  <section class="compare-shell">
    <header class="compare-head">
      <div>
        <a class="back-link" href="/subtitles">← 任务中心</a>
        <h1>翻译效果对比</h1>
        <p>选取原文 SRT 的连续片段，用两组 DeepSeek 参数试译。不写入文件，也不进入任务队列。</p>
      </div>
    </header>

    <section class="setup-grid">
      <BaseCard as="article" class="panel" >
        <div class="section-heading">
          <h2>1. 选择字幕样本</h2>
          <p>填写控制台可读取的媒体目录路径，例如 <code>/media/study3/ABF-302.srt</code>。</p>
        </div>
        <div class="sample-controls">
          <input v-model.trim="sample.path" placeholder="/media/study3/movie.srt">
          <input v-model.number="sample.startNumber" type="number" min="1" placeholder="从第几段开始">
          <div class="segmented">
            <button type="button" :class="{ active: sample.count === 20 }" @click="sample.count = 20">20 段</button>
            <button type="button" :class="{ active: sample.count === 40 }" @click="sample.count = 40">40 段</button>
          </div>
          <BaseButton variant="primary"  type="button" :disabled="loadingSample || !sample.path" @click="loadSample">
            {{ loadingSample ? '载入中' : '载入字幕' }}
          </BaseButton>
        </div>
        <div class="source-mode">
          <span>送译内容</span>
          <div class="segmented">
            <button type="button" :class="{ active: sample.textMode === 'auto' }" @click="setTextMode('auto')">自动提取日文原文</button>
            <button type="button" :class="{ active: sample.textMode === 'full' }" @click="setTextMode('full')">使用完整文本</button>
          </div>
        </div>
        <NoticeBanner v-if="sampleError" tone="error">{{ sampleError }}</NoticeBanner>
        <div v-if="sample.segments.length" class="sample-summary">
          已载入 {{ sample.path }}，共 {{ sample.total }} 段；当前选择第 {{ sample.startNumber }} 至 {{ sample.startNumber + sample.segments.length - 1 }} 段。
          <strong v-if="sample.extractedCount">已从 {{ sample.extractedCount }} 段双语字幕提取日文原文。</strong>
        </div>
      </BaseCard>

      <BaseCard as="article" class="panel" >
        <div class="section-heading with-action">
          <div>
            <h2>2. 设置对比方案</h2>
            <p>A 沿用当前配置，B 用来试验新的语气与上下文。</p>
          </div>
          <BaseButton  type="button" @click="copyAToB">复制 A 到 B</BaseButton>
        </div>
        <div class="variant-grid">
          <DeepseekVariantEditor v-model="variantA" eyebrow="方案 A" title="当前设置" />
          <DeepseekVariantEditor v-model="variantB" eyebrow="方案 B" title="试验设置" accent />
        </div>
        <div class="execute-bar">
          <p>仅调用 DeepSeek 试译当前样本。</p>
          <BaseButton variant="primary"  type="button" :disabled="!sample.segments.length || comparing" @click="compare">
            {{ comparing ? '对比中' : '开始对比翻译' }}
          </BaseButton>
        </div>
      </BaseCard>
    </section>

    <BaseCard as="article" class="panel result-panel" v-if="results.length" >
      <div class="section-heading with-action">
        <div>
          <h2>3. 对比结果</h2>
          <p>A 耗时 {{ resultTime('a') }}，B 耗时 {{ resultTime('b') }}。满意后可将方案 B 保存为默认配置。</p>
        </div>
        <BaseButton variant="primary"  type="button" :disabled="savingVariant" @click="saveVariantB">
          {{ savingVariant ? '保存中' : '应用方案 B' }}
        </BaseButton>
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
              <span>送译原文</span>{{ segment.text }}
            </div>
          </div>
          <p>{{ resultTranslation('a', index) }}</p>
          <p>{{ resultTranslation('b', index) }}</p>
        </div>
      </div>
    </BaseCard>

    <div v-if="snackbar.show" class="toast" :class="snackbar.color">{{ snackbar.message }}</div>
  </section>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import DeepseekVariantEditor from './DeepseekVariantEditor.vue'
import { api, postJson } from '../lib/api'

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
  return { openai_translation_style: 'adult_natural', openai_style_intensity: 'medium', openai_context_lines: 2, openai_glossary: '' }
}

function selectVariant(settings) {
  return {
    openai_translation_style: settings.openai_translation_style || 'adult_natural',
    openai_style_intensity: settings.openai_style_intensity || 'medium',
    openai_context_lines: Number(settings.openai_context_lines ?? 2),
    openai_glossary: settings.openai_glossary || ''
  }
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
    const payload = await postJson('/api/subtitle/compare/sample', {
      path: sample.path,
      start: Math.max(0, Number(sample.startNumber || 1) - 1),
      count: sample.count,
      text_mode: sample.textMode,
      source_language: 'ja',
      target_language: 'zh'
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

function setTextMode(value) {
  sample.textMode = value
  if (sample.segments.length) loadSample()
}

function copyAToB() {
  variantB.value = { ...variantA.value }
}

async function compare() {
  comparing.value = true
  results.value = []
  try {
    const payload = await postJson('/api/subtitle/translate/compare', {
      source_language: 'ja',
      target_language: 'zh',
      segments: sample.segments,
      variants: [
        { id: 'a', label: '方案 A', settings: { ...baseSettings, ...variantA.value } },
        { id: 'b', label: '方案 B', settings: { ...baseSettings, ...variantB.value } }
      ]
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
    const payload = await postJson('/api/subtitle/settings', { ...baseSettings, ...variantB.value, default_translate_backend: 'deepseek' })
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
  window.setTimeout(() => { snackbar.show = false }, 4000)
}

onMounted(loadSettings)
</script>

<style scoped>
.compare-shell {
  display: flex;
  flex-direction: column;
  align-items: center;
  min-height: 100vh;
  padding: 44px 24px 64px;
  color: var(--mm-text);
  background: var(--mm-bg);
}

.compare-head {
  width: min(100%, 980px);
  margin-bottom: 24px;
}

.compare-head h1 {
  margin: 12px 0 6px;
  font-size: 34px;
  font-weight: 650;
}

.compare-head p,
.section-heading p,
.execute-bar p {
  margin: 0;
  color: var(--mm-muted);
  line-height: 1.7;
}

.compare-head p {
  max-width: 560px;
}

.back-link {
  color: var(--mm-primary);
  font-weight: 600;
  text-decoration: none;
}

.setup-grid {
  display: grid;
  width: min(100%, 980px);
  gap: 16px;
}

.panel {
  padding: 22px;
}

.section-heading {
  margin-bottom: 16px;
}

.section-heading h2 {
  margin: 0 0 6px;
  font-size: 22px;
  font-weight: 650;
}

.section-heading code {
  padding: 2px 5px;
  border-radius: 4px;
  background: var(--mm-surface);
}

.with-action {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 18px;
}

.sample-controls {
  display: grid;
  grid-template-columns: minmax(300px, 1fr) 160px auto auto;
  align-items: center;
  gap: 12px;
}

input {
  min-height: 44px;
  width: 100%;
  padding: 0 14px;
  border: 1px solid var(--mm-border);
  border-radius: 8px;
  background: var(--mm-control-bg);
  color: var(--mm-text);
}

.segmented {
  display: inline-flex;
  gap: 4px;
  padding: 4px;
  border: 1px solid var(--mm-border);
  border-radius: 12px;
  background: var(--mm-surface);
}

.segmented button {
  min-height: 36px;
  padding: 0 14px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--mm-muted);
  font-weight: 600;
}

.segmented button.active {
  background: var(--mm-card-bg);
  color: var(--mm-primary);
  box-shadow: var(--mm-shadow);
}

.source-mode,
.execute-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 16px;
  padding-top: 16px;
  border-top: 1px solid var(--mm-border);
}

.source-mode > span {
  color: var(--mm-muted);
  font-weight: 600;
}

.sample-summary {
  margin-top: 14px;
  padding: 12px 14px;
  border-radius: 8px;
  color: var(--mm-success);
  background: var(--mm-success-soft);
  font-size: 13px;
}

.variant-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}

.result-panel {
  width: min(100%, 980px);
  margin-top: 16px;
}

.compare-table {
  overflow: hidden;
  border: 1px solid var(--mm-border);
  border-radius: 14px;
}

.compare-row {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
}

.compare-row > * {
  margin: 0;
  padding: 12px 14px;
  border-top: 1px solid var(--mm-border);
  line-height: 1.6;
  white-space: pre-wrap;
}

.compare-row > * + * {
  border-left: 1px solid var(--mm-border);
}

.compare-row.table-head > * {
  border-top: 0;
  background: var(--mm-surface);
  color: var(--mm-muted);
  font-size: 13px;
  font-weight: 700;
}

.source-cell em {
  display: block;
  margin-bottom: 6px;
  color: var(--mm-muted);
  font-size: 12px;
  font-style: normal;
}

.sent-source {
  margin-top: 9px;
  padding: 8px 10px;
  border-radius: 8px;
  background: var(--mm-success-soft);
  color: var(--mm-success);
  font-size: 13px;
}

.sent-source span {
  margin-right: 8px;
  color: var(--mm-success);
  font-size: 11px;
  font-weight: 700;
}

.toast {
  position: fixed;
  right: 24px;
  bottom: 24px;
  z-index: 80;
  max-width: 480px;
  padding: 12px 16px;
  border-radius: 8px;
  background: var(--mm-text);
  color: #fff;
  box-shadow: var(--mm-shadow);
}

.toast.error {
  background: var(--mm-primary);
}

@media (max-width: 980px) {
  .sample-controls,
  .variant-grid,
  .compare-row {
    grid-template-columns: 1fr;
  }

  .with-action,
  .source-mode,
  .execute-bar {
    display: grid;
  }

  .compare-row > * + * {
    border-left: 0;
  }
}
</style>
