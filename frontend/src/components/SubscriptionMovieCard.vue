<template>
  <BaseCard as="article" :class="cardClasses" padding="none">
    <div v-if="$slots.menu" class="card-menu">
      <slot name="menu" />
    </div>

    <button :class="posterClasses" type="button" @click="emitDetail">
      <img v-if="coverUrl && !imageFailed" :src="coverUrl" alt="" loading="lazy" @error="imageFailed = true">
      <span v-else>{{ coverPlaceholder }}</span>
    </button>

    <div class="movie-body">
      <div class="code-line">
        <strong>{{ code }}</strong>
        <span>{{ date }}</span>
      </div>

      <h3>{{ title }}</h3>

      <div v-if="showActors && actors.length" class="tag-line">
        <button
          v-for="actor in actors"
          :key="actor.id || actor.name"
          type="button"
          @click.stop="$emit('actor', actor)"
        >
          {{ actor.name }}
        </button>
      </div>

      <p v-if="displayNote" class="status-note">{{ displayNote }}</p>

      <div class="card-actions">
        <slot name="actions" />
      </div>
    </div>
  </BaseCard>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { BaseCard } from './ui'

const props = defineProps({
  item: {
    type: Object,
    required: true
  },
  coverUrl: {
    type: String,
    default: ''
  },
  coverPlaceholder: {
    type: String,
    default: '暂无封面'
  },
  actors: {
    type: Array,
    default: () => []
  },
  showActors: {
    type: Boolean,
    default: true
  },
  statusNote: {
    type: String,
    default: ''
  },
  variant: {
    type: String,
    default: ''
  },
  posterFit: {
    type: String,
    default: 'contain'
  }
})

const emit = defineEmits(['detail', 'poster', 'actor'])
const imageFailed = ref(false)

const code = computed(() => props.item.id || props.item.code || props.item.name || '未知')
const date = computed(() => props.item.date || props.item.release_date || '未知日期')
const title = computed(() => props.item.title || props.item.name || props.item.id || '未命名作品')

const cardClasses = computed(() => ['subscription-movie-card', props.variant ? `is-${props.variant}` : ''])
const posterClasses = computed(() => ['poster', props.posterFit === 'cover' ? 'is-cover' : 'is-contain'])
const displayNote = computed(() => props.statusNote || sourceNote(props.item))

watch(() => props.coverUrl, () => {
  imageFailed.value = false
})

function sourceNote(item) {
  const chain = Array.isArray(item?.source_chain) && item.source_chain.length
    ? item.source_chain.join(' + ')
    : (item?.source || '')
  const reasonMap = {
    primary_label: '主厂牌匹配',
    actress_seed: '女优锚点匹配',
    exact_av_id: '番号精确匹配',
    sqlite_cache: '本地缓存',
    javdb_maker_fallback: 'JavDB 兜底',
    javdb_av_fallback: 'JavDB 兜底'
  }
  const reason = reasonMap[item?.match_reason] || item?.match_reason || ''
  const confidence = item?.confidence ? ` · ${item.confidence}` : ''
  if (!chain && !reason) return ''
  return [chain, reason].filter(Boolean).join(' · ') + confidence
}

function emitDetail() {
  emit('detail', props.item)
  emit('poster', props.item)
}
</script>

<style scoped>
.subscription-movie-card {
  position: relative;
  overflow: hidden;
  display: grid;
  grid-template-rows: auto 1fr;
  width: 100%;
  min-width: 0;
  min-height: 338px;
  transition: transform .18s ease, box-shadow .18s ease, border-color .18s ease;
}

.subscription-movie-card:hover {
  transform: translateY(-2px);
  border-color: rgba(255, 56, 92, .34);
  box-shadow: 0 10px 30px rgba(0, 0, 0, .10);
}

.subscription-movie-card.is-compact {
  min-height: 0;
}

.card-menu {
  position: absolute;
  top: 10px;
  right: 10px;
  z-index: 3;
}

.poster {
  display: grid;
  place-items: center;
  width: 100%;
  max-width: 100%;
  min-width: 0;
  aspect-ratio: 3 / 2;
  padding: 0;
  border: 0;
  background: var(--mm-image-bg);
  color: var(--mm-muted);
  cursor: pointer;
  overflow: hidden;
}

.poster img {
  width: 100%;
  max-width: 100%;
  height: 100%;
  object-fit: contain;
  object-position: center;
  background: var(--mm-image-bg);
}

.poster.is-cover img {
  object-fit: cover;
}

.poster.is-contain img {
  object-fit: contain;
}

.movie-body {
  display: grid;
  grid-template-rows: auto minmax(44px, auto) minmax(0, auto) minmax(0, 1fr) auto;
  gap: 10px;
  max-width: 100%;
  min-width: 0;
  min-height: 178px;
  padding: 14px;
}

.subscription-movie-card.is-compact .movie-body {
  min-height: 166px;
  padding: 12px;
}

.subscription-movie-card.is-compact h3 {
  min-height: 42px;
  font-size: 13px;
  line-height: 1.5;
}

.subscription-movie-card.is-compact .card-actions {
  gap: 8px;
}

.code-line {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  min-width: 0;
}

.code-line strong {
  flex: none;
  overflow: visible;
  color: var(--mm-primary);
  font-weight: var(--mm-font-weight-semibold);
  text-overflow: clip;
  white-space: nowrap;
}

.code-line span {
  flex: none;
  color: var(--mm-muted);
  font-size: var(--mm-font-size-sm);
}

h3 {
  min-height: 44px;
  margin: 0;
  overflow: hidden;
  color: var(--mm-text);
  font-size: var(--mm-font-size-body);
  font-weight: var(--mm-font-weight-semibold);
  line-height: 1.55;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

.tag-line {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  min-height: 26px;
}

.tag-line button {
  min-height: 24px;
  padding: 0 9px;
  border: 0;
  border-radius: 999px;
  background: var(--mm-primary-soft);
  color: var(--mm-primary);
  font-size: var(--mm-font-size-sm);
  font-weight: var(--mm-font-weight-medium);
  cursor: pointer;
}

.status-note {
  margin: 0;
  overflow: hidden;
  color: var(--mm-muted);
  font-size: var(--mm-font-size-sm);
  line-height: 1.5;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

.card-actions {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
  padding-top: 4px;
  margin-top: auto;
}

.card-actions:deep(.mm-button) {
  width: 100%;
  min-height: 34px;
  padding-inline: 10px;
}
</style>
