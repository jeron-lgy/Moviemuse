<template>
  <section class="mm-page">
    <PageHeader
      kicker="系统"
      title="自动任务"
      description="集中配置自动字幕、自动转码和 qB 接管策略；编码细节仍在任务中心的转码设置里维护。"
    >
      <template #actions>
        <BaseButton type="button" :disabled="loading" @click="loadAll">刷新</BaseButton>
        <BaseButton type="button" variant="primary" :disabled="saving" @click="saveAutomation">
          {{ saving ? '保存中' : '保存策略' }}
        </BaseButton>
      </template>
    </PageHeader>

    <NoticeBanner v-if="message">{{ message }}</NoticeBanner>
    <NoticeBanner v-if="errorMessage" tone="error">{{ errorMessage }}</NoticeBanner>

    <section class="automation-grid">
      <BaseCard as="article" class="automation-card">
        <h2>自动化开关</h2>
        <p>这些开关决定订阅下载完成后，是否自动进入后处理队列。</p>
        <div class="toggle-list">
          <div class="toggle-line">
            <span>启用自动转码<small>命中后派发转码任务，默认 AV1 NVENC。</small></span>
            <BaseSwitch v-model="postprocess.auto_transcode_enabled" aria-label="启用自动转码" />
          </div>
          <div class="toggle-line">
            <span>启用自动字幕<small>需要字幕的任务会继续进入字幕生成和翻译链路。</small></span>
            <BaseSwitch v-model="postprocess.auto_subtitle_enabled" aria-label="启用自动字幕" />
          </div>
          <div class="toggle-line">
            <span>算力端上线后自动执行队列<small>等待算力端的任务会自动推进。</small></span>
            <BaseSwitch v-model="postprocess.worker_auto_run" aria-label="算力端上线后自动执行队列" />
          </div>
          <div class="toggle-line">
            <span>接管外部 qB 种子<small>扫描已带接管标签的手动种子，源文件默认保留。</small></span>
            <BaseSwitch v-model="postprocess.external_qb_adopt_enabled" aria-label="接管外部 qB 种子" />
          </div>
          <div class="toggle-line">
            <span>外部接管完成后移动源文件<small>转码和字幕完成后把原 qB 文件移入 trash，会影响继续做种。</small></span>
            <BaseSwitch v-model="postprocess.external_qb_trash_source_enabled" aria-label="外部接管完成后移动源文件" />
          </div>
        </div>
      </BaseCard>

      <BaseCard as="article" class="automation-card">
        <div class="option-head">
          <div>
            <h2>qBittorrent 接管范围</h2>
            <p>读取 qB 分类和标签后勾选，避免误接管其他下载。</p>
          </div>
          <BaseButton type="button" :disabled="loadingQb" @click="loadQbOptions">
            {{ loadingQb ? '读取中' : '读取 qB' }}
          </BaseButton>
        </div>
        <div class="option-grid">
          <div>
            <h3>下载分类</h3>
            <label v-for="item in mergedCategories" :key="item" class="option-chip">
              <input v-model="postprocess.allowed_categories" type="checkbox" :value="item">{{ item }}
            </label>
            <p v-if="!mergedCategories.length" class="empty-mini">暂无分类配置。</p>
          </div>
          <div>
            <h3>种子标签</h3>
            <label v-for="item in mergedTags" :key="item" class="option-chip">
              <input v-model="postprocess.required_tags" type="checkbox" :value="item">{{ item }}
            </label>
            <p v-if="!mergedTags.length" class="empty-mini">暂无标签配置。</p>
          </div>
        </div>
      </BaseCard>
    </section>

    <SubscriptionTasksView embedded />
  </section>
</template>

<script setup>
import { computed, reactive, ref } from 'vue'
import { api, postJson } from '../lib/api'
import SubscriptionTasksView from './SubscriptionTasksView.vue'
import { BaseButton, BaseCard, BaseSwitch, NoticeBanner, PageHeader } from '../components/ui'

const loading = ref(false)
const saving = ref(false)
const loadingQb = ref(false)
const message = ref('')
const errorMessage = ref('')
const qbOptions = reactive({ categories: [], tags: [] })
const postprocess = reactive({
  auto_transcode_enabled: false,
  auto_subtitle_enabled: false,
  worker_auto_run: false,
  external_qb_adopt_enabled: false,
  external_qb_trash_source_enabled: false,
  allowed_categories: [],
  required_tags: []
})

const mergedCategories = computed(() => mergeUnique(qbOptions.categories, postprocess.allowed_categories))
const mergedTags = computed(() => mergeUnique(qbOptions.tags, postprocess.required_tags))

loadAll()

async function loadAll() {
  loading.value = true
  errorMessage.value = ''
  try {
    const postPayload = await api('/api/postprocess/settings')
    Object.assign(postprocess, {
      auto_transcode_enabled: !!postPayload.settings?.auto_transcode_enabled,
      auto_subtitle_enabled: !!postPayload.settings?.auto_subtitle_enabled,
      worker_auto_run: !!postPayload.settings?.worker_auto_run,
      external_qb_adopt_enabled: !!postPayload.settings?.external_qb_adopt_enabled,
      external_qb_trash_source_enabled: !!postPayload.settings?.external_qb_trash_source_enabled,
      allowed_categories: Array.isArray(postPayload.settings?.allowed_categories) ? postPayload.settings.allowed_categories : [],
      required_tags: Array.isArray(postPayload.settings?.required_tags) ? postPayload.settings.required_tags : []
    })
  } catch (error) {
    errorMessage.value = error.message || '读取自动任务配置失败'
  } finally {
    loading.value = false
  }
}

async function loadQbOptions() {
  loadingQb.value = true
  message.value = ''
  errorMessage.value = ''
  try {
    const payload = await api('/api/integrations/qbittorrent/options')
    qbOptions.categories = payload.categories || []
    qbOptions.tags = payload.tags || []
    message.value = payload.status === 'ok' ? '已读取 qB 可选分类和标签。' : (payload.message || '读取失败，已保留当前配置。')
  } catch (error) {
    errorMessage.value = error.message || '读取 qB 选项失败'
  } finally {
    loadingQb.value = false
  }
}

async function saveAutomation() {
  saving.value = true
  message.value = ''
  errorMessage.value = ''
  try {
    await postJson('/api/postprocess/settings', {
      auto_transcode_enabled: postprocess.auto_transcode_enabled,
      auto_subtitle_enabled: postprocess.auto_subtitle_enabled,
      worker_auto_run: postprocess.worker_auto_run,
      external_qb_adopt_enabled: postprocess.external_qb_adopt_enabled,
      external_qb_trash_source_enabled: postprocess.external_qb_trash_source_enabled,
      allowed_categories: postprocess.allowed_categories,
      required_tags: postprocess.required_tags
    })
    message.value = '自动任务策略已保存'
  } catch (error) {
    errorMessage.value = error.message || '保存自动任务失败'
  } finally {
    saving.value = false
  }
}

function mergeUnique(primary = [], secondary = []) {
  return Array.from(new Set([...(primary || []), ...(secondary || [])].map((item) => String(item || '').trim()).filter(Boolean)))
}
</script>

<style scoped>
h2,
h3,
p {
  margin: 0;
}

.automation-card p {
  margin-top: 8px;
  color: var(--mm-muted);
  line-height: 1.7;
}

.option-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
}

.automation-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}

.automation-card {
  display: grid;
  align-content: start;
  gap: 16px;
  min-height: 260px;
}

.toggle-list {
  display: grid;
}

.toggle-line {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  min-height: 58px;
  border-top: 1px solid var(--mm-border);
  color: var(--mm-text);
  font-weight: 600;
}

.toggle-line:first-child {
  border-top: 0;
}

.toggle-line span {
  display: grid;
  gap: 4px;
}

.toggle-line small {
  color: var(--mm-muted);
  font-weight: 400;
}

input[type="checkbox"] {
  width: 22px;
  height: 22px;
}

select {
  width: 100%;
}

.option-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 18px;
}

.option-chip {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-height: 36px;
  margin: 10px 8px 0 0;
  padding: 0 12px;
  border: 1px solid var(--mm-border);
  border-radius: 999px;
  background: var(--mm-control-bg);
  font-weight: 500;
}

.empty-mini {
  margin-top: 12px;
}

@media (max-width: 900px) {
  .automation-grid,
  .option-grid {
    grid-template-columns: 1fr;
  }

  .option-head {
    display: grid;
  }
}
</style>
