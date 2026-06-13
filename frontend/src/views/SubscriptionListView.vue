<template>
  <section class="subscriptions-view" @click="closeActionMenu">
    <PageHeader kicker="订阅管理" title="订阅" description="管理番号和女优订阅，按状态跟踪下载、完成、入库和洗版。">
      <template #actions>
        <BaseButton type="button" @click="refetchAll">刷新</BaseButton>
        <BaseButton type="button" :disabled="cleanupBusy" @click="cleanupDirtySubscriptions">
          {{ cleanupBusy ? '清理中' : '清理脏订阅' }}
        </BaseButton>
        <BaseButton variant="primary" type="button" :disabled="downloadingPending" @click="downloadPending">
          {{ downloadingPending ? '执行中' : '一键下载订阅中' }}
        </BaseButton>
      </template>
    </PageHeader>

    <NoticeBanner v-if="notice">{{ notice }}</NoticeBanner>
    <NoticeBanner v-if="error" tone="error">{{ error.message }}</NoticeBanner>

    <nav class="top-tabs">
      <button :class="{ active: mainTab === 'av' }" type="button" @click="mainTab = 'av'">
        订阅番号 <span>{{ avItems.length }}</span>
      </button>
      <button :class="{ active: mainTab === 'actress' }" type="button" @click="mainTab = 'actress'">
        订阅女优 <span>{{ actressItems.length }}</span>
      </button>
    </nav>

    <section v-if="mainTab === 'av'" class="tab-panel">
      <div class="state-tabs">
        <button v-for="state in avStates" :key="state.key" :class="{ active: avState === state.key }" type="button" @click="avState = state.key">
          {{ state.label }} <span>{{ state.count }}</span>
        </button>
      </div>

      <BaseCard as="div" class="empty" v-if="isLoading">正在读取订阅番号...</BaseCard>
      <BaseCard as="div" class="empty" v-else-if="filteredAvs.length === 0">这个状态暂时没有番号。</BaseCard>
      <div v-else class="card-grid">
        <SubscriptionMovieCard
          v-for="item in filteredAvs"
          :key="item.id"
          :item="item"
          :cover-url="proxyImage(item.cover, item)"
          :actors="actresses(item)"
          :status-note="displayStatusNote(item)"
          @detail="openDetail"
          @actor="openActress"
        >
          <template #menu>
            <div class="more-menu" @click.stop>
              <button class="more-trigger" type="button" aria-label="更多操作" @click.stop="toggleActionMenu(item.id)">
                <span></span><span></span><span></span>
              </button>
              <div v-if="actionMenuId === item.id" class="more-panel" @click.stop>
                <button v-if="item.status !== 'pending'" type="button" @click="setAvStatus(item, 'pending')">移回订阅中</button>
                <button v-if="item.status !== 'done'" type="button" @click="setAvStatus(item, 'done')">标记完成</button>
                <button v-if="item.status !== 'in_library'" type="button" @click="setAvStatus(item, 'in_library')">标记已入库</button>
              </div>
            </div>
          </template>

          <template #actions>
            <template v-if="item.status === 'in_library'">
              <BaseButton variant="primary" type="button" :disabled="busyId === item.id" @click.stop="openWashDialog(item)">洗版</BaseButton>
              <BaseButton variant="danger" type="button" :disabled="busyId === item.id" @click.stop="removeAv(item)">取消</BaseButton>
            </template>
            <template v-else>
              <BaseButton variant="primary" type="button" :disabled="busyId === item.id" @click.stop="downloadAv(item)">下载</BaseButton>
              <BaseButton variant="danger" type="button" :disabled="busyId === item.id" @click.stop="removeAv(item)">取消</BaseButton>
            </template>
          </template>
        </SubscriptionMovieCard>
      </div>
    </section>

    <section v-else class="tab-panel">
      <BaseCard as="div" class="empty" v-if="isActressLoading">正在读取订阅女优...</BaseCard>
      <BaseCard as="div" class="empty" v-else-if="actressItems.length === 0">还没有订阅女优。</BaseCard>
      <div v-else class="actress-grid">
        <BaseCard as="article" class="actress-card" v-for="item in actressItems" :key="item.id">
          <div class="card-menu">
            <div class="more-menu" @click.stop>
              <button class="more-trigger" type="button" aria-label="更多操作" @click.stop="toggleActressMenu(item.id)">
                <span></span><span></span><span></span>
              </button>
              <div v-if="actressMenuId === item.id" class="more-panel" @click.stop>
                <button type="button" @click="openSubscribeLatestDialog(item)">一键订阅</button>
                <button type="button" @click="openConfig(item)">配置</button>
              </div>
            </div>
          </div>
          <button class="actress-cover" type="button" @click="openActress(item)">
            <img v-if="actressCover(item)" :class="{ 'is-work-cover': isLatestWorkCover(item) }" :src="actressCoverUrl(item)" alt="" loading="lazy">
            <span v-else>暂无封面</span>
          </button>
          <div class="actress-body">
            <h3>{{ actressName(item) }}</h3>
            <p>{{ item.id }} · {{ item.since_date || '未设置日期' }} 后</p>
            <p>{{ item.include_vr ? '包含 VR' : '不订阅 VR' }} · {{ item.poll_enabled === false ? '暂停轮询' : '正在轮询' }}</p>
            <div class="actress-actions">
              <BaseButton type="button" @click="openActress(item)">查看番号</BaseButton>
              <BaseButton variant="danger" type="button" :disabled="busyId === item.id" @click="removeActress(item)">取消</BaseButton>
            </div>
          </div>
        </BaseCard>
      </div>
    </section>

    <div v-if="configActress" class="modal-mask" @click.self="configActress = null">
      <BaseCard as="form" class="config-modal" @submit.prevent="saveActressConfig">
        <button class="modal-close" type="button" @click="configActress = null">x</button>
        <h2>编辑 {{ configActress.name || configActress.id }}</h2>
        <label>
          <span>限制日期</span>
          <input v-model="actressForm.since_date" type="date">
        </label>
        <BaseSwitch v-model="actressForm.poll_enabled" label="启用轮询" />
        <BaseSwitch v-model="actressForm.include_vr" label="订阅 VR" />
        <div class="modal-actions">
          <BaseButton type="button" @click="configActress = null">取消</BaseButton>
          <BaseButton variant="primary" type="submit" :disabled="busyId === configActress.id">保存</BaseButton>
        </div>
      </BaseCard>
    </div>

    <div v-if="subscribeLatestTarget" class="modal-mask" @click.self="subscribeLatestTarget = null">
      <BaseCard as="form" class="config-modal" @submit.prevent="confirmSubscribeLatest">
        <button class="modal-close" type="button" @click="subscribeLatestTarget = null">x</button>
        <h2>一键订阅 {{ actressName(subscribeLatestTarget) }}</h2>
        <p class="modal-hint">默认只订阅限制日期之后新增的番号。</p>
        <label>
          <span>限制日期</span>
          <input v-model="subscribeLatestForm.since_date" type="date">
        </label>
        <BaseSwitch v-model="subscribeLatestForm.include_vr" label="订阅 VR" />
        <div class="modal-actions">
          <BaseButton type="button" @click="subscribeLatestTarget = null">取消</BaseButton>
          <BaseButton variant="primary" type="submit" :disabled="busyId === subscribeLatestTarget.id">确认订阅</BaseButton>
        </div>
      </BaseCard>
    </div>

    <div v-if="washTarget" class="modal-mask" @click.self="washTarget = null">
      <BaseCard as="section" class="wash-modal">
        <button class="modal-close" type="button" @click="washTarget = null">x</button>
        <h2>洗版 {{ washTarget.id }}</h2>
        <p>选择本次洗版目标，系统会加入对应的轮询任务。</p>
        <div class="wash-actions">
          <BaseButton variant="primary" type="button" :disabled="busyId === washTarget.id" @click="confirmWash('chinese')">洗版中文</BaseButton>
          <BaseButton type="button" :disabled="busyId === washTarget.id" @click="confirmWash('4k')">洗版 4K</BaseButton>
        </div>
      </BaseCard>
    </div>

    <MovieDetailDialog
      v-if="detailItem"
      :item="detailItem"
      @close="detailItem = null"
      @subscribe-av="downloadAv"
      @actor="openActress"
      @maker="openMaker"
      @recommend="openDetail"
    />
  </section>
</template>

<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useQuery } from '@tanstack/vue-query'
import MovieDetailDialog from '../components/MovieDetailDialog.vue'
import SubscriptionMovieCard from '../components/SubscriptionMovieCard.vue'
import { api, postJson } from '../lib/api'
import { imageProxyUrl } from '../lib/images'

const mainTab = ref('av')
const router = useRouter()
const avState = ref('pending')
const busyId = ref('')
const notice = ref('')
const downloadingPending = ref(false)
const cleanupBusy = ref(false)
const configActress = ref(null)
const detailItem = ref(null)
const actionMenuId = ref('')
const actressMenuId = ref('')
const washTarget = ref(null)
const subscribeLatestTarget = ref(null)
const actressProfiles = ref({})
const actressForm = reactive({ since_date: '', poll_enabled: true, include_vr: false })
const subscribeLatestForm = reactive({ since_date: '', include_vr: false })

const avQuery = useQuery({
  queryKey: ['subscriptions', 'av'],
  queryFn: () => api('/api/subscriptions/av'),
  staleTime: 300_000,
  refetchInterval: false
})

const actressQuery = useQuery({
  queryKey: ['subscriptions', 'actress'],
  queryFn: () => api('/api/subscriptions/actress'),
  staleTime: 300_000,
  refetchInterval: false
})

const isLoading = computed(() => avQuery.isLoading.value)
const isActressLoading = computed(() => actressQuery.isLoading.value)
const error = computed(() => avQuery.error.value || actressQuery.error.value)
const avItems = computed(() => sortBySubscribedAt(avQuery.data.value?.subscriptions || []))
const actressItems = computed(() => sortBySubscribedAt(actressQuery.data.value?.subscriptions || []))

function sortBySubscribedAt(items) {
  return [...items].sort((a, b) => Number(b?.subscribed_at || 0) - Number(a?.subscribed_at || 0))
}

const avStates = computed(() => [
  { key: 'pending', label: '订阅中', count: avItems.value.filter((item) => (item.status || 'pending') === 'pending').length },
  { key: 'done', label: '已完成', count: avItems.value.filter((item) => item.status === 'done').length },
  { key: 'in_library', label: '已入库', count: avItems.value.filter((item) => item.status === 'in_library').length },
  { key: 'wash_active', label: '洗版中', count: avItems.value.filter((item) => washActive(item)).length },
  { key: 'wash_completed', label: '洗版完成', count: avItems.value.filter((item) => washCompleted(item)).length }
])

const filteredAvs = computed(() => avItems.value.filter((item) => itemMatchesState(item, avState.value)))

function washStatus(item) {
  const wash = item?.wash && typeof item.wash === 'object' ? item.wash : null
  return String(wash?.status || '').toLowerCase()
}

function washActive(item) {
  return ['requested', 'downloading', 'error'].includes(washStatus(item))
}

function washCompleted(item) {
  return washStatus(item) === 'completed'
}

function itemMatchesState(item, state) {
  if (state === 'wash_active') return washActive(item)
  if (state === 'wash_completed') return washCompleted(item)
  return (item.status || 'pending') === state
}

watch(
  [actressItems, mainTab],
  ([items, tab]) => {
    if (tab === 'actress') hydrateActressProfiles(items)
  },
  { immediate: true }
)

function refetchAll() {
  avQuery.refetch()
  actressQuery.refetch()
}

function proxyImage(url, item = null, options = {}) {
  return imageProxyUrl(url, item, options)
}

function actorAssetId(item) {
  return `actor-${item?.id || item?.javdb_id || item?.dmm_name || item?.name || 'unknown'}`
}

function actressProfile(item) {
  return actressProfiles.value[item.id] || {}
}

function isBadProfileName(value) {
  const text = String(value || '').trim().toLowerCase()
  return !text || text.includes('404') || text.includes('页面未找到') || text.includes('page not found')
}

function actressName(item) {
  if (!item) return ''
  const profileName = actressProfile(item).name
  if (!isBadProfileName(profileName)) return profileName
  if (!isBadProfileName(item.name)) return item.name
  return item.id
}

function actressCover(item) {
  if (!item) return ''
  return item.cover || item.cover_url || item.avatar || item.image || item.photo || actressProfile(item).cover || item.latest_cover || ''
}

function actressCoverUrl(item) {
  const cover = actressCover(item)
  if (!cover) return ''
  if (isLatestWorkCover(item)) {
    return proxyImage(cover, { id: item.latest_av_id || item.id, cover }, { kind: 'cover', avId: item.latest_av_id || item.id })
  }
  return proxyImage(cover, null, { kind: 'actor', entityId: actorAssetId(item) })
}

function isLatestWorkCover(item) {
  const cover = actressCover(item)
  return !!cover && cover === item?.latest_cover
}

async function hydrateActressProfiles(items) {
  const missing = items
    .filter((item) => item?.id && !actressCover(item) && !actressProfiles.value[item.id])
    .slice(0, 12)

  if (!missing.length) return

  for (const item of missing) {
    try {
      const payload = await api(`/api/subscriptions/actress/${encodeURIComponent(item.id)}/profile`)
      const profile = payload.profile || {}
      if (profile.name || profile.cover) {
        actressProfiles.value = {
          ...actressProfiles.value,
          [item.id]: {
            name: isBadProfileName(profile.name) ? '' : (profile.name || ''),
            cover: profile.cover || ''
          }
        }
      }
    } catch {
      actressProfiles.value = {
        ...actressProfiles.value,
        [item.id]: { name: item.name || item.id, cover: '' }
      }
    }
  }
}

function actresses(item) {
  const raw = item.actresses || item.actress || []
  const list = Array.isArray(raw) ? raw : [raw]
  return list.flatMap((actor) => {
    if (typeof actor === 'string') return splitActorNames(actor).map((name) => ({ name, id: '' }))
    return { name: actor?.name || '', id: actor?.id || actor?.code || '' }
  }).filter((actor) => actor.name)
}

function splitActorNames(value) {
  const text = String(value || '').trim()
  if (!text) return []
  const parts = text.split(/\s*[、,|]\s*|\s{2,}/).map((item) => item.trim()).filter(Boolean)
  if (parts.length > 1) return parts
  const spaced = text.split(/\s+/).map((item) => item.trim()).filter(Boolean)
  if (spaced.length >= 2 && spaced.length <= 20 && spaced.every((item) => item.length >= 2 && item.length <= 16)) return spaced
  return [text]
}

function washMessage(item) {
  const wash = item.wash && typeof item.wash === 'object' ? item.wash : null
  if (!wash?.download_message) return ''
  return wash.download_message
}

function displayStatusNote(item) {
  const message = item?.download_message || washMessage(item) || ''
  if (!message) return ''
  if (String(message).includes('qBittorrent 已存在')) {
    return `${message}，等待后处理确认文件路径`
  }
  return message
}

function toggleActionMenu(id) {
  actionMenuId.value = actionMenuId.value === id ? '' : id
  actressMenuId.value = ''
}

function toggleActressMenu(id) {
  actressMenuId.value = actressMenuId.value === id ? '' : id
  actionMenuId.value = ''
}

function closeActionMenu() {
  actionMenuId.value = ''
  actressMenuId.value = ''
}

function openDetail(item) {
  closeActionMenu()
  detailItem.value = item
}

function openActress(item) {
  const id = item.id || item.code || ''
  const name = item.name || ''
  const query = {}
  if (id) query.actress_id = id
  if (name) query.actress_name = name
  router.push({ path: '/subscription-search', query })
}

function openMaker(link) {
  if (!link?.url) return
  router.push({ path: '/makers', query: { url: link.url, name: link.name || '' } })
}

async function runBusy(id, action, success) {
  busyId.value = id
  notice.value = ''
  try {
    await action()
    notice.value = success
    if (mainTab.value === 'av') await avQuery.refetch()
    else await actressQuery.refetch()
  } catch (error) {
    notice.value = error.message || '操作失败'
  } finally {
    busyId.value = ''
    closeActionMenu()
  }
}

function downloadAv(item) {
  runBusy(item.id, () => postJson(`/api/subscriptions/av/${encodeURIComponent(item.id)}/download`), `${item.id} 已执行下载检查`)
}

function setAvStatus(item, status) {
  const label = status === 'pending' ? '订阅中' : (status === 'done' ? '已完成' : '已入库')
  runBusy(item.id, () => postJson(`/api/subscriptions/av/${encodeURIComponent(item.id)}/status`, { status }), `${item.id} 已标记为${label}`)
}

function requestWash(item, mode = 'chinese') {
  const label = mode === '4k' ? '4K 洗版' : '中文洗版'
  return runBusy(item.id, () => postJson(`/api/subscriptions/av/${encodeURIComponent(item.id)}/wash`, { mode, status: 'requested' }), `${item.id} 已加入${label}轮询`)
}

function openWashDialog(item) {
  closeActionMenu()
  washTarget.value = item
}

async function confirmWash(mode) {
  if (!washTarget.value) return
  const item = washTarget.value
  await requestWash(item, mode)
  washTarget.value = null
}

function removeAv(item) {
  runBusy(item.id, () => api(`/api/subscriptions/av/${encodeURIComponent(item.id)}`, { method: 'DELETE' }), `${item.id} 已取消订阅`)
}

async function downloadPending() {
  downloadingPending.value = true
  notice.value = ''
  try {
    const result = await postJson('/api/subscriptions/av/download-pending')
    notice.value = `一键下载完成：发送 ${result.sent || 0}，未找到 ${result.not_found || 0}，失败 ${result.failed || 0}`
    await avQuery.refetch()
  } catch (error) {
    notice.value = error.message || '一键下载失败'
  } finally {
    downloadingPending.value = false
  }
}

async function cleanupDirtySubscriptions() {
  cleanupBusy.value = true
  notice.value = ''
  try {
    const preview = await postJson('/api/subscriptions/av/cleanup-dirty', { dry_run: true })
    const count = preview.result?.candidate_count || 0
    if (!count) {
      notice.value = '没有需要清理的历史脏订阅'
      return
    }
    const names = (preview.result?.candidates || []).slice(0, 5).map((item) => item.id).join(', ')
    const ok = window.confirm(`将清理 ${count} 条历史脏订阅${names ? `：${names}` : ''}。继续吗？`)
    if (!ok) return
    const result = await postJson('/api/subscriptions/av/cleanup-dirty', { dry_run: false })
    notice.value = `已清理 ${result.result?.removed_count || 0} 条历史脏订阅`
    await avQuery.refetch()
  } catch (error) {
    notice.value = error.message || '清理历史脏订阅失败'
  } finally {
    cleanupBusy.value = false
  }
}

function openConfig(item) {
  closeActionMenu()
  configActress.value = item
  actressForm.since_date = item.since_date || new Date().toISOString().slice(0, 10)
  actressForm.poll_enabled = item.poll_enabled !== false
  actressForm.include_vr = item.include_vr === true
}

function saveActressConfig() {
  if (!configActress.value) return
  const id = configActress.value.id
  runBusy(
    id,
    () => postJson(`/api/subscriptions/actress/${encodeURIComponent(id)}`, { ...actressForm }),
    `${configActress.value.name || id} 配置已保存`
  )
  configActress.value = null
}

function openSubscribeLatestDialog(item) {
  closeActionMenu()
  subscribeLatestTarget.value = item
  subscribeLatestForm.since_date = item.since_date || new Date().toISOString().slice(0, 10)
  subscribeLatestForm.include_vr = item.include_vr === true
}

async function confirmSubscribeLatest() {
  if (!subscribeLatestTarget.value) return
  const item = subscribeLatestTarget.value
  await runBusy(
    item.id,
    async () => {
      await postJson(`/api/subscriptions/actress/${encodeURIComponent(item.id)}`, {
        since_date: subscribeLatestForm.since_date,
        include_vr: subscribeLatestForm.include_vr,
        poll_enabled: item.poll_enabled !== false,
        name: actressName(item),
        cover: item.cover || item.cover_url || actressProfile(item).cover || '',
        latest_cover: item.latest_cover || '',
        latest_av_id: item.latest_av_id || '',
        latest_title: item.latest_title || '',
        latest_date: item.latest_date || ''
      })
      await postJson(`/api/subscriptions/actress/${encodeURIComponent(item.id)}/subscribe-latest`)
    },
    `${actressName(item)} 已执行一键订阅`
  )
  subscribeLatestTarget.value = null
}

function removeActress(item) {
  runBusy(item.id, () => api(`/api/subscriptions/actress/${encodeURIComponent(item.id)}`, { method: 'DELETE' }), `${item.name || item.id} 已取消订阅`)
}
</script>

<style scoped>
.subscriptions-view {
  display: grid;
  gap: 24px;
}

.top-tabs,
.state-tabs {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  border-bottom: 1px solid var(--mm-border);
}

.top-tabs button,
.state-tabs button {
  min-height: 44px;
  border: 0;
  border-bottom: 2px solid transparent;
  background: transparent;
  color: var(--mm-muted);
  font-weight: 500;
  cursor: pointer;
}

.top-tabs button {
  padding: 0 24px;
  font-size: 18px;
}

.state-tabs button {
  padding: 0 18px;
  font-size: 15px;
}

.top-tabs button.active,
.state-tabs button.active {
  border-bottom-color: var(--mm-primary);
  color: var(--mm-primary);
}

.top-tabs span,
.state-tabs span {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 22px;
  min-height: 22px;
  margin-left: 6px;
  border-radius: 9999px;
  background: var(--mm-surface);
  font-size: 12px;
}

.tab-panel {
  display: grid;
  gap: 18px;
}

.card-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(270px, 1fr));
  gap: 16px;
}

.more-menu {
  position: relative;
}

.more-trigger {
  display: grid;
  grid-template-columns: repeat(3, 4px);
  gap: 3px;
  place-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  border: 0;
  border-radius: 999px;
  background: transparent;
  appearance: none;
  cursor: pointer;
  filter: drop-shadow(0 1px 4px rgba(0, 0, 0, .42));
}

.more-trigger span {
  width: 4px;
  height: 4px;
  border-radius: 999px;
  background: #fff;
}

.more-trigger:hover,
.more-trigger:focus-visible {
  background: transparent;
  outline: 2px solid rgba(255, 255, 255, .72);
  outline-offset: 2px;
}

.more-panel {
  position: absolute;
  top: 40px;
  right: 0;
  z-index: 8;
  display: grid;
  min-width: 150px;
  overflow: hidden;
  border: 1px solid var(--mm-border);
  border-radius: 12px;
  background: var(--mm-card-bg);
  box-shadow: var(--mm-menu-shadow);
}

.more-panel button {
  min-height: 38px;
  padding: 0 14px;
  border: 0;
  background: var(--mm-card-bg);
  color: var(--mm-text);
  font-size: var(--mm-font-size-sm);
  text-align: left;
  cursor: pointer;
}

.more-panel button:hover {
  background: var(--mm-primary-soft);
  color: var(--mm-primary);
}

.actress-card {
  position: relative;
  overflow: hidden;
  box-shadow: none;
}

.actress-card .card-menu {
  position: absolute;
  top: 10px;
  right: 10px;
  z-index: 3;
}

.actress-cover img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.actress-cover img.is-work-cover {
  padding: 10px;
  object-fit: contain;
  background: var(--mm-image-bg);
}

.actress-body p {
  margin: 0;
  color: var(--mm-muted);
  font-size: 13px;
}

.actress-body h3 {
  margin: 0;
  color: var(--mm-text);
  font-size: 15px;
  font-weight: 500;
  line-height: 1.45;
}

.actress-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 16px;
}

.actress-cover {
  display: grid;
  place-items: center;
  width: 100%;
  aspect-ratio: 1 / 1;
  border: 0;
  background: var(--mm-surface);
  color: var(--mm-muted);
  cursor: pointer;
}

.actress-body {
  display: grid;
  gap: 8px;
  padding: 14px;
}

.actress-actions {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
  margin-top: 8px;
}

.empty {
  padding: 24px;
  color: var(--mm-muted);
}

.modal-mask {
  position: fixed;
  inset: 0;
  z-index: 40;
  display: grid;
  place-items: center;
  padding: 24px;
  background: rgba(34, 34, 34, .42);
}

.config-modal {
  position: relative;
  width: min(560px, 100%);
  padding: 28px;
}

.wash-modal {
  position: relative;
  display: grid;
  gap: 16px;
  width: min(460px, 100%);
  padding: 28px;
}

.wash-modal h2,
.wash-modal p {
  margin: 0;
}

.wash-modal h2 {
  color: var(--mm-text);
  font-size: 24px;
  font-weight: var(--mm-font-weight-semibold);
}

.wash-modal p {
  color: var(--mm-muted);
  line-height: 1.7;
}

.wash-actions {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
  margin-top: 8px;
}

.config-modal h2 {
  margin: 0 0 20px;
  font-size: 24px;
  font-weight: 600;
}

.modal-hint {
  margin: -8px 0 18px;
  color: var(--mm-muted);
  line-height: 1.6;
}

.config-modal label {
  display: grid;
  gap: 8px;
  margin-bottom: 16px;
}

.config-modal input[type="date"] {
  min-height: 44px;
  padding: 0 12px;
  border: 1px solid var(--mm-border);
  border-radius: 8px;
}

.check-row {
  display: flex !important;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.check-row input {
  width: 22px;
  height: 22px;
}

.modal-close {
  position: absolute;
  top: 18px;
  right: 18px;
  border: 0;
  background: transparent;
  color: var(--mm-muted);
  font-size: 28px;
  cursor: pointer;
}

.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}
</style>


