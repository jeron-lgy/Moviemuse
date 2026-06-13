<template>
  <Teleport to="body">
    <div class="movie-detail-mask" @click.self="emit('close')">
      <section class="movie-detail-panel" role="dialog" aria-modal="true" :aria-label="dialogTitle">
        <button class="detail-close" type="button" aria-label="关闭详情" @click="emit('close')">x</button>

        <div class="detail-hero">
          <div class="detail-cover">
            <img v-if="coverUrl" :src="proxyImage(coverUrl, mergedItem)" alt="" />
            <span v-else>暂无封面</span>
          </div>

          <div class="detail-summary">
            <p class="detail-kicker">{{ movieCode }}</p>
            <h2>{{ movieTitle }}</h2>

            <div class="detail-actions">
              <BaseButton variant="primary" type="button" @click.stop="emit('subscribe-av', mergedItem)">
                订阅番号
              </BaseButton>
              <BaseButton
                v-if="primaryActor"
                type="button"
                :disabled="isActorBusyOrSubscribed(primaryActor)"
                @click.stop.prevent="openActorSubscribe(primaryActor)"
              >
                {{ actorSubscribeLabel(primaryActor) }}
              </BaseButton>
              <BaseButton v-else-if="actors.length > 1 &amp;&amp; actors.length <= 2" type="button" :disabled="busyActor === '__all__'" @click.stop="subscribeAllActors">
                订阅全部女优
              </BaseButton>
              <BaseButton v-if="detailUrl" as="a" :href="detailUrl" target="_blank" rel="noreferrer">
                打开 JavDB
              </BaseButton>
            </div>

            <NoticeBanner v-if="errorMessage" tone="error">{{ errorMessage }}</NoticeBanner>
            <NoticeBanner v-else-if="successMessage">{{ successMessage }}</NoticeBanner>
            <NoticeBanner v-else-if="loading">正在读取 JavDB 详情...</NoticeBanner>

            <div class="info-grid">
              <div>
                <span>发售日期</span>
                <strong>{{ releaseDate }}</strong>
              </div>
              <div>
                <span>时长</span>
                <strong>{{ displayValue(detail.duration) }}</strong>
              </div>
              <div>
                <span>评分</span>
                <strong>{{ displayValue(detail.rating) }}</strong>
              </div>
              <div>
                <span>导演</span>
                <div class="chip-row" v-if="directors.length">
                  <button v-for="link in directors" :key="link.url || link.name" type="button" @click="openExternal(link)">
                    {{ link.name }}
                  </button>
                </div>
                <strong v-else>未知</strong>
              </div>
              <div>
                <span>发行商</span>
                <div class="chip-row" v-if="makers.length">
                  <button v-for="link in makers" :key="link.url || link.name" type="button" @click="emit('maker', link)">
                    {{ link.name }}
                  </button>
                </div>
                <strong v-else>未知</strong>
              </div>
              <div>
                <span>演员</span>
                <div class="actor-row" v-if="actors.length">
                  <span v-for="actor in actors" :key="actorKey(actor)" class="actor-pill">
                    <button class="actor-name-button" type="button" @click="emit('actor', actor)">{{ actor.name }}</button>
                    <button
                      class="actor-subscribe-button"
                      type="button"
                      :disabled="isActorBusyOrSubscribed(actor)"
                      @click.stop.prevent="openActorSubscribe(actor)"
                    >
                      {{ actorSubscribeLabel(actor) }}
                    </button>
                  </span>
                </div>
                <strong v-else>未知</strong>
              </div>
            </div>

            <div v-if="tags.length" class="tag-cloud">
              <button v-for="tag in tags" :key="tag.url || tag.name" type="button" @click="openExternal(tag)">
                {{ tag.name }}
              </button>
            </div>
          </div>
        </div>

        <section class="detail-section">
          <div class="section-head">
            <h3>预告</h3>
          </div>
          <video v-if="detail.trailer" controls :src="proxyMedia(detail.trailer, mergedItem)"></video>
          <BaseCard v-else class="empty-media">JavDB 当前没有提供预告</BaseCard>
        </section>

        <section class="detail-section">
          <div class="section-head">
            <h3>剧照</h3>
            <span>{{ screenshots.length }} 张</span>
          </div>
          <div v-if="screenshots.length" class="screenshot-grid">
            <a v-for="(shot, index) in screenshots" :key="shot" :href="shot" target="_blank" rel="noreferrer">
              <img :src="proxyImage(shot, null, { kind: 'screenshot', entityId: `${movieCode}-${index + 1}` })" alt="" loading="lazy" />
            </a>
          </div>
          <BaseCard v-else class="empty-media">暂无剧照</BaseCard>
        </section>

        <section class="detail-section">
          <div class="section-head">
            <h3>猜你喜欢</h3>
            <span>{{ recommendations.length }} 条</span>
          </div>
          <div v-if="recommendations.length" class="recommend-grid">
            <SubscriptionMovieCard
              v-for="movie in recommendations"
              :key="movie.url || movie.id || movie.title"
              :item="movie"
              :cover-url="proxyImage(movie.cover, movie)"
              :show-actors="false"
              @detail="emit('recommend', $event)"
            >
              <template #actions>
                <BaseButton type="button" @click.stop="emit('recommend', movie)">详情</BaseButton>
                <BaseButton variant="primary" type="button" @click.stop="emit('subscribe-av', movie)">订阅</BaseButton>
              </template>
            </SubscriptionMovieCard>
          </div>
          <BaseCard v-else class="empty-media">暂无推荐</BaseCard>
        </section>

      </section>

      <div v-if="actorSubscribeTarget" class="actor-subscribe-mask" @click.self="closeActorSubscribe">
        <BaseCard as="form" class="actor-subscribe-dialog" @submit.prevent="confirmActorSubscribe">
          <button class="modal-close" type="button" @click="closeActorSubscribe">x</button>
          <h3>订阅 {{ actorSubscribeTarget.name }}</h3>
          <p>默认只订阅今天之后新增的作品，不订阅 VR。确认后会写入女优订阅列表，并把最新番号扫描放到后台。</p>
          <label>
            限制日期
            <input v-model="actorSubscribeForm.since_date" type="date">
          </label>
          <BaseSwitch v-model="actorSubscribeForm.include_vr" label="订阅 VR" />
          <div class="modal-actions">
            <BaseButton type="button" @click="closeActorSubscribe">取消</BaseButton>
            <BaseButton variant="primary" type="submit" :disabled="busyActor === actorKey(actorSubscribeTarget)">
              {{ busyActor === actorKey(actorSubscribeTarget) ? '订阅中' : '确认订阅' }}
            </BaseButton>
          </div>
        </BaseCard>
      </div>
    </div>
  </Teleport>
</template>

<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { useQueryClient } from '@tanstack/vue-query'
import { api, postJson } from '../lib/api'
import { imageProxyUrl, mediaProxyUrl } from '../lib/images'
import SubscriptionMovieCard from './SubscriptionMovieCard.vue'
import { BaseButton, BaseCard, BaseSwitch, NoticeBanner } from './ui'

const props = defineProps({
  item: {
    type: Object,
    required: true
  }
})

const emit = defineEmits(['close', 'subscribe-av', 'actor', 'maker', 'recommend'])
const queryClient = useQueryClient()

const detail = ref({})
const loading = ref(false)
const errorMessage = ref('')
const successMessage = ref('')
const busyActor = ref('')
const actorSubscribeTarget = ref(null)
const subscribedActorKeys = ref(new Set())
const actorSubscribeForm = reactive({
  since_date: new Date().toISOString().slice(0, 10),
  include_vr: false
})

const detailUrl = computed(() => detail.value.url || props.item.url || '')
const coverUrl = computed(() => detail.value.cover || props.item.cover || '')
const movieCode = computed(() => detail.value.id || props.item.id || props.item.code || '未知番号')
const movieTitle = computed(() => detail.value.title || props.item.title || props.item.name || movieCode.value)
const dialogTitle = computed(() => `${movieCode.value} 详情`)
const releaseDate = computed(() => detail.value.release_date || detail.value.date || props.item.date || props.item.release_date || '未知')
const mergedItem = computed(() => ({ ...props.item, ...detail.value, id: movieCode.value }))
const actors = computed(() => normalizePeople(detail.value.actors?.length ? detail.value.actors : (detail.value.actresses?.length ? detail.value.actresses : (props.item.actresses || props.item.actress || []))))
const primaryActor = computed(() => actors.value.length === 1 ? actors.value[0] : null)
const directors = computed(() => normalizeLinks(detail.value.director))
const makers = computed(() => normalizeLinks(detail.value.maker))
const tags = computed(() => normalizeLinks(detail.value.tags))
const screenshots = computed(() => Array.isArray(detail.value.screenshots) ? detail.value.screenshots : [])
const recommendations = computed(() => Array.isArray(detail.value.recommendations) ? detail.value.recommendations : [])

watch(
  () => props.item?.url,
  () => loadDetail(),
  { immediate: true }
)

async function loadDetail() {
  detail.value = {}
  errorMessage.value = ''
  successMessage.value = ''
  if (!props.item?.url) return
  loading.value = true
  try {
    const payload = await api(`/api/subscriptions/av/detail?url=${encodeURIComponent(props.item.url)}`)
    detail.value = payload.detail || {}
  } catch (error) {
    errorMessage.value = error.message || '读取详情失败'
  } finally {
    loading.value = false
  }
}

function proxyImage(url, item = null, options = {}) {
  return imageProxyUrl(url, item, options)
}

function proxyMedia(url, item = null, options = {}) {
  return mediaProxyUrl(url, item, options)
}

function displayValue(value) {
  return value || '未知'
}

function normalizePeople(value) {
  const list = Array.isArray(value) ? value : (value ? [value] : [])
  return list.flatMap((actor) => {
    if (typeof actor === 'string') return splitActorNames(actor).map((name) => ({ name, id: '', url: '', cover: '' }))
    const url = actor?.url || ''
    const idFromUrl = String(url).match(/\/actors\/([A-Za-z0-9]+)$/)?.[1] || ''
    return {
      name: actor?.name || actor?.value || '',
      id: actor?.id || actor?.code || idFromUrl,
      url,
      cover: actor?.cover || '',
      source: actor?.source || '',
      dmm_name: actor?.dmm_name || actor?.name || actor?.value || ''
    }
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

function normalizeLinks(value) {
  if (!value) return []
  const raw = Array.isArray(value) ? value : (value.links || value.value || value)
  const list = Array.isArray(raw) ? raw : String(raw || '').split(',').map((name) => name.trim()).filter(Boolean)
  return list.map((item) => {
    if (typeof item === 'string') return { name: item, url: '' }
    return { name: item?.name || item?.value || '', url: item?.url || '' }
  }).filter((item) => item.name)
}

function actorKey(actor) {
  return actor.id || actor.url || actor.name
}

function actorIdentityKeys(actor) {
  const keys = new Set()
  const add = (value) => {
    const text = String(value || '').trim().toLowerCase()
    if (text) keys.add(text)
  }
  add(actor?.id)
  add(actor?.javdb_id)
  add(actor?.javlibrary_star_id)
  add(actor?.dmm_name)
  add(actor?.name)
  add(actor?.url)
  return keys
}

function isActorSubscribed(actor) {
  const known = subscribedActorKeys.value
  for (const key of actorIdentityKeys(actor)) {
    if (known.has(key)) return true
  }
  const current = queryClient.getQueryData(['subscriptions', 'actress'])
  const rows = Array.isArray(current?.subscriptions) ? current.subscriptions : []
  return rows.some((row) => {
    for (const key of actorIdentityKeys(row)) {
      if (actorIdentityKeys(actor).has(key)) return true
    }
    return false
  })
}

function isActorBusyOrSubscribed(actor) {
  return busyActor.value === actorKey(actor) || isActorSubscribed(actor)
}

function actorSubscribeLabel(actor) {
  if (busyActor.value === actorKey(actor)) return '订阅中'
  return isActorSubscribed(actor) ? '已订阅' : '订阅女优'
}

function actorPayload(actor) {
  return {
    id: actor.id || actor.name,
    name: actor.name,
    cover: actor.cover || '',
    source: actor.source || (actor.id ? 'javdb' : 'dmm'),
    javdb_id: actor.id || '',
    dmm_name: actor.dmm_name || actor.name,
    dmm_url: actor.url && actor.url.includes('dmm.co.jp') ? actor.url : '',
    since_date: actorSubscribeForm.since_date,
    poll_enabled: true,
    include_vr: actorSubscribeForm.include_vr
  }
}

function openActorSubscribe(actor) {
  if (isActorSubscribed(actor)) {
    successMessage.value = `${actor.name} 已在女优订阅列表里`
    return
  }
  errorMessage.value = ''
  successMessage.value = ''
  actorSubscribeTarget.value = actor
  actorSubscribeForm.since_date = new Date().toISOString().slice(0, 10)
  actorSubscribeForm.include_vr = false
}

function closeActorSubscribe() {
  actorSubscribeTarget.value = null
}

async function confirmActorSubscribe() {
  if (!actorSubscribeTarget.value) return
  await subscribeActress(actorSubscribeTarget.value)
}

async function subscribeActress(actor) {
  const key = actorKey(actor)
  busyActor.value = key
  errorMessage.value = ''
  successMessage.value = ''
  try {
    const payload = await postJson('/api/subscriptions/actress', actorPayload(actor))
    updateActressSubscriptionCache(payload.subscription)
    markActorSubscribed(payload.subscription || actor)
    successMessage.value = `${actor.name} 已加入女优订阅，后台扫描已入队`
    closeActorSubscribe()
  } catch (error) {
    errorMessage.value = error.message || '订阅女优失败'
  } finally {
    busyActor.value = ''
  }
}

async function subscribeAllActors() {
  busyActor.value = '__all__'
  errorMessage.value = ''
  successMessage.value = ''
  try {
    for (const actor of actors.value) {
      if (isActorSubscribed(actor)) continue
      const payload = await postJson('/api/subscriptions/actress', actorPayload(actor))
      updateActressSubscriptionCache(payload.subscription)
      markActorSubscribed(payload.subscription || actor)
    }
    successMessage.value = '女优订阅已写入，后台扫描已入队'
    closeActorSubscribe()
  } catch (error) {
    errorMessage.value = error.message || '订阅女优失败'
  } finally {
    busyActor.value = ''
  }
}

function markActorSubscribed(actor) {
  const next = new Set(subscribedActorKeys.value)
  for (const key of actorIdentityKeys(actor)) next.add(key)
  subscribedActorKeys.value = next
}

function updateActressSubscriptionCache(item) {
  if (!item?.id) return
  const key = ['subscriptions', 'actress']
  queryClient.setQueryData(key, (current) => {
    const rows = Array.isArray(current?.subscriptions) ? current.subscriptions : []
    return {
      ...(current || {}),
      subscriptions: [item, ...rows.filter((row) => row?.id !== item.id)]
    }
  })
  queryClient.invalidateQueries({ queryKey: key })
}

function openExternal(link) {
  if (link.url) window.open(link.url, '_blank', 'noopener,noreferrer')
}
</script>

<style scoped>
.movie-detail-mask {
  position: fixed;
  inset: 0;
  z-index: 3000;
  display: flex;
  align-items: flex-start;
  justify-content: center;
  overflow: auto;
  padding: 32px;
  background: rgba(24, 24, 27, .46);
}

.movie-detail-panel {
  position: relative;
  z-index: 1;
  width: min(1200px, 100%);
  margin: 0 auto;
  padding: 28px;
  border: 1px solid var(--mm-border);
  border-radius: var(--mm-radius-lg);
  background: var(--mm-bg, #fff);
  box-shadow: 0 28px 90px rgba(0, 0, 0, .24);
}

.detail-close {
  position: absolute;
  top: 18px;
  right: 18px;
  z-index: 2;
  width: 40px;
  height: 40px;
  border: 0;
  border-radius: 999px;
  background: #fff;
  color: var(--mm-muted);
  font-size: 22px;
  cursor: pointer;
}

.detail-hero {
  display: grid;
  grid-template-columns: minmax(260px, 390px) minmax(0, 1fr);
  gap: 28px;
  padding-right: 48px;
}

.detail-cover {
  display: grid;
  place-items: center;
  align-self: start;
  min-height: 320px;
  border-radius: var(--mm-radius-md);
  background: var(--mm-surface);
  color: var(--mm-muted);
  overflow: hidden;
}

.detail-cover img {
  width: 100%;
  height: auto;
  max-height: 520px;
  object-fit: contain;
}

.detail-summary {
  display: grid;
  align-content: start;
  gap: 18px;
  min-width: 0;
}

.detail-kicker {
  margin: 0;
  color: var(--mm-primary);
  font-size: 30px;
  font-weight: var(--mm-font-weight-semibold);
  line-height: 1;
}

h2,
h3 {
  margin: 0;
  color: var(--mm-text);
  font-weight: var(--mm-font-weight-semibold);
}

h2 {
  font-size: 22px;
  line-height: 1.55;
}

.detail-actions,
.chip-row,
.tag-cloud,
.actor-row {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}

.info-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px 22px;
}

.info-grid > div {
  display: grid;
  gap: 8px;
  min-height: 72px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--mm-border);
}

.info-grid span,
.section-head span {
  color: var(--mm-muted);
  font-size: var(--mm-font-size-sm);
}

.info-grid strong {
  color: var(--mm-text);
  font-weight: var(--mm-font-weight-medium);
}

.chip-row button,
.tag-cloud button,
.actor-pill {
  min-height: 28px;
  border: 0;
  border-radius: 999px;
  background: #fff0f3;
  color: var(--mm-primary);
  font-size: var(--mm-font-size-sm);
  font-weight: var(--mm-font-weight-medium);
}

.chip-row button,
.tag-cloud button {
  padding: 0 10px;
  cursor: pointer;
}

.actor-pill {
  display: inline-flex;
  align-items: center;
  overflow: hidden;
}

.actor-pill button {
  min-height: 28px;
  padding: 0 10px;
  border: 0;
  background: transparent;
  color: inherit;
  font: inherit;
  cursor: pointer;
}

.actor-subscribe-button {
  border-radius: 999px !important;
  background: var(--mm-primary) !important;
  color: #fff !important;
  box-shadow: 0 8px 22px rgba(255, 56, 92, .18);
}

.actor-subscribe-button:disabled {
  opacity: .7;
  cursor: wait;
}

.detail-section {
  display: grid;
  gap: 14px;
  margin-top: 30px;
}

.section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

video {
  width: min(860px, 100%);
  border-radius: var(--mm-radius-md);
  background: #000;
}

.screenshot-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 14px;
}

.screenshot-grid a {
  overflow: hidden;
  border-radius: var(--mm-radius-sm);
  background: var(--mm-surface);
  aspect-ratio: 4 / 3;
}

.screenshot-grid img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.recommend-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 16px;
}

.empty-media {
  padding: 28px;
  color: var(--mm-muted);
  text-align: center;
}

.actor-subscribe-mask {
  position: fixed;
  inset: 0;
  z-index: 3;
  display: grid;
  place-items: center;
  padding: 24px;
  background: rgba(24, 24, 27, .36);
}

.actor-subscribe-dialog {
  position: relative;
  display: grid;
  gap: 16px;
  width: min(520px, 100%);
  padding: 28px;
}

.actor-subscribe-dialog h3,
.actor-subscribe-dialog p {
  margin: 0;
}

.actor-subscribe-dialog p {
  color: var(--mm-muted);
  line-height: 1.7;
}

.actor-subscribe-dialog label {
  display: grid;
  gap: 8px;
  color: var(--mm-text);
  font-weight: var(--mm-font-weight-medium);
}

.actor-subscribe-dialog input[type="date"] {
  min-height: 44px;
  padding: 0 12px;
  border: 1px solid var(--mm-border);
  border-radius: var(--mm-radius-sm);
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
}

@media (max-width: 980px) {
  .recommend-grid,
  .screenshot-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 820px) {
  .movie-detail-mask {
    padding: 16px;
  }

  .movie-detail-panel {
    padding: 18px;
  }

  .detail-hero,
  .info-grid {
    grid-template-columns: 1fr;
  }

  .detail-hero {
    padding-right: 0;
  }

  .recommend-grid,
  .screenshot-grid {
    grid-template-columns: 1fr;
  }
}
</style>

