<template>
  <section class="variant-panel" :class="{ accent }">
    <header>
      <div>
        <span>{{ eyebrow }}</span>
        <h3>{{ title }}</h3>
      </div>
      <em v-if="accent">试验配置</em>
    </header>

    <div class="form-grid">
      <FormField label="翻译风格" wide>
          <select :value="modelValue.openai_translation_style" @change="update('openai_translation_style', $event.target.value)">
          <option v-for="item in styleOptions" :key="item.value" :value="item.value">{{ item.label }}</option>
        </select>
        </FormField>
      <FormField label="语气强度">
          <select
          :value="modelValue.openai_style_intensity"
          :disabled="modelValue.openai_translation_style === 'faithful'"
          @change="update('openai_style_intensity', $event.target.value)"
        >
          <option v-for="item in intensityOptions" :key="item.value" :value="item.value">{{ item.label }}</option>
        </select>
        </FormField>
      <FormField label="上下文">
          <select :value="modelValue.openai_context_lines" @change="update('openai_context_lines', Number($event.target.value))">
          <option v-for="item in contextOptions" :key="item.value" :value="item.value">{{ item.label }}</option>
        </select>
        </FormField>
      <FormField label="术语偏好（可选）" wide>
          <textarea
          :value="modelValue.openai_glossary"
          rows="3"
          placeholder="原词 = 希望采用的中文表达"
          @input="update('openai_glossary', $event.target.value)"
        ></textarea>
        </FormField>
    </div>
  </section>
</template>

<script setup>
const props = defineProps({
  modelValue: { type: Object, required: true },
  eyebrow: { type: String, default: '' },
  title: { type: String, required: true },
  accent: { type: Boolean, default: false }
})

const emit = defineEmits(['update:modelValue'])

const styleOptions = [
  { label: '忠实直译', value: 'faithful' },
  { label: '成人自然', value: 'adult_natural' },
  { label: '挑逗润色', value: 'seductive' }
]
const intensityOptions = [
  { label: '克制', value: 'restrained' },
  { label: '中等', value: 'medium' },
  { label: '明显', value: 'strong' }
]
const contextOptions = [
  { label: '不使用', value: 0 },
  { label: '前后 2 行', value: 2 },
  { label: '前后 4 行', value: 4 }
]

function update(key, value) {
  emit('update:modelValue', { ...props.modelValue, [key]: value })
}
</script>

<style scoped>
.variant-panel {
  height: 100%;
  padding: 18px;
  border: 1px solid var(--mm-border);
  border-radius: 14px;
  background: var(--mm-card-bg);
}

.variant-panel.accent {
  border-color: var(--mm-primary);
  background: var(--mm-primary-soft);
}

header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 16px;
}

header span {
  color: var(--mm-muted);
  font-size: 12px;
  font-weight: 600;
}

header h3 {
  margin: 4px 0 0;
  color: var(--mm-text);
  font-size: 18px;
  font-weight: 650;
}

header em {
  padding: 4px 10px;
  border-radius: 999px;
  background: var(--mm-control-bg);
  color: var(--mm-primary);
  font-size: 12px;
  font-style: normal;
  font-weight: 600;
}

.form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
}

select,
textarea {
  min-height: 44px;
  width: 100%;
  padding: 0 12px;
  border: 1px solid var(--mm-border);
  border-radius: 8px;
  background: var(--mm-control-bg);
  color: var(--mm-text);
}

textarea {
  padding-top: 10px;
}
</style>
