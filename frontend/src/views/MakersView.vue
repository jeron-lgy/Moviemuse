<template>
  <section class="makers-view">
    <PageHeader kicker="订阅管理" :title="activeMaker?.name || '厂牌发售'" description="首屏读取厂牌最近 14 条作品，详情页再加载女优字段，让页面响应更轻。">
      <template #actions>
        <BaseButton variant="primary" type="button" :disabled="loading" @click="refreshCurrent">
          {{ loading ? '刷新中' : '刷新当前厂牌' }}
        </BaseButton>
      </template>
    </PageHeader>

    <BaseCard class="maker-toolbar" >
      <div class="maker-tabs">
        <button
          v-for="maker in makers"
          :key="maker.url"
          :class="{ active: maker.url === activeUrl }"
          type="button"
          @click="selectMaker(maker)"
        >
          {{ maker.name }}
        </button>
        <button class="add-maker" type="button" @click="showMakerDialog = true">+</button>
      </div>
      <form class="maker-search" @submit.prevent="searchByUrl">
        <input v-model.trim="manualUrl" type="url" placeholder="厂牌：javdb 链接，例如 https://javdb.com/makers/7R?f=download">
        <BaseButton  type="submit" :disabled="!manualUrl">读取</BaseButton>
      </form>
      <p>数据源：JavDB listing 缓存。演员信息只在详情页读取。</p>
    </BaseCard>

    <NoticeBanner v-if="errorMessage" tone="error">{{ errorMessage }}</NoticeBanner>
    <NoticeBanner v-else-if="successMessage" >{{ successMessage }}</NoticeBanner>

    <section class="result-section">
      <div class="section-head">
        <h2>最近发售</h2>
        <span>{{ movies.length }} 条</span>
      </div>
      <BaseCard as="div" class="empty" v-if="loading" >正在读取厂牌发售...</BaseCard>
      <BaseCard as="div" class="empty" v-else-if="!movies.length" >该厂牌暂时没有作品数据。</BaseCard>
      <div v-else class="card-grid">
        <SubscriptionMovieCard
          v-for="item in movies"
          :key="item.url || item.id"
          :item="item"
          :cover-url="proxyImage(item.cover)"
          :show-actors="false"
          @detail="openDetail"
        >
          <template #actions>
              <BaseButton  type="button" @click.stop="openDetail(item)">详情</BaseButton>
              <BaseButton variant="primary"  type="button" @click.stop="openSubscribe(item)">订阅</BaseButton>
          </template>
        </SubscriptionMovieCard>
      </div>
      <div v-if="!loading && hasMore" class="load-more">
        <BaseButton type="button" :disabled="loadingMore" @click="loadMore">
          {{ loadingMore ? '加载中...' : '加载更多' }}
        </BaseButton>
      </div>
    </section>

    <SubscribeAvDialog
      v-if="subscribeItem"
      :item="subscribeItem"
      :cover-url="proxyImage(subscribeItem.cover)"
      :submitting="submitting"
      @close="closeSubscribe"
      @confirm="confirmSubscribe"
    />

    <MovieDetailDialog
      v-if="detailItem"
      :item="detailItem"
      @close="detailItem = null"
      @subscribe-av="openSubscribe"
      @actor="openActress"
      @maker="openMakerFromDetail"
      @recommend="openDetail"
    />

    <div v-if="showMakerDialog" class="modal-mask" @click.self="showMakerDialog = false">
      <BaseCard as="form" class="maker-dialog"  @submit.prevent="addMaker">
        <button class="modal-close" type="button" @click="showMakerDialog = false">×</button>
        <h2>添加常驻厂牌</h2>
        <label>
          厂牌名称
          <input v-model.trim="newMaker.name" placeholder="例如 S1 NO.1 STYLE">
        </label>
        <label>
          JavDB 链接
          <input v-model.trim="newMaker.url" type="url" placeholder="https://javdb.com/makers/7R?f=download">
        </label>
        <div class="modal-actions">
          <BaseButton  type="button" @click="showMakerDialog = false">取消</BaseButton>
          <BaseButton variant="primary"  type="submit" :disabled="savingMaker || !newMaker.name || !newMaker.url">
            {{ savingMaker ? '保存中' : '保存' }}
          </BaseButton>
        </div>
      </BaseCard>
    </div>
  </section>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import { useRoute, useRouter } from 'vue-router'
import MovieDetailDialog from '../components/MovieDetailDialog.vue'
import SubscribeAvDialog from '../components/SubscribeAvDialog.vue'
import SubscriptionMovieCard from '../components/SubscriptionMovieCard.vue'
import { api, postJson } from '../lib/api'

const route = useRoute()
const router = useRouter()

const defaultMakers = [
  { name: 'S1 NO.1 STYLE', url: 'https://javdb.com/makers/7R?f=download' },
  { name: 'PRESTIGE', url: 'https://javdb.com/makers/6M?f=download' },
  { name: 'IDEA POCKET', url: 'https://javdb.com/makers/ZXX?f=download' },
  { name: 'Madonna', url: 'https://javdb.com/makers/zKW?f=download' },
  { name: 'SOD Create', url: 'https://javdb.com/makers/q6?f=download' }
]

const { data: settingsData, refetch: refetchSettings } = useQuery({
  queryKey: ['subscription-settings'],
  queryFn: () => api('/api/subscriptions/settings'),
  staleTime: 60_000
})

const activeUrl = ref('')
const manualUrl = ref('')
const movies = ref([])
const loading = ref(false)
const loadingMore = ref(false)
const listingLimit = ref(14)
const hasMore = ref(false)
const errorMessage = ref('')
const successMessage = ref('')
const subscribeItem = ref(null)
const detailItem = ref(null)
const submitting = ref(false)
const showMakerDialog = ref(false)
const savingMaker = ref(false)
const newMaker = reactive({ name: '', url: '' })

const makers = computed(() => {
  const rows = settingsData.value?.settings?.pinned_makers
  return Array.isArray(rows) && rows.length ? rows : defaultMakers
})
const activeMaker = computed(() => makers.value.find((maker) => maker.url === activeUrl.value) || makers.value[0])

onMounted(() => {
  applyRouteMaker()
})

watch(() => route.query.url, () => applyRouteMaker())

function selectMaker(maker) {
  activeUrl.value = maker.url
  manualUrl.value = maker.url
  listingLimit.value = 14
  router.replace({ path: '/makers', query: { url: maker.url, name: maker.name } })
  loadListing()
}

function searchByUrl() {
  activeUrl.value = manualUrl.value
  listingLimit.value = 14
  loadListing()
}

function refreshCurrent() {
  listingLimit.value = 14
  loadListing(true)
}

function loadMore() {
  listingLimit.value += 14
  loadListing(false, true)
}

function applyRouteMaker() {
  const url = String(route.query.url || '')
  if (url) {
    activeUrl.value = url
    manualUrl.value = url
  } else if (!activeUrl.value) {
    activeUrl.value = makers.value[0]?.url || defaultMakers[0].url
    manualUrl.value = activeUrl.value
  }
  listingLimit.value = 14
  loadListing()
}

async function loadListing(force = false, keepContent = false) {
  const url = activeUrl.value || manualUrl.value
  if (!url) return
  if (keepContent) {
    loadingMore.value = true
  } else {
    loading.value = true
  }
  errorMessage.value = ''
  successMessage.value = ''
  try {
    const requestedLimit = listingLimit.value + 1
    const params = new URLSearchParams({ url, limit: String(requestedLimit) })
    if (force) params.set('force', 'true')
    const payload = await api(`/api/javdb/listing?${params.toString()}`)
    const results = Array.isArray(payload.results) ? payload.results : []
    hasMore.value = results.length > listingLimit.value
    movies.value = results.slice(0, listingLimit.value)
  } catch (error) {
    if (keepContent) {
      listingLimit.value = Math.max(14, listingLimit.value - 14)
    } else {
      movies.value = []
      hasMore.value = false
    }
    errorMessage.value = error.message || '读取厂牌发售失败'
  } finally {
    if (keepContent) {
      loadingMore.value = false
    } else {
      loading.value = false
    }
  }
}

function proxyImage(url) {
  return url ? `/api/proxy/image?url=${encodeURIComponent(url)}` : ''
}

function openDetail(item) {
  detailItem.value = item
}

function openActress(actor) {
  if (!actor?.name && !actor?.id) return
  detailItem.value = null
  router.push({ path: '/subscription-search', query: { type: 'actress', q: actor.name || actor.id } })
}

function openMakerFromDetail(link) {
  if (!link?.url) return
  detailItem.value = null
  activeUrl.value = link.url
  manualUrl.value = link.url
  router.replace({ path: '/makers', query: { url: link.url, name: link.name || '' } })
  loadListing()
}

function openSubscribe(item) {
  subscribeItem.value = item
}

function closeSubscribe() {
  subscribeItem.value = null
}

async function confirmSubscribe(filters) {
  if (!subscribeItem.value) return
  submitting.value = true
  errorMessage.value = ''
  successMessage.value = ''
  try {
    await postJson('/api/subscriptions/av', {
      ...subscribeItem.value,
      filters: { ...filters },
      subscription_mode: filters.subscription_mode
    })
    successMessage.value = `${subscribeItem.value.id} 已加入订阅`
    subscribeItem.value = null
  } catch (error) {
    errorMessage.value = error.message || '订阅失败'
  } finally {
    submitting.value = false
  }
}

async function addMaker() {
  savingMaker.value = true
  errorMessage.value = ''
  successMessage.value = ''
  try {
    const next = [...makers.value.filter((maker) => maker.url !== newMaker.url), { name: newMaker.name, url: newMaker.url }]
    await postJson('/api/subscriptions/settings', { pinned_makers: next })
    await refetchSettings()
    activeUrl.value = newMaker.url
    manualUrl.value = newMaker.url
    newMaker.name = ''
    newMaker.url = ''
    showMakerDialog.value = false
    successMessage.value = '常驻厂牌已保存'
    await loadListing()
  } catch (error) {
    errorMessage.value = error.message || '保存厂牌失败'
  } finally {
    savingMaker.value = false
  }
}
</script>

<style scoped>
.makers-view {
  display: grid;
  gap: 24px;
}

.eyebrow {
  margin: 0 0 6px;
  color: var(--mm-primary);
  font-size: 13px;
  font-weight: 600;
}

h1,
h2,
h3,
p {
  margin: 0;
}

h1 {
  color: var(--mm-text);
  font-size: 30px;
  font-weight: 650;
  letter-spacing: -0.3px;
}
.maker-toolbar p {
  margin-top: 8px;
  color: var(--mm-muted);
  line-height: 1.7;
}

.maker-toolbar {
  display: grid;
  gap: 16px;
  padding: 18px;
}

.maker-tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}

.maker-tabs button {
  min-height: 42px;
  padding: 0 18px;
  border: 1px solid var(--mm-border);
  border-radius: 8px;
  background: #fff;
  color: var(--mm-muted);
  font-weight: 600;
  cursor: pointer;
}

.maker-tabs button.active {
  border-color: var(--mm-primary);
  background: #fff5f7;
  color: var(--mm-primary);
}

.maker-tabs .add-maker {
  width: 48px;
  padding: 0;
  color: var(--mm-primary);
  font-size: 24px;
}

.maker-search {
  display: none;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 12px;
}

.maker-search input,
.maker-dialog input {
  min-height: 44px;
  width: 100%;
  padding: 0 14px;
  border: 1px solid var(--mm-border);
  border-radius: 8px;
  background: #fff;
  color: var(--mm-text);
}

.section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 14px;
}

.section-head h2 {
  font-size: 22px;
  font-weight: 600;
}

.section-head span {
  color: var(--mm-muted);
}

.card-grid {
  display: grid;
  grid-template-columns: repeat(7, minmax(0, 1fr));
  gap: 16px;
}

.load-more {
  display: flex;
  justify-content: center;
  margin-top: 22px;
}

.empty {
  padding: 48px;
  color: var(--mm-muted);
  text-align: center;
}

.modal-mask {
  position: fixed;
  inset: 0;
  z-index: 50;
  display: grid;
  place-items: center;
  padding: 24px;
  background: rgba(34, 34, 34, .45);
}

.maker-dialog {
  position: relative;
  width: min(920px, 100%);
  max-height: min(86vh, 900px);
  overflow: auto;
  padding: 28px;
}

.maker-dialog {
  width: min(560px, 100%);
  display: grid;
  gap: 18px;
}

.maker-dialog label {
  display: grid;
  gap: 8px;
  color: var(--mm-text);
  font-weight: 600;
}

.modal-close {
  position: absolute;
  top: 14px;
  right: 14px;
  border: 0;
  background: transparent;
  color: var(--mm-muted);
  font-size: 24px;
  cursor: pointer;
}

.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 24px;
}

@media (max-width: 1500px) {
  .card-grid {
    grid-template-columns: repeat(5, minmax(0, 1fr));
  }
}

@media (max-width: 1100px) {
  .card-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}

@media (max-width: 760px) {
  .maker-search {
    grid-template-columns: 1fr;
  }

  .card-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

}
</style>
