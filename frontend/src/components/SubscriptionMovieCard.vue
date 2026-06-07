<template>
  <BaseCard as="article" class="subscription-movie-card" padding="none">
    <div v-if="$slots.menu" class="card-menu">
      <slot name="menu" />
    </div>

    <button class="poster" type="button" @click="emitDetail">
      <img v-if="coverUrl" :src="coverUrl" alt="" loading="lazy">
      <span v-else>暂无封面</span>
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

      <p v-if="statusNote" class="status-note">{{ statusNote }}</p>

      <div class="card-actions">
        <slot name="actions" />
      </div>
    </div>
  </BaseCard>
</template>

<script setup>
import { computed } from 'vue'
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
  }
})

const emit = defineEmits(['detail', 'poster', 'actor'])

const code = computed(() => props.item.id || props.item.code || props.item.name || '未知')
const date = computed(() => props.item.date || props.item.release_date || '未知日期')
const title = computed(() => props.item.title || props.item.name || props.item.id || '未命名作品')

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
  background: var(--mm-surface);
  color: var(--mm-muted);
  cursor: pointer;
  overflow: hidden;
}

.poster img {
  width: 100%;
  max-width: 100%;
  height: 100%;
  object-fit: cover;
  object-position: center;
}

.movie-body {
  display: grid;
  grid-template-rows: auto auto auto minmax(0, 1fr) auto;
  gap: 10px;
  max-width: 100%;
  min-width: 0;
  min-height: 178px;
  padding: 14px;
}

.code-line {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  min-width: 0;
}

.code-line strong {
  overflow: hidden;
  color: var(--mm-primary);
  font-weight: var(--mm-font-weight-semibold);
  text-overflow: ellipsis;
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
  background: #fff0f3;
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
}
</style>
