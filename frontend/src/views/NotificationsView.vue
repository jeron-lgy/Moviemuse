<template>
  <section class="notifications-view" :class="{ embedded }">
    <PageHeader v-if="!embedded" kicker="系统" title="通知" description="管理消息发送渠道，选择哪些系统事件需要主动推送。">
      <template #actions>
        <BaseButton variant="primary" type="button" :disabled="saving" @click="saveNotifications">
          {{ saving ? '保存中' : '保存' }}
        </BaseButton>
      </template>
    </PageHeader>

    <NoticeBanner v-if="message">{{ message }}</NoticeBanner>
    <NoticeBanner v-if="errorMessage" tone="error">{{ errorMessage }}</NoticeBanner>

    <BaseCard class="channel-board">
      <div class="section-head">
        <div>
          <h2>通知渠道</h2>
          <p>点击加号选择 Server 酱、Gotify 或 WeCom，弹出配置卡片后填写参数。</p>
        </div>
        <BaseButton variant="primary" type="button" :disabled="saving" @click="saveNotifications">
          {{ saving ? '保存中' : '保存渠道' }}
        </BaseButton>
      </div>

      <div class="channel-list">
        <article
          v-for="(channel, index) in channels"
          :key="channel.id || index"
          class="channel-card"
          :class="{ disabled: !channel.enabled }"
          @click="openChannel(index)"
        >
          <div class="channel-top">
            <i :class="{ on: channel.enabled }"></i>
            <strong>{{ channel.name }}</strong>
          </div>
          <span>{{ channelTypeLabel(channel.type) }}</span>
          <button type="button" @click.stop="removeChannel(index)">x</button>
        </article>

        <div v-if="!channels.length" class="empty-card">还没有通知渠道，点击加号新增。</div>
      </div>

      <div class="actions-row">
        <div class="add-wrap">
          <BaseButton class="add-button" type="button" @click="showTypeMenu = !showTypeMenu">+</BaseButton>
          <div v-if="showTypeMenu" class="type-menu">
            <button type="button" @click="createChannel('serverchan')">Server 酱</button>
            <button type="button" @click="createChannel('gotify')">Gotify</button>
            <button type="button" @click="createChannel('wechat_work')">WeCom</button>
          </div>
        </div>
      </div>
    </BaseCard>

    <BaseCard class="events-card">
      <h2>可发送通知</h2>
      <p>默认建议开启订阅链路、入库状态和失败告警；扫描完成和字幕完成可以按需开启。</p>
      <div class="event-list">
        <article v-for="event in events" :key="event.key" class="event-row">
          <input v-model="eventStates[event.key]" type="checkbox" :aria-label="event.name">
          <span>
            <strong>{{ event.name }}</strong>
            <em>{{ event.description }}</em>
            <small>{{ templatePreview(event).title }}</small>
          </span>
          <BaseButton size="sm" type="button" @click="openTemplate(event)">模板</BaseButton>
        </article>
      </div>
    </BaseCard>

    <div v-if="embedded" class="embedded-actions">
      <BaseButton type="button" @click="loadAll">刷新</BaseButton>
      <BaseButton variant="primary" type="button" :disabled="saving" @click="saveNotifications">
        {{ saving ? '保存中' : '保存' }}
      </BaseButton>
    </div>

    <TaskDialog v-if="editingChannel" :title="`配置 ${editingChannel.name}`" @close="editingChannel = null">
      <BaseSwitch v-model="editingChannel.enabled" label="启用" />

      <div class="form-grid">
        <FormField label="名称">
          <input v-model.trim="editingChannel.name">
        </FormField>

        <template v-if="editingChannel.type === 'serverchan'">
          <FormField label="SendKey" wide>
            <SecretInput v-model.trim="editingChannel.config.send_key" autocomplete="off" placeholder="SCT..." />
          </FormField>
        </template>

        <template v-else-if="editingChannel.type === 'gotify'">
          <FormField label="Gotify 地址" wide>
            <input v-model.trim="editingChannel.config.url" placeholder="https://gotify.example.com">
          </FormField>
          <FormField label="Token">
            <SecretInput v-model.trim="editingChannel.config.token" autocomplete="off" />
          </FormField>
          <FormField label="优先级">
            <input v-model.number="editingChannel.config.priority" type="number" min="0" max="10">
          </FormField>
        </template>

        <template v-else-if="editingChannel.type === 'wechat_work'">
          <FormField label="Corp ID">
            <input v-model.trim="editingChannel.config.corp_id" placeholder="ww...">
          </FormField>
          <FormField label="Corp Secret">
            <SecretInput v-model.trim="editingChannel.config.corp_secret" autocomplete="off" />
          </FormField>
          <FormField label="Agent ID">
            <input v-model.trim="editingChannel.config.agent_id">
          </FormField>
          <FormField label="Proxy">
            <input v-model.trim="editingChannel.config.proxy" placeholder="http://host:port">
          </FormField>
          <FormField label="To User">
            <input v-model.trim="editingChannel.config.touser" placeholder="@all or UserID|UserID">
          </FormField>
          <FormField label="Default Image URL" wide>
            <input v-model.trim="editingChannel.config.default_image_url" placeholder="https://example.com/cover.jpg">
          </FormField>
          <FormField label="Callback Token">
            <SecretInput v-model.trim="editingChannel.config.token" autocomplete="off" />
          </FormField>
          <FormField label="Encoding AES Key" wide>
            <SecretInput v-model.trim="editingChannel.config.aes_key" autocomplete="off" />
          </FormField>
          <FormField label="Callback Path">
            <input v-model.trim="editingChannel.config.callback_path" placeholder="/api/v1/message">
          </FormField>
          <BaseSwitch v-model="editingChannel.config.cover_enabled" label="发送封面图" />
        </template>
      </div>

      <template #actions>
        <BaseButton type="button" @click="testChannel(editingChannel)">测试</BaseButton>
        <BaseButton
          v-if="editingChannel.type === 'wechat_work'"
          type="button"
          :disabled="sendingWechatTest"
          @click="testWechatSuite(editingChannel)"
        >
          {{ sendingWechatTest ? '发送中' : '发送全部微信测试' }}
        </BaseButton>
        <BaseButton v-if="editingChannel.type === 'wechat_work'" type="button" @click="createWechatMenu(editingChannel)">Create menu</BaseButton>
        <BaseButton type="button" @click="editingChannel = null">取消</BaseButton>
        <BaseButton variant="primary" type="button" :disabled="saving" @click="commitChannelAndSave">
          {{ saving ? '保存中' : '保存渠道' }}
        </BaseButton>
      </template>
    </TaskDialog>

    <TaskDialog v-if="editingEvent" :title="`通知模板：${editingEvent.name}`" @close="editingEvent = null">
      <div class="form-grid">
        <FormField label="标题模板" wide hint="可用变量：{event_name}、{time}、{title}、{status}、{detail}">
          <input v-model.trim="editingTemplate.title">
        </FormField>
        <FormField label="正文模板" wide>
          <textarea v-model.trim="editingTemplate.message" rows="5"></textarea>
        </FormField>
      </div>
      <BaseCard class="template-preview">
        <span>消息预览</span>
        <strong>{{ renderTemplate(editingTemplate.title, editingEvent) }}</strong>
        <p>{{ renderTemplate(editingTemplate.message, editingEvent) }}</p>
      </BaseCard>
      <template #actions>
        <BaseButton type="button" @click="resetTemplate(editingEvent)">恢复默认</BaseButton>
        <BaseButton type="button" @click="editingEvent = null">取消</BaseButton>
        <BaseButton variant="primary" type="button" @click="commitTemplate">保存模板</BaseButton>
      </template>
    </TaskDialog>
  </section>
</template>

<script setup>
import { reactive, ref } from 'vue'
import TaskDialog from '../components/TaskDialog.vue'
import { api, postJson } from '../lib/api'
import { BaseButton, BaseCard, BaseSwitch, FormField, NoticeBanner, PageHeader, SecretInput } from '../components/ui'

defineProps({
  embedded: {
    type: Boolean,
    default: false
  }
})

const channelTypes = {
  serverchan: { label: 'Server 酱', defaults: { send_key: '' } },
  gotify: { label: 'Gotify', defaults: { url: '', token: '', priority: 5 } },
  wechat_work: {
    label: 'WeCom',
    defaults: {
      corp_id: '',
      corp_secret: '',
      agent_id: '',
      proxy: '',
      touser: '@all',
      default_image_url: '',
      cover_enabled: false,
      token: '',
      aes_key: '',
      callback_path: '/api/v1/message'
    }
  }
}

const saving = ref(false)
const message = ref('')
const errorMessage = ref('')
const showTypeMenu = ref(false)
const sendingWechatTest = ref(false)
const channels = ref([])
const events = ref([])
const eventStates = reactive({})
const eventTemplates = reactive({})
const editingChannel = ref(null)
const editingIndex = ref(-1)
const editingEvent = ref(null)
const editingTemplate = reactive({ title: '', message: '' })

loadAll()

async function loadAll() {
  try {
    const [settingsPayload, eventsPayload] = await Promise.all([
      api('/api/system-settings'),
      api('/api/notifications/events')
    ])
    const notify = settingsPayload.settings?.notifications || {}
    channels.value = (notify.channels || []).map(normalizeChannel)
    events.value = eventsPayload.events || []
    const savedEvents = notify.events || {}
    const savedTemplates = notify.templates || {}
    for (const event of events.value) {
      eventStates[event.key] = savedEvents[event.key] ?? defaultEventEnabled(event.key)
      eventTemplates[event.key] = normalizeTemplate(savedTemplates[event.key], event)
    }
  } catch (error) {
    errorMessage.value = error.message || '读取通知配置失败'
  }
}

function channelTypeLabel(type) {
  return channelTypes[type]?.label || type
}

function createChannel(type) {
  const defaults = channelTypes[type]
  if (!defaults) return
  editingIndex.value = -1
  editingChannel.value = {
    id: `${type}-${Date.now()}`,
    type,
    name: defaults.label,
    enabled: true,
    config: clone(defaults.defaults)
  }
  showTypeMenu.value = false
}

function openChannel(index) {
  editingIndex.value = index
  editingChannel.value = normalizeChannel(channels.value[index])
}

function removeChannel(index) {
  channels.value.splice(index, 1)
}

function commitChannel() {
  if (!editingChannel.value) return
  const channel = normalizeChannel(editingChannel.value)
  if (editingIndex.value >= 0) channels.value[editingIndex.value] = channel
  else channels.value.push(channel)
  editingChannel.value = null
}

async function commitChannelAndSave() {
  commitChannel()
  await saveNotifications()
}

async function testChannel(channel) {
  message.value = ''
  errorMessage.value = ''
  try {
    const result = await postJson('/api/notifications/test', { channel: normalizeChannel(channel) })
    message.value = `测试结果：${result.status || 'ok'} ${result.message || ''}`.trim()
  } catch (error) {
    errorMessage.value = error.message || '测试通知失败'
  }
}

async function testWechatSuite(channel) {
  message.value = ''
  errorMessage.value = ''
  sendingWechatTest.value = true
  try {
    const result = await postJson('/api/notifications/wechat-work/test-suite', { channel: normalizeChannel(channel) })
    const sent = result.sent ?? 0
    const total = result.total ?? 0
    message.value = `企业微信全部测试已发送：${sent}/${total}`
  } catch (error) {
    errorMessage.value = error.message || '发送企业微信测试失败'
  } finally {
    sendingWechatTest.value = false
  }
}

async function createWechatMenu(channel) {
  message.value = ''
  errorMessage.value = ''
  try {
    const result = await postJson('/api/wechat/menu', { channel: normalizeChannel(channel) })
    message.value = `Menu result: ${result.status || 'ok'} ${result.message || ''}`.trim()
  } catch (error) {
    errorMessage.value = error.message || 'Create WeCom menu failed'
  }
}

async function saveNotifications() {
  saving.value = true
  message.value = ''
  errorMessage.value = ''
  try {
    await postJson('/api/system-settings', {
      notifications: {
        channels: channels.value.map(normalizeChannel),
        events: { ...eventStates },
        templates: { ...eventTemplates }
      }
    })
    message.value = '通知配置已保存'
  } catch (error) {
    errorMessage.value = error.message || '保存通知配置失败'
  } finally {
    saving.value = false
  }
}

function clone(value) {
  return JSON.parse(JSON.stringify(value || {}))
}

function normalizeChannel(channel) {
  const source = clone(channel)
  const type = source.type || 'serverchan'
  const defaults = channelTypes[type] || channelTypes.serverchan
  return {
    id: source.id || `${type}-${Date.now()}`,
    type,
    name: source.name || defaults.label,
    enabled: source.enabled !== false,
    config: {
      ...clone(defaults.defaults),
      ...clone(source.config)
    }
  }
}

function defaultEventEnabled(key) {
  return [
    'av_subscribed',
    'mteam_found',
    'torrent_sent',
    'jellyfin_in_library',
    'task_failed',
    'automation_actress_poll',
    'automation_av_download',
    'automation_wash_download'
  ].includes(key)
}

function defaultTemplate(event) {
  return {
    title: `MovieMuse：${event.name}`,
    message: `{event_name}\n状态：{status}\n详情：{detail}\n时间：{time}`
  }
}

function normalizeTemplate(template, event) {
  const fallback = defaultTemplate(event)
  const source = template && typeof template === 'object' ? template : {}
  return {
    title: String(source.title || fallback.title),
    message: String(source.message || fallback.message)
  }
}

function sampleData(event) {
  return {
    event_name: event.name,
    time: new Date().toLocaleString('zh-CN', { hour12: false }),
    title: event.name,
    status: eventStates[event.key] ? '已启用' : '未启用',
    detail: event.description
  }
}

function renderTemplate(template, event) {
  const data = sampleData(event)
  return String(template || '').replace(/\{([a-zA-Z0-9_]+)\}/g, (_, key) => data[key] ?? `{${key}}`)
}

function templatePreview(event) {
  const template = eventTemplates[event.key] || defaultTemplate(event)
  return {
    title: renderTemplate(template.title, event),
    message: renderTemplate(template.message, event)
  }
}

function openTemplate(event) {
  editingEvent.value = event
  const template = eventTemplates[event.key] || defaultTemplate(event)
  editingTemplate.title = template.title
  editingTemplate.message = template.message
}

function resetTemplate(event) {
  const template = defaultTemplate(event)
  editingTemplate.title = template.title
  editingTemplate.message = template.message
}

function commitTemplate() {
  if (!editingEvent.value) return
  eventTemplates[editingEvent.value.key] = {
    title: editingTemplate.title,
    message: editingTemplate.message
  }
  editingEvent.value = null
}

defineExpose({
  saveNotifications,
  loadAll
})
</script>

<style scoped>
.notifications-view {
  display: grid;
  gap: 24px;
  --mm-input-radius: 14px;
}

.section-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 24px;
}

h2,
p {
  margin: 0;
}

.section-head p,
.events-card p {
  margin-top: 8px;
  color: var(--mm-muted);
  line-height: 1.7;
}

.channel-list {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 14px;
  margin: 18px 0;
}

.channel-card,
.empty-card {
  min-height: 118px;
  padding: 18px;
  border: 1px solid var(--mm-border);
  border-radius: 18px;
  background: var(--mm-surface);
}

.channel-card {
  position: relative;
  cursor: pointer;
}

.channel-card.disabled {
  opacity: .58;
}

.channel-top {
  display: flex;
  align-items: center;
  gap: 10px;
}

.channel-top i {
  width: 8px;
  height: 8px;
  border-radius: 999px;
  background: #c9c9c9;
}

.channel-top i.on {
  background: var(--mm-primary);
}

.channel-top strong {
  color: var(--mm-text);
  font-size: 18px;
}

.channel-card > span {
  display: block;
  margin-top: 28px;
  color: var(--mm-muted);
}

.channel-card > button {
  position: absolute;
  top: 12px;
  right: 12px;
  border: 0;
  background: transparent;
  color: var(--mm-muted);
  font-size: 20px;
  cursor: pointer;
}

.actions-row {
  display: flex;
  align-items: center;
  gap: 12px;
}

.embedded-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  justify-content: flex-start;
}

.add-wrap {
  position: relative;
}

.add-button {
  min-width: 72px;
}

.type-menu {
  position: absolute;
  left: 0;
  bottom: calc(100% + 8px);
  z-index: 20;
  display: grid;
  min-width: 150px;
  padding: 8px;
  border: 1px solid var(--mm-border);
  border-radius: 14px;
  background: var(--mm-card-bg);
  box-shadow: var(--mm-menu-shadow);
}

.type-menu button {
  min-height: 38px;
  padding: 0 12px;
  border: 0;
  border-radius: 10px;
  background: transparent;
  text-align: left;
  cursor: pointer;
}

.type-menu button:hover {
  background: var(--mm-primary-soft);
  color: var(--mm-primary);
}

.event-list {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 12px;
  margin-top: 16px;
}

.event-row {
  display: grid;
  grid-template-columns: 22px minmax(0, 1fr) auto;
  gap: 10px;
  align-items: start;
  padding: 14px;
  border: 1px solid var(--mm-border);
  border-radius: 14px;
  background: var(--mm-surface);
}

.event-row input,
.enable-row input {
  width: 18px;
  height: 18px;
  accent-color: var(--mm-primary);
}

.event-row strong,
.event-row em,
.event-row small {
  display: block;
}

.event-row em,
.event-row small {
  margin-top: 4px;
  color: var(--mm-muted);
  font-size: 13px;
  font-style: normal;
  line-height: 1.5;
}

.event-row small {
  color: var(--mm-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.enable-row {
  display: flex;
  align-items: center;
  gap: 12px;
  font-weight: 600;
}

.form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}

input {
  width: 100%;
  min-height: 44px;
  padding: 0 14px;
  border: 1px solid var(--mm-border);
  border-radius: 14px;
  background: var(--mm-control-bg);
  color: var(--mm-text);
  font: inherit;
}

textarea {
  width: 100%;
  min-height: 120px;
  padding: 12px 14px;
  border: 1px solid var(--mm-border);
  border-radius: 14px;
  background: var(--mm-control-bg);
  color: var(--mm-text);
  font: inherit;
  resize: vertical;
}

.template-preview {
  display: grid;
  gap: 8px;
  padding: 18px;
  box-shadow: none;
}

.template-preview span {
  color: var(--mm-muted);
  font-size: var(--mm-font-size-sm);
}

.template-preview strong,
.template-preview p {
  margin: 0;
}

.template-preview p {
  color: var(--mm-muted);
  line-height: 1.7;
  white-space: pre-wrap;
}

@media (max-width: 760px) {
  .section-head,
  .form-grid {
    grid-template-columns: 1fr;
    display: grid;
  }
}
</style>
