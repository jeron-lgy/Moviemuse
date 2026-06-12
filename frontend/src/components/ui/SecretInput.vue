<template>
  <div class="mm-secret-input">
    <input
      v-bind="$attrs"
      :type="visible ? 'text' : 'password'"
      :value="modelValue"
      @input="onInput"
    >
    <button
      type="button"
      class="mm-secret-toggle"
      :aria-label="visible ? '隐藏内容' : '显示内容'"
      :title="visible ? '隐藏内容' : '显示内容'"
      @click="visible = !visible"
    >
      <EyeOff v-if="visible" :size="18" stroke-width="2" />
      <Eye v-else :size="18" stroke-width="2" />
    </button>
  </div>
</template>

<script setup>
import { ref } from 'vue'
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
const visible = ref(false)

function onInput(event) {
  const next = props.modelModifiers.trim ? event.target.value.trim() : event.target.value
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
  background: #fff;
  color: var(--mm-text);
  font: inherit;
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
}

.mm-secret-toggle:hover {
  background: var(--mm-surface);
  color: var(--mm-text);
}
</style>
