<template>
  <section class="settings-view">
    <PageHeader
      kicker="系统"
      title="系统"
      description="集中配置订阅链路、洗版策略、Jellyfin、MTeam、qBittorrent 和通知。"
    >
      <template #actions>
        <BaseButton type="button" :disabled="loading" @click="loadAll">刷新</BaseButton>
        <BaseButton variant="primary" type="button" :disabled="saving" @click="saveAll">
          {{ saving ? '保存中' : '保存' }}
        </BaseButton>
      </template>
    </PageHeader>

    <NoticeBanner v-if="message">{{ message }}</NoticeBanner>
    <NoticeBanner v-if="errorMessage" tone="error">{{ errorMessage }}</NoticeBanner>

    <BaseCard as="nav" class="setting-tabs">
      <button v-for="tab in tabs" :key="tab.key" type="button" :class="{ active: activeTab === tab.key }" @click="activeTab = tab.key">
        {{ tab.label }}
      </button>
    </BaseCard>

    <BaseCard v-if="activeTab === 'mteam'" class="setting-panel">
      <div class="panel-head">
        <div>
          <h2>MTeam</h2>
          <p>搜索番号和女优时联动 MTeam，支持 RSS 和 API 参数保留。</p>
        </div>
        <BaseButton type="button" @click="testIntegration('mteam')">测试连接</BaseButton>
      </div>
      <div class="form-grid">
        <FormField label="启用" wide>
          <input v-model="system.mteam.enabled" type="checkbox">
        </FormField>
        <FormField label="网址">
          <input v-model.trim="system.mteam.site_url" placeholder="https://zp.m-team.io/">
        </FormField>
        <FormField label="模式">
          <select v-model="system.mteam.mode">
            <option value="rss">RSS</option>
            <option value="api">API</option>
          </select>
        </FormField>
        <FormField label="RSS 地址" wide>
          <input v-model.trim="system.mteam.rss_url">
        </FormField>
        <FormField label="API 地址">
          <input v-model.trim="system.mteam.api_url">
        </FormField>
        <FormField label="API Key">
          <input v-model.trim="system.mteam.api_key" type="password">
        </FormField>
      </div>
    </BaseCard>

    <BaseCard v-else-if="activeTab === 'qb'" class="setting-panel">
      <div class="panel-head">
        <div>
          <h2>qBittorrent</h2>
          <p>订阅下载会把 MTeam 种子推送到这里。</p>
        </div>
        <BaseButton type="button" @click="testIntegration('qbittorrent')">测试连接</BaseButton>
      </div>
      <div class="form-grid">
        <FormField label="地址">
          <input v-model.trim="system.qbittorrent.url" placeholder="http://host:8080">
        </FormField>
        <FormField label="用户名">
          <input v-model.trim="system.qbittorrent.username">
        </FormField>
        <FormField label="密码">
          <input v-model.trim="system.qbittorrent.password" type="password">
        </FormField>
        <FormField label="下载路径">
          <input v-model.trim="system.qbittorrent.save_path">
        </FormField>
        <FormField label="下载分类">
          <input v-model.trim="system.qbittorrent.category">
        </FormField>
        <FormField label="标签">
          <input v-model.trim="system.qbittorrent.tags">
        </FormField>
      </div>
    </BaseCard>

    <BaseCard v-else-if="activeTab === 'jellyfin'" class="setting-panel">
      <div class="panel-head">
        <div>
          <h2>Jellyfin</h2>
          <p>订阅前查重，已入库的番号会自动切到已入库。读取媒体库前会先保存当前 Jellyfin 配置。</p>
        </div>
        <div class="panel-actions">
          <BaseButton type="button" :disabled="loadingLibraries" @click="loadJellyfinLibraries">
            {{ loadingLibraries ? '读取中' : '读取媒体库' }}
          </BaseButton>
          <BaseButton type="button" @click="testIntegration('jellyfin')">测试连接</BaseButton>
        </div>
      </div>
      <div class="form-grid">
        <FormField label="地址">
          <input v-model.trim="system.jellyfin.url" placeholder="http://host:8096">
        </FormField>
        <FormField label="密钥">
          <input v-model.trim="system.jellyfin.api_key" type="password">
        </FormField>
        <FormField label="用户">
          <input v-model.trim="system.jellyfin.username">
        </FormField>
        <FormField label="媒体库">
          <select v-if="jellyfinLibraries.length" v-model="selectedJellyfinLibrary" @change="syncJellyfinLibrary">
            <option value="">全部媒体库</option>
            <option v-for="library in jellyfinLibraries" :key="library.id" :value="library.id">
              {{ library.name }}
            </option>
          </select>
          <input v-else v-model.trim="system.jellyfin.library_id" placeholder="点击读取媒体库，或手动填写媒体库 ID">
        </FormField>
        <FormField label="媒体库名称">
          <input v-model.trim="system.jellyfin.library_name">
        </FormField>
        <FormField label="启用查重">
          <input v-model="system.jellyfin.dedupe_enabled" type="checkbox">
        </FormField>
      </div>
    </BaseCard>

    <BaseCard v-else-if="activeTab === 'strategy'" class="setting-panel">
      <div class="panel-head">
        <div>
          <h2>订阅与洗版</h2>
          <p>订阅策略和洗版策略合并到系统维护；定时任务仍在任务页统一执行。</p>
        </div>
      </div>
      <div class="form-grid">
        <FormField label="启用订阅轮询">
          <input v-model="subscription.poll_enabled" type="checkbox">
        </FormField>
        <FormField label="最大共演人数">
          <input v-model.number="subscription.max_coactors" type="number" min="1" max="12">
        </FormField>
        <FormField label="洗版启用">
          <input v-model="subscription.wash.enabled" type="checkbox">
        </FormField>
        <FormField label="洗版过期天数">
          <input v-model.number="subscription.wash.expire_days" type="number" min="1">
        </FormField>
        <FormField label="洗版检查中文">
          <input v-model="subscription.wash.check_chinese" type="checkbox">
        </FormField>
        <FormField label="洗版检查 4K">
          <input v-model="subscription.wash.check_4k" type="checkbox">
        </FormField>
      </div>
    </BaseCard>

    <BaseCard v-else-if="activeTab === 'makers'" class="setting-panel">
      <div class="panel-head">
        <div>
          <h2>常驻厂牌</h2>
          <p>厂牌发售页会读取这里的列表。</p>
        </div>
        <BaseButton variant="primary" type="button" @click="addMaker">添加厂牌</BaseButton>
      </div>
      <div class="maker-list">
        <div v-for="(maker, index) in subscription.pinned_makers" :key="`${maker.name}-${index}`" class="maker-row">
          <input v-model.trim="maker.name" placeholder="厂牌">
          <input v-model.trim="maker.url" placeholder="JavDB 链接">
          <BaseButton type="button" @click="removeMaker(index)">删除</BaseButton>
        </div>
      </div>
    </BaseCard>

    <NotificationsView v-else ref="notificationsView" embedded />
  </section>
</template>

<script setup>
import { reactive, ref } from 'vue'
import { api, postJson } from '../lib/api'
import NotificationsView from './NotificationsView.vue'
import { BaseButton, BaseCard, FormField, NoticeBanner, PageHeader } from '../components/ui'

const tabs = [
  { key: 'mteam', label: 'MTeam' },
  { key: 'qb', label: 'qBittorrent' },
  { key: 'jellyfin', label: 'Jellyfin' },
  { key: 'strategy', label: '订阅与洗版' },
  { key: 'makers', label: '常驻厂牌' },
  { key: 'notifications', label: '通知' }
]

const defaultMakers = [
  { name: 'S1 NO.1 STYLE', url: 'https://javdb.com/makers/7R?f=download' },
  { name: 'PRESTIGE', url: 'https://javdb.com/makers/6M?f=download' },
  { name: 'IDEA POCKET', url: 'https://javdb.com/makers/ZXX?f=download' },
  { name: 'Madonna', url: 'https://javdb.com/makers/zKW?f=download' },
  { name: 'SOD Create', url: 'https://javdb.com/makers/q6?f=download' }
]

const activeTab = ref('jellyfin')
const loading = ref(false)
const saving = ref(false)
const loadingLibraries = ref(false)
const message = ref('')
const errorMessage = ref('')
const jellyfinLibraries = ref([])
const selectedJellyfinLibrary = ref('')
const notificationsView = ref(null)

const system = reactive({
  mteam: { site_url: '', mode: 'rss', rss_url: '', api_url: '', api_key: '', enabled: false },
  qbittorrent: { url: '', username: '', password: '', save_path: '', category: '', tags: '' },
  jellyfin: { url: '', api_key: '', username: '', library_id: '', library_name: '', dedupe_enabled: true }
})

const subscription = reactive({
  poll_enabled: true,
  max_coactors: 2,
  wash: { enabled: true, expire_days: 90, check_chinese: true, check_4k: true },
  pinned_makers: [...defaultMakers]
})

loadAll()

async function loadAll() {
  loading.value = true
  message.value = ''
  errorMessage.value = ''
  try {
    const [systemPayload, subPayload] = await Promise.all([
      api('/api/system-settings'),
      api('/api/subscriptions/settings')
    ])
    Object.assign(system.mteam, systemPayload.settings?.mteam || {})
    Object.assign(system.qbittorrent, systemPayload.settings?.qbittorrent || {})
    Object.assign(system.jellyfin, systemPayload.settings?.jellyfin || {})
    selectedJellyfinLibrary.value = system.jellyfin.library_id || ''
    Object.assign(subscription, {
      poll_enabled: subPayload.settings?.poll_enabled ?? true,
      max_coactors: subPayload.settings?.max_coactors ?? 2,
      wash: { ...subscription.wash, ...(subPayload.settings?.wash || {}) },
      pinned_makers: Array.isArray(subPayload.settings?.pinned_makers) && subPayload.settings.pinned_makers.length
        ? subPayload.settings.pinned_makers.map((item) => ({ name: item.name || '', url: item.url || '' }))
        : [...defaultMakers]
    })
  } catch (err) {
    errorMessage.value = err.message || '读取设置失败'
  } finally {
    loading.value = false
  }
}

async function saveSystemSettings() {
  await postJson('/api/system-settings', {
    mteam: { ...system.mteam },
    qbittorrent: { ...system.qbittorrent },
    jellyfin: { ...system.jellyfin }
  })
}

async function saveSubscriptionSettings() {
  await postJson('/api/subscriptions/settings', {
    poll_enabled: subscription.poll_enabled,
    max_coactors: subscription.max_coactors,
    wash: { ...subscription.wash },
    pinned_makers: subscription.pinned_makers
      .filter((item) => item.name || item.url)
      .map((item) => ({ name: item.name, url: item.url }))
  })
}

async function saveAll() {
  saving.value = true
  message.value = ''
  errorMessage.value = ''
  try {
    if (activeTab.value === 'notifications' && notificationsView.value?.saveNotifications) {
      await notificationsView.value.saveNotifications()
      return
    }
    syncJellyfinLibrary()
    await Promise.all([saveSystemSettings(), saveSubscriptionSettings()])
    message.value = '系统设置已保存'
  } catch (err) {
    errorMessage.value = err.message || '保存设置失败'
  } finally {
    saving.value = false
  }
}

async function testIntegration(name) {
  message.value = ''
  errorMessage.value = ''
  try {
    syncJellyfinLibrary()
    await saveSystemSettings()
    const result = await postJson(`/api/integrations/test/${name}`)
    message.value = result.message || result.detail?.message || `${name} 测试完成`
  } catch (err) {
    errorMessage.value = err.message || `${name} 测试失败`
  }
}

async function loadJellyfinLibraries() {
  loadingLibraries.value = true
  message.value = ''
  errorMessage.value = ''
  try {
    await saveSystemSettings()
    const payload = await api('/api/jellyfin/libraries')
    jellyfinLibraries.value = Array.isArray(payload.libraries) ? payload.libraries : []
    selectedJellyfinLibrary.value = system.jellyfin.library_id || ''
    if (!selectedJellyfinLibrary.value && jellyfinLibraries.value.length === 1) {
      selectedJellyfinLibrary.value = jellyfinLibraries.value[0].id
      syncJellyfinLibrary()
    }
    message.value = `已读取 ${jellyfinLibraries.value.length} 个 Jellyfin 媒体库`
  } catch (err) {
    errorMessage.value = err.message || '读取 Jellyfin 媒体库失败'
  } finally {
    loadingLibraries.value = false
  }
}

function syncJellyfinLibrary() {
  if (jellyfinLibraries.value.length) {
    const library = jellyfinLibraries.value.find((item) => item.id === selectedJellyfinLibrary.value)
    system.jellyfin.library_id = selectedJellyfinLibrary.value || ''
    system.jellyfin.library_name = library?.name || system.jellyfin.library_name || ''
  } else {
    selectedJellyfinLibrary.value = system.jellyfin.library_id || ''
  }
}

function addMaker() {
  subscription.pinned_makers.push({ name: '', url: '' })
}

function removeMaker(index) {
  subscription.pinned_makers.splice(index, 1)
}
</script>

<style scoped>
.settings-view {
  display: grid;
  gap: 18px;
}

.setting-tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  padding: 10px;
}

.setting-tabs button {
  min-height: 40px;
  padding: 0 16px;
  border: 1px solid var(--mm-border);
  border-radius: 14px;
  background: #fff;
  color: var(--mm-muted);
  font: inherit;
  font-weight: var(--mm-font-weight-medium);
  cursor: pointer;
}

.setting-tabs button.active,
.setting-tabs button:hover {
  border-color: rgba(255, 56, 92, .35);
  background: #fff0f3;
  color: var(--mm-primary);
}

.setting-panel {
  display: grid;
  gap: 18px;
}

.panel-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 18px;
}

.panel-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  justify-content: flex-end;
}

.panel-head h2 {
  margin: 0;
  color: var(--mm-text);
  font-size: 22px;
  font-weight: var(--mm-font-weight-semibold);
}

.panel-head p {
  margin: 6px 0 0;
  color: var(--mm-muted);
  line-height: 1.6;
}

.form-grid,
.maker-row {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}

.form-grid :deep(.mm-field:first-child:has(input[type="checkbox"])) {
  align-self: end;
}

.maker-list {
  display: grid;
  gap: 12px;
}

.maker-row {
  grid-template-columns: minmax(160px, 240px) minmax(0, 1fr) auto;
  align-items: end;
}

input,
select {
  width: 100%;
  min-height: 44px;
  padding: 0 14px;
  border: 1px solid var(--mm-border);
  border-radius: 14px;
  background: #fff;
  color: var(--mm-text);
  font: inherit;
}

input[type="checkbox"] {
  width: 22px;
  min-height: 22px;
  accent-color: var(--mm-primary);
}

@media (max-width: 760px) {
  .panel-head,
  .form-grid,
  .maker-row {
    grid-template-columns: 1fr;
    display: grid;
  }
}
</style>
