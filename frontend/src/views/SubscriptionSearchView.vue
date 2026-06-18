<template>
  <section class="search-view" @click="closeMenus">
    <PageHeader kicker="订阅管理" :title="pageTitle" :description="pageDescription" />

    <BaseCard as="form" class="search-bar"  @submit.prevent="runSearch">
      <select v-model="searchType" aria-label="搜索类型">
        <option value="av">番号</option>
        <option value="actress">女优</option>
      </select>
      <input v-model.trim="keyword" type="search" placeholder="输入番号（如 SNOS-250）或女优名..." autocomplete="off">
      <BaseButton variant="primary"  type="submit" :disabled="loading || !keyword">{{ loading ? '搜索中' : '搜索' }}</BaseButton>
    </BaseCard>

    <NoticeBanner v-if="errorMessage" tone="error">{{ errorMessage }}</NoticeBanner>

    <section class="result-section">
      <div class="section-head">
        <h2>{{ resultTitle }}</h2>
        <span>{{ javdbResults.length }} 条</span>
      </div>
      <BaseCard as="div" class="empty" v-if="loading" >正在读取 JavDB...</BaseCard>
      <BaseCard as="div" class="empty" v-else-if="!javdbResults.length">暂无 JavDB 结果。</BaseCard>
      <div v-else class="card-grid">
        <SubscriptionMovieCard
          v-for="item in javdbResults"
          :key="item.url || item.id"
          :item="item"
          :cover-url="proxyImage(item.cover, item)"
          :actors="normalizedActresses(item)"
          @detail="openDetail"
          @actor="searchActress"
        >
          <template v-if="searchType === 'av' && isMovieSubscribed(item)" #menu>
            <div class="more-menu" @click.stop>
              <button class="more-trigger" type="button" aria-label="更多操作" @click.stop="toggleMovieMenu(item)">
                <span></span><span></span><span></span>
              </button>
              <div v-if="movieMenuId === movieKey(item)" class="more-panel" @click.stop>
                <button type="button" @click="openSubscribe(item, true)">重新订阅</button>
              </div>
            </div>
          </template>
          <template #actions>
              <SubscriptionHoverButton
                v-if="searchType === 'av' && isMovieSubscriptionCancelable(item)"
                :busy="busyMovie === movieKey(item)"
                @click.stop="cancelMovieSubscription(item)"
              />
              <BaseButton v-else-if="searchType === 'av'" variant="primary" type="button" :disabled="isMovieSubscribed(item)" @click.stop="openSubscribe(item)">
                {{ movieSubscribeLabel(item) }}
              </BaseButton>
              <SubscriptionHoverButton
                v-else-if="isActressSubscribed(item)"
                size="lg"
                :busy="busyActress === actressCancelKey(item)"
                @click.stop="cancelActressSubscription(item)"
              />
              <BaseButton variant="primary" size="lg" v-else type="button" @click.stop="openActressSubscribe(item)">
                {{ actressSubscribeLabel(item) }}
              </BaseButton>
              <BaseButton  type="button" @click.stop="openDetail(item)">详情</BaseButton>
          </template>
        </SubscriptionMovieCard>
      </div>
    </section>

    <section v-if="!actressMode" class="result-section">
      <div class="section-head">
        <h2>MTeam</h2>
        <span>{{ mteamResults.length }} 条</span>
      </div>
      <BaseCard as="div" class="empty" v-if="loading" >正在同步查询 MTeam...</BaseCard>
      <BaseCard as="div" class="empty" v-else-if="mteamMessage" >{{ mteamMessage }}</BaseCard>
      <BaseCard as="div" class="empty" v-else-if="!mteamResults.length">暂无匹配资源。</BaseCard>
      <BaseCard as="div" class="mteam-list" v-else >
        <div v-for="item in mteamResults" :key="item.id || item.url || item.title" class="mteam-row">
          <div>
            <strong>{{ item.title }}</strong>
            <p>{{ item.pubDate || item.created_at || item.size || '' }}</p>
          </div>
          <BaseButton as="a"  :href="item.url" target="_blank" rel="noreferrer">打开</BaseButton>
          <BaseButton variant="primary"  type="button" @click="downloadMteam(item)">下载</BaseButton>
        </div>
      </BaseCard>
    </section>

    <SubscribeAvDialog
      v-if="subscribeItem"
      :item="subscribeItem"
      :cover-url="proxyImage(subscribeItem.cover, subscribeItem)"
      :submitting="submitting"
      @close="subscribeItem = null"
      @confirm="confirmSubscribe"
    />

    <div v-if="actressSubscribeItem" class="modal-mask" @click.self="actressSubscribeItem = null">
      <BaseCard as="form" class="actress-subscribe-modal" @submit.prevent="confirmActressSubscribe">
        <button class="modal-close" type="button" @click="actressSubscribeItem = null">×</button>
        <div class="modal-title-row">
          <img v-if="actressCover(actressSubscribeItem)" :src="actressCoverUrl(actressSubscribeItem)" alt="">
          <span v-else class="cover-placeholder">暂无封面</span>
          <div>
            <h2>订阅女优 {{ actressSubscribeItem.name || actressSubscribeItem.title || actressSubscribeItem.id }}</h2>
            <p>默认只订阅限制日期之后新增的番号。</p>
          </div>
        </div>
        <FormField label="限制日期" hint="默认是今天，只处理这个日期之后新增的番号。">
          <input v-model="actressSubscribeForm.since_date" type="date">
        </FormField>
        <BaseSwitch v-model="actressSubscribeForm.include_vr" label="订阅 VR" />
        <div class="modal-actions">
          <BaseButton type="button" @click="actressSubscribeItem = null">取消</BaseButton>
          <BaseButton variant="primary" type="submit" :disabled="submitting">
            {{ submitting ? '处理中' : '确认订阅' }}
          </BaseButton>
        </div>
      </BaseCard>
    </div>

    <MovieDetailDialog
      v-if="detailItem"
      :item="detailItem"
      @close="detailItem = null"
      @subscribe-av="openSubscribe"
      @actor="searchActress"
      @maker="openMaker"
      @recommend="openDetail"
    />
  </section>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import { useRoute, useRouter } from 'vue-router'
import MovieDetailDialog from '../components/MovieDetailDialog.vue'
import SubscribeAvDialog from '../components/SubscribeAvDialog.vue'
import SubscriptionHoverButton from '../components/SubscriptionHoverButton.vue'
import SubscriptionMovieCard from '../components/SubscriptionMovieCard.vue'
import { BaseButton, BaseCard, BaseSwitch, FormField } from '../components/ui'
import { api, postJson } from '../lib/api'
import { imageProxyUrl } from '../lib/images'
import { avSubscribeLabel, isActressSubscribed as findActressSubscribed, normalizeAvId, subscribedActress as findSubscribedActress, subscribedAv } from '../lib/subscriptionStatus'

const route = useRoute()
const router = useRouter()
const queryClient = useQueryClient()
const keyword = ref(String(route.query.q || ''))
const searchType = ref(String(route.query.type || 'av'))
const loading = ref(false)
const errorMessage = ref('')
const results = ref([])
const mteam = ref(null)
const subscribeItem = ref(null)
const actressSubscribeItem = ref(null)
const detailItem = ref(null)
const submitting = ref(false)
const movieMenuId = ref('')
const busyMovie = ref('')
const busyActress = ref('')
const actressSubscribeForm = ref({ since_date: new Date().toISOString().slice(0, 10), include_vr: false })

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

const javdbResults = computed(() => Array.isArray(results.value) ? results.value : [])
const mteamResults = computed(() => Array.isArray(mteam.value?.results) ? mteam.value.results : [])
const avSubscriptions = computed(() => Array.isArray(avQuery.data.value?.subscriptions) ? avQuery.data.value.subscriptions : [])
const actressSubscriptions = computed(() => Array.isArray(actressQuery.data.value?.subscriptions) ? actressQuery.data.value.subscriptions : [])
const actressMode = computed(() => !!route.query.actress_id)
const pageTitle = computed(() => actressMode.value ? `${String(route.query.actress_name || keyword.value || '女优')} 的番号` : '搜索')
const pageDescription = computed(() => actressMode.value ? '查看该女优的 JavDB 番号列表，可继续订阅单个番号。' : '搜索番号和女优，JavDB 与 MTeam 结果会在同一页分区显示。')
const resultTitle = computed(() => actressMode.value ? '番号列表' : '搜索结果')
const mteamMessage = computed(() => {
  if (!mteam.value) return ''
  if (mteam.value.enabled === false) return 'MTeam 未启用，可在订阅设置里配置。'
  return mteam.value.message || ''
})

watch(
  () => route.query,
  () => loadRouteQuery(),
  { immediate: true }
)

function loadRouteQuery() {
  if (route.query.actress_id) {
    keyword.value = String(route.query.actress_name || route.query.q || route.query.actress_id || '')
    searchType.value = 'av'
    loadActressAvs(String(route.query.actress_id), String(route.query.actress_name || ''))
    return
  }
  keyword.value = String(route.query.q || keyword.value || '')
  searchType.value = String(route.query.type || searchType.value || 'av')
  if (keyword.value) runSearch(false)
}

async function runSearch(updateRoute = true) {
  if (!keyword.value) return
  loading.value = true
  errorMessage.value = ''
  try {
    if (updateRoute) router.replace({ path: '/subscription-search', query: { q: keyword.value, type: searchType.value } })
    const payload = await api(`/api/subscriptions/search?q=${encodeURIComponent(keyword.value)}&type=${encodeURIComponent(searchType.value)}&include_mteam=true`)
    if (payload.status === 'error') {
      errorMessage.value = payload.message || 'JavDB 当前不可用'
    }
    results.value = payload.results || []
    mteam.value = payload.mteam || null
  } catch (error) {
    errorMessage.value = error.message || '搜索失败'
    results.value = []
    mteam.value = null
  } finally {
    loading.value = false
  }
}

async function loadActressAvs(actressId, actressName = '') {
  if (!actressId) return
  loading.value = true
  errorMessage.value = ''
  mteam.value = null
  try {
    const payload = await api(`/api/subscriptions/actress/${encodeURIComponent(actressId)}/avs`)
    if (payload.status === 'error') {
      errorMessage.value = payload.message || 'JavDB 当前不可用'
    }
    results.value = (payload.results || []).map((item) => ({
      ...item,
      source_actress_id: actressId,
      source_actress_name: actressName
    }))
  } catch (error) {
    errorMessage.value = error.message || '读取女优番号失败'
    results.value = []
  } finally {
    loading.value = false
  }
}

function proxyImage(url, item = null, options = {}) {
  return imageProxyUrl(url, item, options)
}

function actorAssetId(item) {
  return `actor-${item?.id || item?.javdb_id || item?.dmm_name || item?.name || 'unknown'}`
}

function actressCover(item) {
  if (!item) return ''
  return item.cover || item.cover_url || item.avatar || item.image || item.photo || item.latest_cover || item.latest?.cover || ''
}

function actressCoverUrl(item) {
  const cover = actressCover(item)
  if (!cover) return ''
  if (cover === item?.latest_cover || cover === item?.latest?.cover) {
    const latestId = item.latest_av_id || item.latest?.id || item.id
    return proxyImage(cover, { id: latestId, cover }, { kind: 'cover', avId: latestId })
  }
  return proxyImage(cover, null, { kind: 'actor', entityId: actorAssetId(item) })
}

function normalizedActresses(item) {
  const raw = item.actresses || item.actress || []
  const list = Array.isArray(raw) ? raw : [raw]
  return list.map((actor) => {
    if (typeof actor === 'string') return { name: actor, id: '' }
    return { name: actor?.name || '', id: actor?.id || actor?.code || '' }
  }).filter((actor) => actor.name)
}

function searchActress(actor) {
  keyword.value = actor.name
  searchType.value = 'actress'
  runSearch()
}

function movieKey(item) {
  return normalizeAvId(item?.id || item?.code || '') || String(item?.url || '')
}

function isMovieSubscribed(item) {
  return !!subscribedAv(item, avSubscriptions.value)
}

function movieSubscribeLabel(item) {
  return avSubscribeLabel(item, avSubscriptions.value)
}

function isMovieSubscriptionCancelable(item) {
  return movieSubscribeLabel(item) === '已订阅'
}

function isActressSubscribed(item) {
  return findActressSubscribed(item, actressSubscriptions.value)
}

function subscribedActress(item) {
  return findSubscribedActress(item, actressSubscriptions.value)
}

function actressCancelKey(item) {
  return subscribedActress(item)?.id || item?.id || item?.name || item?.title || ''
}

function actressSubscribeLabel(item) {
  return isActressSubscribed(item) ? '已订阅' : '订阅女优'
}

function openSubscribe(item, force = false) {
  if (!force && isMovieSubscribed(item)) return
  closeMenus()
  subscribeItem.value = item
}

function openActressSubscribe(item) {
  if (isActressSubscribed(item)) return
  actressSubscribeItem.value = item
  actressSubscribeForm.value = {
    since_date: new Date().toISOString().slice(0, 10),
    include_vr: false
  }
}

async function confirmActressSubscribe() {
  if (!actressSubscribeItem.value) return
  const item = actressSubscribeItem.value
  submitting.value = true
  try {
    const payload = await postJson('/api/subscriptions/actress', {
      id: item.id,
      name: item.name || item.title,
      cover: actressCover(item),
      latest_cover: item.latest_cover || item.latest?.cover || '',
      latest_av_id: item.latest_av_id || item.latest?.id || '',
      latest_title: item.latest_title || item.latest?.title || '',
      latest_date: item.latest_date || item.latest?.date || item.latest?.release_date || '',
      since_date: actressSubscribeForm.value.since_date,
      poll_enabled: true,
      include_vr: actressSubscribeForm.value.include_vr
    })
    updateSubscriptionCache('actress', payload.subscription)
    await actressQuery.refetch()
    errorMessage.value = `${item.name || item.title} 已订阅`
    actressSubscribeItem.value = null
  } catch (error) {
    errorMessage.value = error.message || '订阅女优失败'
  } finally {
    submitting.value = false
  }
}

async function confirmSubscribe(filters) {
  if (!subscribeItem.value) return
  submitting.value = true
  try {
    const payload = await postJson('/api/subscriptions/av', {
      ...subscribeItem.value,
      filters: { ...filters },
      subscription_mode: filters.subscription_mode
    })
    updateSubscriptionCache('av', payload.subscription)
    await avQuery.refetch()
    errorMessage.value = `${subscribeItem.value.id} 已加入订阅`
    subscribeItem.value = null
  } catch (error) {
    errorMessage.value = error.message || '订阅失败'
  } finally {
    submitting.value = false
  }
}

function toggleMovieMenu(item) {
  const key = movieKey(item)
  movieMenuId.value = movieMenuId.value === key ? '' : key
}

function closeMenus() {
  movieMenuId.value = ''
}

async function cancelMovieSubscription(item) {
  const current = subscribedAv(item, avSubscriptions.value)
  const id = current?.id || item?.id
  const key = movieKey(item)
  if (!id || busyMovie.value) return
  busyMovie.value = key
  try {
    await api(`/api/subscriptions/av/${encodeURIComponent(id)}`, { method: 'DELETE' })
    removeSubscriptionCacheItem('av', id)
    await avQuery.refetch()
    errorMessage.value = `${id} 已取消订阅`
  } catch (error) {
    errorMessage.value = error.message || '取消订阅失败'
  } finally {
    busyMovie.value = ''
  }
}

async function cancelActressSubscription(item) {
  const current = subscribedActress(item)
  const id = current?.id || item?.id || item?.name || item?.title
  if (!id || busyActress.value) return
  busyActress.value = id
  try {
    await api(`/api/subscriptions/actress/${encodeURIComponent(id)}`, { method: 'DELETE' })
    removeSubscriptionCacheItem('actress', id)
    await actressQuery.refetch()
    errorMessage.value = `${current?.name || item?.name || item?.title || id} 已取消订阅`
  } catch (error) {
    errorMessage.value = error.message || '取消女优订阅失败'
  } finally {
    busyActress.value = ''
  }
}

function updateSubscriptionCache(type, item) {
  if (!item?.id) return
  const key = ['subscriptions', type]
  queryClient.setQueryData(key, (current) => {
    const rows = Array.isArray(current?.subscriptions) ? current.subscriptions : []
    const next = [item, ...rows.filter((row) => row?.id !== item.id)]
    return { ...(current || {}), subscriptions: next }
  })
  queryClient.invalidateQueries({ queryKey: key })
}

function removeSubscriptionCacheItem(type, id) {
  if (!id) return
  const key = ['subscriptions', type]
  queryClient.setQueryData(key, (current) => {
    const rows = Array.isArray(current?.subscriptions) ? current.subscriptions : []
    return { ...(current || {}), subscriptions: rows.filter((row) => row?.id !== id) }
  })
  queryClient.invalidateQueries({ queryKey: key })
}

async function downloadMteam(item) {
  try {
    await postJson('/api/mteam/download', item)
    errorMessage.value = 'MTeam 种子已推送'
  } catch (error) {
    errorMessage.value = error.message || 'MTeam 下载失败'
  }
}

function openDetail(item) {
  detailItem.value = item
}

function openMaker(link) {
  if (!link?.url) return
  detailItem.value = null
  router.push({ path: '/makers', query: { url: link.url, name: link.name || '' } })
}
</script>

<style scoped>
.search-view {
  display: grid;
  gap: 24px;
}

.eyebrow {
  color: var(--mm-primary) !important;
  font-size: 13px;
  font-weight: 500;
}

.search-bar {
  display: grid;
  grid-template-columns: 120px minmax(0, 1fr) auto;
  gap: 12px;
  padding: 12px;
  box-shadow: none;
}

.search-bar input,
.search-bar select {
  min-height: 44px;
  border: 1px solid var(--mm-border);
  border-radius: 8px;
  padding: 0 12px;
  color: var(--mm-text);
  background: var(--mm-control-bg);
}

.section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}

.section-head h2 {
  margin: 0;
  font-size: 22px;
  font-weight: 500;
}

.section-head span,
.empty {
  color: var(--mm-muted);
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

.more-panel {
  position: absolute;
  top: 40px;
  right: 0;
  z-index: 10;
  min-width: 124px;
  padding: 6px;
  border: 1px solid var(--mm-border);
  border-radius: var(--mm-radius-sm);
  background: var(--mm-card-bg);
  box-shadow: var(--mm-shadow-md);
}

.more-panel button {
  width: 100%;
  min-height: 34px;
  border: 0;
  border-radius: var(--mm-radius-sm);
  background: transparent;
  color: var(--mm-text);
  font-weight: 600;
  cursor: pointer;
}

.more-panel button:hover {
  background: var(--mm-primary-soft);
  color: var(--mm-primary);
}

.mteam-list {
  overflow: hidden;
  box-shadow: none;
}

.mteam-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 120px 120px;
  gap: 12px;
  align-items: center;
  padding: 14px;
  border-bottom: 1px solid var(--mm-border);
}

.mteam-row:last-child {
  border-bottom: 0;
}

.mteam-row strong {
  font-size: 15px;
  font-weight: 500;
}

.mteam-row p {
  margin: 4px 0 0;
  color: var(--mm-muted);
  font-size: 13px;
}

@media (max-width: 720px) {
  .search-bar,
  .mteam-row {
    grid-template-columns: 1fr;
  }
}

.modal-mask {
  position: fixed;
  inset: 0;
  z-index: 80;
  display: grid;
  place-items: center;
  padding: 24px;
  background: rgba(34, 34, 34, .42);
}

.actress-subscribe-modal {
  position: relative;
  width: min(560px, 100%);
  padding: 28px;
}

.modal-close {
  position: absolute;
  top: 16px;
  right: 18px;
  border: 0;
  background: transparent;
  color: var(--mm-muted);
  font-size: 28px;
  cursor: pointer;
}

.modal-title-row {
  display: grid;
  grid-template-columns: 88px minmax(0, 1fr);
  gap: 16px;
  align-items: start;
  margin-bottom: 20px;
  padding-right: 34px;
}

.modal-title-row img,
.cover-placeholder {
  width: 88px;
  aspect-ratio: 1 / 1;
  border-radius: var(--mm-radius-sm);
  object-fit: cover;
}

.cover-placeholder {
  display: grid;
  place-items: center;
  background: var(--mm-surface);
  color: var(--mm-muted);
}

.modal-title-row h2,
.modal-title-row p {
  margin: 0;
}

.modal-title-row h2 {
  font-size: var(--mm-font-size-section);
  font-weight: var(--mm-font-weight-semibold);
}

.modal-title-row p {
  margin-top: 8px;
  color: var(--mm-muted);
  line-height: 1.6;
}

.check-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
  min-height: 44px;
  margin-top: 14px;
  font-weight: var(--mm-font-weight-semibold);
}

.check-row input {
  width: 22px;
  height: 22px;
  accent-color: var(--mm-primary);
}

.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 28px;
}
</style>
