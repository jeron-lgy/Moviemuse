<template>
  <section class="rankings-view">
    <PageHeader
      kicker="订阅管理"
      title="DMM/FANZA 榜单"
      description="低频读取 DMM/FANZA 通贩榜单，只缓存榜单信息和封面 URL；封面按页面可见范围懒加载到本地。"
    >
      <template #actions>
        <BaseButton type="button" :disabled="loading" @click="loadRanking()">
          刷新页面
        </BaseButton>
        <BaseButton variant="primary" type="button" :disabled="loading" @click="loadRanking(true)">
          {{ loading ? '刷新中' : '强制更新榜单' }}
        </BaseButton>
      </template>
    </PageHeader>

    <BaseCard class="ranking-toolbar">
      <div class="ranking-tabs">
        <button
          v-for="tab in tabs"
          :key="tab.key"
          type="button"
          :class="{ active: activeTab === tab.key }"
          @click="selectTab(tab.key)"
        >
          {{ tab.label }}
        </button>
      </div>
      <p>
        数据源：DMM/FANZA 通贩 HTML 榜单。当前 {{ rankingLabel }}，
        {{ rankingMeta.cached ? '来自本地 SQLite 缓存' : '来自本次读取或服务缓存' }}。
        <span v-if="rankingMeta.fetched_at">读取时间：{{ formatTime(rankingMeta.fetched_at) }}</span>
      </p>
    </BaseCard>

    <NoticeBanner v-if="errorMessage" tone="error">{{ errorMessage }}</NoticeBanner>
    <NoticeBanner v-else-if="successMessage">{{ successMessage }}</NoticeBanner>

    <section class="result-section">
      <div class="section-head">
        <h2>{{ rankingLabel }}</h2>
        <span>{{ items.length }} 条</span>
      </div>

      <BaseCard as="div" class="empty" v-if="loading">正在读取 DMM/FANZA 榜单...</BaseCard>
      <BaseCard as="div" class="empty" v-else-if="!items.length">暂无榜单数据。</BaseCard>

      <div v-else-if="activeKind === 'actress'" class="actress-grid">
        <BaseCard v-for="item in items" :key="`${item.rank}-${item.name}`" as="article" class="actress-card" padding="none">
          <div class="rank-badge">{{ item.rank }}</div>
          <button class="actress-cover" type="button" @click="openActress(item)">
            <img v-if="item.cover" :src="actressCoverUrl(item)" alt="" loading="lazy">
            <span v-else>暂无头像</span>
          </button>
          <div class="actress-body">
            <div class="actress-title-row">
              <h3>{{ item.name }}</h3>
              <span>{{ item.product_count || 0 }} 部</span>
            </div>
            <p class="latest-title">{{ item.latest_title || '暂无最新作标题' }}</p>
            <p class="latest-date">{{ item.latest_release_date || item.latest_date || '未知日期' }}</p>
            <div class="card-actions">
              <BaseButton type="button" @click="openActress(item)">查看作品</BaseButton>
              <BaseButton variant="primary" type="button" :disabled="busyActress === item.name" @click="subscribeActress(item)">
                {{ busyActress === item.name ? '订阅中' : '订阅女优' }}
              </BaseButton>
            </div>
          </div>
        </BaseCard>
      </div>

      <div v-else class="movie-grid">
        <SubscriptionMovieCard
          v-for="item in items"
          :key="item.url || item.id"
          :item="item"
          :cover-url="proxyImage(item.cover, item)"
          :actors="normalizedActresses(item)"
          variant="compact"
          poster-fit="contain"
          @detail="openDetail"
          @actor="openActress"
        >
          <template #menu>
            <span class="rank-badge">{{ item.rank }}</span>
          </template>
          <template #actions>
            <BaseButton type="button" @click.stop="openDetail(item)">详情</BaseButton>
            <BaseButton variant="primary" type="button" @click.stop="openSubscribe(item)">订阅</BaseButton>
          </template>
        </SubscriptionMovieCard>
      </div>
    </section>

    <SubscribeAvDialog
      v-if="subscribeItem"
      :item="subscribeItem"
      :cover-url="proxyImage(subscribeItem.cover, subscribeItem)"
      :submitting="submitting"
      @close="subscribeItem = null"
      @confirm="confirmSubscribe"
    />

    <MovieDetailDialog
      v-if="detailItem"
      :item="detailItem"
      @close="detailItem = null"
      @subscribe-av="openSubscribe"
      @actor="openActress"
      @maker="openMaker"
      @recommend="openDetail"
    />
  </section>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useQueryClient } from '@tanstack/vue-query'
import { useRoute, useRouter } from 'vue-router'
import MovieDetailDialog from '../components/MovieDetailDialog.vue'
import SubscribeAvDialog from '../components/SubscribeAvDialog.vue'
import SubscriptionMovieCard from '../components/SubscriptionMovieCard.vue'
import { api, postJson } from '../lib/api'
import { imageProxyUrl } from '../lib/images'

const route = useRoute()
const router = useRouter()
const queryClient = useQueryClient()

const tabs = [
  { key: 'movie-week', label: '作品周榜', kind: 'movie', term: 'week' },
  { key: 'movie-monthly', label: '作品月榜', kind: 'movie', term: 'monthly' },
  { key: 'actress-monthly', label: '女优榜', kind: 'actress', term: 'monthly' }
]

const defaultTab = 'movie-week'
const activeTab = ref(normalizeTab(route.query.tab))
const rankingMeta = ref({ cached: false, fetched_at: 0 })
const items = ref([])
const loading = ref(false)
const errorMessage = ref('')
const successMessage = ref('')
const detailItem = ref(null)
const subscribeItem = ref(null)
const submitting = ref(false)
const busyActress = ref('')

const activeConfig = computed(() => tabs.find((tab) => tab.key === activeTab.value) || tabs[0])
const activeKind = computed(() => activeConfig.value.kind)
const rankingLabel = computed(() => activeConfig.value.label)

onMounted(() => loadRanking())

watch(
  () => route.query.tab,
  (value) => {
    const next = normalizeTab(value)
    if (next !== activeTab.value) {
      activeTab.value = next
      loadRanking()
    }
  }
)

function normalizeTab(value) {
  const key = String(value || defaultTab)
  return tabs.some((tab) => tab.key === key) ? key : defaultTab
}

function selectTab(key) {
  if (key === activeTab.value) return
  activeTab.value = key
  router.replace({ path: '/rankings', query: { tab: key } })
  loadRanking()
}

async function loadRanking(force = false) {
  loading.value = true
  errorMessage.value = ''
  successMessage.value = ''
  try {
    const params = new URLSearchParams({
      kind: activeConfig.value.kind,
      term: activeConfig.value.term,
      limit: '100'
    })
    if (force) params.set('force', 'true')
    const payload = await api(`/api/dmm/ranking?${params.toString()}`)
    const ranking = payload.ranking || {}
    rankingMeta.value = {
      cached: !!ranking.cached,
      fetched_at: Number(ranking.fetched_at || 0)
    }
    items.value = Array.isArray(ranking.items) ? ranking.items : []
  } catch (error) {
    errorMessage.value = error.message || '读取 DMM/FANZA 榜单失败'
    items.value = []
    rankingMeta.value = { cached: false, fetched_at: 0 }
  } finally {
    loading.value = false
  }
}

function proxyImage(url, item = null) {
  return imageProxyUrl(url, item)
}

function actressAssetId(item) {
  return `actor-${item?.dmm_name || item?.name || item?.id || 'unknown'}`
}

function actressCoverUrl(item) {
  return imageProxyUrl(item.cover, null, { kind: 'actor', entityId: actressAssetId(item) })
}

function normalizedActresses(item) {
  const raw = item.actresses || item.actors || []
  const list = Array.isArray(raw) ? raw : [raw]
  return list.map((actor) => {
    if (typeof actor === 'string') return { name: actor, id: '' }
    return { name: actor?.name || actor?.value || '', id: actor?.id || actor?.code || '', url: actor?.url || '' }
  }).filter((actor) => actor.name)
}

function openDetail(item) {
  detailItem.value = item
}

function openSubscribe(item) {
  subscribeItem.value = item
}

function openActress(item) {
  const name = item?.name || item?.dmm_name || item?.id
  if (!name) return
  const id = item?.id || item?.dmm_name || name
  detailItem.value = null
  router.push({ path: '/subscription-search', query: { actress_id: id, actress_name: name } })
}

function openMaker(link) {
  if (!link?.url) return
  detailItem.value = null
  router.push({ path: '/makers', query: { url: link.url, name: link.name || '' } })
}

async function confirmSubscribe(filters) {
  if (!subscribeItem.value) return
  submitting.value = true
  errorMessage.value = ''
  successMessage.value = ''
  try {
    const payload = await postJson('/api/subscriptions/av', {
      ...subscribeItem.value,
      filters: { ...filters },
      subscription_mode: filters.subscription_mode
    })
    updateSubscriptionCache('av', payload.subscription)
    successMessage.value = `${subscribeItem.value.id} 已加入订阅`
    subscribeItem.value = null
  } catch (error) {
    errorMessage.value = error.message || '订阅失败'
  } finally {
    submitting.value = false
  }
}

async function subscribeActress(item) {
  const name = item?.name || item?.dmm_name || item?.id
  if (!name) return
  busyActress.value = name
  errorMessage.value = ''
  successMessage.value = ''
  try {
    const payload = await postJson('/api/subscriptions/actress', {
      id: name,
      name,
      cover: item.cover || '',
      dmm_name: item.dmm_name || name,
      dmm_url: item.dmm_url || item.url || '',
      latest_cover: item.latest_cover || '',
      latest_av_id: item.latest_av_id || '',
      latest_title: item.latest_title || '',
      latest_date: item.latest_release_date || item.latest_date || '',
      since_date: new Date().toISOString().slice(0, 10),
      poll_enabled: true,
      include_vr: false
    })
    updateSubscriptionCache('actress', payload.subscription)
    successMessage.value = `${name} 已加入女优订阅，后台扫描已入队`
  } catch (error) {
    errorMessage.value = error.message || '订阅女优失败'
  } finally {
    busyActress.value = ''
  }
}

function updateSubscriptionCache(type, item) {
  if (!item?.id) return
  const key = ['subscriptions', type]
  queryClient.setQueryData(key, (current) => {
    const rows = Array.isArray(current?.subscriptions) ? current.subscriptions : []
    return {
      ...(current || {}),
      subscriptions: [item, ...rows.filter((row) => row?.id !== item.id)]
    }
  })
  queryClient.invalidateQueries({ queryKey: key })
}

function formatTime(value) {
  const seconds = Number(value || 0)
  if (!seconds) return ''
  return new Date(seconds * 1000).toLocaleString()
}
</script>

<style scoped>
.rankings-view {
  display: grid;
  gap: 24px;
}

.ranking-toolbar {
  display: grid;
  gap: 16px;
  padding: 18px;
}

.ranking-toolbar p {
  margin: 0;
  color: var(--mm-muted);
  line-height: 1.7;
}

.ranking-tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}

.ranking-tabs button {
  min-height: 42px;
  padding: 0 18px;
  border: 1px solid var(--mm-border);
  border-radius: 8px;
  background: var(--mm-control-bg);
  color: var(--mm-muted);
  font-weight: 600;
  cursor: pointer;
}

.ranking-tabs button.active {
  border-color: var(--mm-primary);
  background: var(--mm-primary-soft);
  color: var(--mm-primary);
}

.section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 14px;
}

.section-head h2 {
  margin: 0;
  font-size: 22px;
  font-weight: 600;
}

.section-head span {
  color: var(--mm-muted);
}

.movie-grid {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 16px;
}

.actress-grid {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 16px;
}

.actress-card {
  position: relative;
  overflow: hidden;
}

.rank-badge {
  display: inline-grid;
  place-items: center;
  min-width: 34px;
  height: 28px;
  padding: 0 10px;
  border-radius: 999px;
  background: var(--mm-primary);
  color: #fff;
  font-weight: 650;
  box-shadow: 0 8px 22px rgba(255, 56, 92, .18);
}

.actress-card > .rank-badge {
  position: absolute;
  top: 10px;
  left: 10px;
  z-index: 2;
}

.actress-cover {
  display: grid;
  place-items: center;
  width: 100%;
  aspect-ratio: 4 / 3;
  padding: 0;
  border: 0;
  background: var(--mm-surface);
  color: var(--mm-muted);
  cursor: pointer;
  overflow: hidden;
}

.actress-cover img {
  width: 100%;
  height: 100%;
  object-fit: contain;
}

.actress-body {
  display: grid;
  gap: 10px;
  padding: 14px;
}

.actress-title-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.actress-title-row h3 {
  margin: 0;
  color: var(--mm-text);
  font-size: 16px;
  font-weight: 650;
}

.actress-title-row span,
.latest-date {
  color: var(--mm-muted);
  font-size: var(--mm-font-size-sm);
}

.latest-title,
.latest-date {
  margin: 0;
}

.latest-title {
  min-height: 44px;
  overflow: hidden;
  color: var(--mm-text);
  font-weight: 600;
  line-height: 1.55;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

.card-actions {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
  margin-top: 4px;
}

.empty {
  padding: 48px;
  color: var(--mm-muted);
  text-align: center;
}

@media (max-width: 1500px) {
  .movie-grid,
  .actress-grid {
    grid-template-columns: repeat(4, minmax(0, 1fr));
  }
}

@media (max-width: 1100px) {
  .movie-grid,
  .actress-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}

@media (max-width: 760px) {
  .movie-grid,
  .actress-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .card-actions {
    grid-template-columns: 1fr;
  }
}
</style>
