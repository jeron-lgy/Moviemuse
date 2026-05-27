<template>
  <section class="variant-panel" :class="{ accent }">
    <header>
      <div>
        <span>{{ eyebrow }}</span>
        <h3>{{ title }}</h3>
      </div>
      <v-chip v-if="accent" size="small" color="primary" variant="tonal">试验配置</v-chip>
    </header>

    <v-row dense>
      <v-col cols="12">
        <div class="field-label">翻译风格</div>
        <v-select
          :model-value="modelValue.openai_translation_style"
          :items="styleOptions"
          item-title="label"
          item-value="value"
          @update:model-value="update('openai_translation_style', $event)"
        />
      </v-col>
      <v-col cols="6">
        <div class="field-label">语气强度</div>
        <v-select
          :model-value="modelValue.openai_style_intensity"
          :items="intensityOptions"
          item-title="label"
          item-value="value"
          :disabled="modelValue.openai_translation_style === 'faithful'"
          @update:model-value="update('openai_style_intensity', $event)"
        />
      </v-col>
      <v-col cols="6">
        <div class="field-label">上下文</div>
        <v-select
          :model-value="modelValue.openai_context_lines"
          :items="contextOptions"
          item-title="label"
          item-value="value"
          @update:model-value="update('openai_context_lines', $event)"
        />
      </v-col>
      <v-col cols="12">
        <div class="field-label">术语偏好（可选）</div>
        <v-textarea
          :model-value="modelValue.openai_glossary"
          rows="2"
          placeholder="原词 = 希望采用的中文表达"
          @update:model-value="update('openai_glossary', $event)"
        />
      </v-col>
    </v-row>
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
  border: 1px solid #dce7ef;
  border-radius: 8px;
  background: #fff;
}

.variant-panel.accent {
  border-color: #9adbd2;
  background: #fcfffe;
}

header {
  margin-bottom: 14px;
  display: flex;
  justify-content: space-between;
  align-items: start;
  gap: 12px;
}

header span {
  color: #718096;
  font-size: 12px;
  font-weight: 700;
}

header h3 {
  margin: 3px 0 0;
  color: #111827;
  font-size: 18px;
}

.field-label {
  margin-bottom: 7px;
  color: #667085;
  font-size: 13px;
  font-weight: 750;
}
</style>
