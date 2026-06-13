<!-- LEGACY: /subscription-wash now redirects into /system. Keep only as a reference until old routes are removed. -->
<template>
  <section class="wash-view">
    <PageHeader kicker="订阅管理" title="洗版" description="配置已入库番号的中文、4K 洗版策略；下载和后处理状态在任务中心追踪。">
      <template #actions>
        <BaseButton type="button" :disabled="loading" @click="loadSettings">刷新</BaseButton>
        <BaseButton variant="primary" type="button" :disabled="saving" @click="saveSettings">
          {{ saving ? '保存中' : '保存' }}
        </BaseButton>
      </template>
    </PageHeader>

    <NoticeBanner v-if="message" >{{ message }}</NoticeBanner>
    <NoticeBanner v-if="errorMessage" tone="error">{{ errorMessage }}</NoticeBanner>

    <section class="wash-grid">
      <BaseCard as="article" class="wash-card" >
        <h2>跟踪策略</h2>
        <p>控制是否自动保留洗版请求，以及到期后是否自动取消。</p>
        <div class="toggle-line">
          <span>启用洗版跟踪<small>允许已入库番号进入中文或 4K 洗版轮询。</small></span>
          <BaseSwitch v-model="wash.enabled" aria-label="启用洗版跟踪" />
        </div>
        <div class="toggle-line">
          <span>到期自动取消<small>超过期限仍未匹配资源时自动结束请求。</small></span>
          <BaseSwitch v-model="wash.auto_cancel_expired" aria-label="到期自动取消" />
        </div>
      </BaseCard>

      <BaseCard as="article" class="wash-card" >
        <h2>匹配条件</h2>
        <p>这些条件会影响洗版 MTeam 资源过滤。</p>
        <div class="form-grid compact">
          <FormField label="过期期限（天）">
          <input v-model.number="wash.expire_days" type="number" min="7" max="365">
        </FormField>
          <FormField label="最少做种数">
          <input v-model.number="wash.min_seeders" type="number" min="0" max="999">
        </FormField>
          <FormField label="最大体积（GB）">
          <input v-model.number="wash.max_size_gb" type="number" min="1" max="500">
        </FormField>
        </div>
      </BaseCard>

      <BaseCard as="article" class="wash-card wide" >
        <h2>洗版类型</h2>
        <p>勾选需要关注的版本类型，并设置命中多个资源时的偏好。</p>
        <div class="option-grid">
          <div class="toggle-line">
            <span>检查中文<small>关注中文字幕、中文命名或相关资源。</small></span>
            <BaseSwitch v-model="wash.check_chinese" aria-label="检查中文" />
          </div>
          <div class="toggle-line">
            <span>检查 4K<small>关注 4K / UHD 资源。</small></span>
            <BaseSwitch v-model="wash.check_4k" aria-label="检查 4K" />
          </div>
          <div class="toggle-line">
            <span>优先中文<small>中文和普通资源同时命中时优先中文。</small></span>
            <BaseSwitch v-model="wash.prefer_chinese" aria-label="优先中文" />
          </div>
          <div class="toggle-line">
            <span>优先 4K<small>4K 和普通资源同时命中时优先 4K。</small></span>
            <BaseSwitch v-model="wash.prefer_4k" aria-label="优先 4K" />
          </div>
        </div>
      </BaseCard>
    </section>
  </section>
</template>

<script setup>
import { reactive, ref } from 'vue'
import { api, postJson } from '../lib/api'

const loading = ref(false)
const saving = ref(false)
const message = ref('')
const errorMessage = ref('')
const wash = reactive(defaultWash())

loadSettings()

async function loadSettings() {
  loading.value = true
  message.value = ''
  errorMessage.value = ''
  try {
    const payload = await api('/api/subscriptions/settings')
    Object.assign(wash, defaultWash(), payload.settings?.wash || {})
  } catch (error) {
    errorMessage.value = error.message || '读取洗版设置失败'
  } finally {
    loading.value = false
  }
}

async function saveSettings() {
  saving.value = true
  message.value = ''
  errorMessage.value = ''
  try {
    await postJson('/api/subscriptions/settings', { wash: { ...wash } })
    message.value = '洗版设置已保存'
  } catch (error) {
    errorMessage.value = error.message || '保存洗版设置失败'
  } finally {
    saving.value = false
  }
}

function defaultWash() {
  return {
    enabled: true,
    expire_days: 90,
    min_seeders: 1,
    max_size_gb: 80,
    auto_cancel_expired: true,
    check_chinese: true,
    check_4k: true,
    prefer_chinese: true,
    prefer_4k: true
  }
}
</script>

<style scoped>
.wash-view {
  display: grid;
  gap: 24px;
}

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
.wash-card p {
  margin-top: 8px;
  color: var(--mm-muted);
  line-height: 1.7;
}

.wash-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}

.wash-card {
  display: grid;
  align-content: start;
  gap: 16px;
  padding: 24px;
}

.wash-card.wide {
  grid-column: 1 / -1;
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

.toggle-line:first-of-type {
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

.form-grid,
.option-grid {
  display: grid;
  gap: 16px;
}

.form-grid.compact {
  grid-template-columns: repeat(3, minmax(0, 1fr));
}

.option-grid {
  grid-template-columns: repeat(2, minmax(0, 1fr));
}

@media (max-width: 980px) {
  .wash-grid,
  .form-grid.compact,
  .option-grid {
    grid-template-columns: 1fr;
  }
}
</style>
