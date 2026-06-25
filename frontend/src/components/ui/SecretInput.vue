<template>
  <div class="mm-secret-input" :class="{ saved: isSavedPlaceholder }">
    <input
      v-bind="$attrs"
      :type="inputType"
      :value="displayValue"
      :placeholder="placeholderText"
      @input="onInput"
    >
    <span v-if="isSavedPlaceholder" class="mm-secret-saved">已保存</span>
    <button
      type="button"
      class="mm-secret-toggle"
      :aria-label="toggleLabel"
      :title="toggleLabel"
      @click="visible = !visible"
    >
      <EyeOff v-if="visible" :size="18" stroke-width="2" />
      <Eye v-else :size="18" stroke-width="2" />
    </button>
  </div>
</template>

<script setup>
import { computed, ref, useAttrs } from 'vue'
import { Eye, EyeOff } from '@lucide/vue'

defineOptions({ inheritAttrs: false })

const props = defineProps({
  modelValue: {
    type: [String, Number],
    default: ''
  },
  modelModifiers: {
    type: Object,
    default: () => ({})
  }
})

const emit = defineEmits(['update:modelValue'])
const attrs = useAttrs()
const visible = ref(false)
const SECRET_PLACEHOLDER = '********'

const normalizedValue = computed(() => String(props.modelValue ?? ''))
const isSavedPlaceholder = computed(() => normalizedValue.value.trim() === SECRET_PLACEHOLDER)
const displayValue = computed(() => {
  if (isSavedPlaceholder.value) return visible.value ? SECRET_PLACEHOLDER : ''
  return props.modelValue
})
const inputType = computed(() => (visible.value ? 'text' : 'password'))
const placeholderText = computed(() => {
  if (isSavedPlaceholder.value) return '已保存，输入新值可替换'
  return attrs.placeholder || ''
})
const toggleLabel = computed(() => {
  if (isSavedPlaceholder.value) return visible.value ? '隐藏已保存占位符' : '显示已保存占位符'
  return visible.value ? '隐藏内容' : '显示内容'
})

function onInput(event) {
  const next = props.modelModifiers.trim ? event.target.value.trim() : event.target.value
  visible.value = false
  emit('update:modelValue', next)
}
</script>

<style scoped>
.mm-secret-input {
  position: relative;
  width: 100%;
}

.mm-secret-input input {
  width: 100%;
  min-height: 44px;
  padding: 0 46px 0 14px;
  border: 1px solid var(--mm-border);
  border-radius: var(--mm-input-radius, 8px);
  background: var(--mm-control-bg);
  color: var(--mm-text);
  font: inherit;
}

.mm-secret-input.saved input {
  padding-right: 116px;
}

.mm-secret-saved {
  position: absolute;
  top: 50%;
  right: 46px;
  display: inline-flex;
  align-items: center;
  min-height: 24px;
  padding: 0 9px;
  border: 1px solid var(--mm-primary);
  border-radius: 999px;
  background: var(--mm-primary-soft);
  color: var(--mm-primary);
  font-size: var(--mm-font-size-sm);
  font-weight: var(--mm-font-weight-medium);
  line-height: 1;
  pointer-events: none;
  transform: translateY(-50%);
}

.mm-secret-toggle {
  position: absolute;
  top: 50%;
  right: 8px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  border: 0;
  border-radius: max(6px, calc(var(--mm-input-radius, 8px) - 4px));
  background: transparent;
  color: var(--mm-muted);
  cursor: pointer;
  transform: translateY(-50%);
}

.mm-secret-toggle svg {
  display: block;
}

.mm-secret-toggle:hover {
  background: var(--mm-surface);
  color: var(--mm-text);
}
</style>
