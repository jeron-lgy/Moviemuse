<template>
  <div class="modal-mask" @click.self="$emit('close')">
    <BaseCard as="form" class="subscribe-modal" @submit.prevent="submit">
      <button class="modal-close" type="button" @click="$emit('close')">×</button>
      <div class="modal-title-row">
        <img v-if="coverUrl" :src="coverUrl" alt="">
        <div>
          <h2>订阅 {{ item.id || item.code || '' }}</h2>
          <p>{{ item.title || item.name || item.id }}</p>
        </div>
      </div>
      <label v-for="field in filterFields" :key="field.key" class="check-row">
        <span>{{ field.label }}</span>
        <input v-model="filters[field.key]" type="checkbox">
      </label>
      <div class="size-row">
        <FormField label="最小体积(MB)">
          <input v-model="filters.min_size_mb" type="number" min="0">
        </FormField>
        <FormField label="最大体积(MB)">
          <input v-model="filters.max_size_mb" type="number" min="0">
        </FormField>
      </div>
      <div class="mode-group">
        <span>订阅模式</span>
        <label><input v-model="filters.subscription_mode" type="radio" value="strict"> 严格模式</label>
        <label><input v-model="filters.subscription_mode" type="radio" value="predownload"> 预下载模式</label>
      </div>
      <div class="modal-actions">
        <BaseButton type="button" @click="$emit('close')">取消</BaseButton>
        <BaseButton variant="primary" type="submit" :disabled="submitting">
          {{ submitting ? '处理中' : '确认' }}
        </BaseButton>
      </div>
    </BaseCard>
  </div>
</template>

<script setup>
import { reactive, watch } from 'vue'

const props = defineProps({
  item: {
    type: Object,
    required: true
  },
  coverUrl: {
    type: String,
    default: ''
  },
  submitting: {
    type: Boolean,
    default: false
  }
})

const emit = defineEmits(['close', 'confirm'])

const filterFields = [
  { key: 'only_chinese', label: '仅中文' },
  { key: 'only_uncensored', label: '仅无码' },
  { key: 'exclude_uncensored', label: '排除无码' },
  { key: 'only_free', label: '仅免费' },
  { key: 'only_uhd', label: '仅 UHD' },
  { key: 'exclude_uhd', label: '排除 UHD' }
]

const filters = reactive(defaultFilters())

watch(
  () => props.item,
  () => Object.assign(filters, defaultFilters()),
  { immediate: true }
)

function defaultFilters() {
  return {
    only_chinese: false,
    only_uncensored: false,
    exclude_uncensored: false,
    only_free: false,
    only_uhd: false,
    exclude_uhd: false,
    min_size_mb: '',
    max_size_mb: '',
    subscription_mode: 'strict'
  }
}

function submit() {
  emit('confirm', { ...filters })
}
</script>

<style scoped>
.modal-mask {
  position: fixed;
  inset: 0;
  z-index: 80;
  display: grid;
  place-items: center;
  padding: 24px;
  background: rgba(34, 34, 34, .42);
}

.subscribe-modal {
  position: relative;
  width: min(760px, 100%);
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
  grid-template-columns: 104px minmax(0, 1fr);
  gap: 18px;
  align-items: start;
  margin-bottom: 20px;
  padding-right: 34px;
}

.modal-title-row img {
  width: 104px;
  aspect-ratio: 2 / 3;
  border-radius: var(--mm-radius-sm);
  object-fit: cover;
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
  font-weight: var(--mm-font-weight-semibold);
}

.check-row input,
.mode-group input {
  width: 22px;
  height: 22px;
  accent-color: var(--mm-primary);
}

.size-row {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
  margin-top: 16px;
}

.mode-group {
  display: grid;
  gap: 12px;
  margin-top: 18px;
}

.mode-group > span {
  font-weight: var(--mm-font-weight-semibold);
}

.mode-group label {
  display: flex;
  align-items: center;
  gap: 10px;
}

.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 28px;
}

@media (max-width: 720px) {
  .modal-title-row,
  .size-row {
    grid-template-columns: 1fr;
  }
}
</style>
