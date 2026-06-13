<template>
  <section class="mm-page ui-preview">
    <PageHeader
      kicker="Design System"
      title="MovieMuse 公共组件预览"
      description="临时查看按钮、卡片、表单、标签、提示条和页面标题的统一视觉。"
    >
      <template #actions>
        <BaseThemeToggle />
        <BaseButton type="button">普通按钮</BaseButton>
        <BaseButton type="button" variant="primary">主按钮</BaseButton>
      </template>
    </PageHeader>

    <section class="preview-grid">
      <BaseCard as="article" class="preview-panel">
        <h2>按钮</h2>
        <p>按钮尺寸和状态统一从 token 读取，后续只改一处。</p>
        <div class="button-stack">
          <BaseButton type="button" size="sm">小按钮</BaseButton>
          <BaseButton type="button">默认按钮</BaseButton>
          <BaseButton type="button" size="lg">大按钮</BaseButton>
          <BaseButton type="button" variant="primary">确认操作</BaseButton>
          <BaseButton type="button" variant="danger">危险操作</BaseButton>
          <BaseButton type="button" variant="ghost">弱按钮</BaseButton>
          <BaseButton type="button" disabled>禁用状态</BaseButton>
        </div>
      </BaseCard>

      <BaseCard as="article" class="preview-panel">
        <h2>状态标签</h2>
        <p>用于任务状态、服务状态、订阅状态。</p>
        <div class="pill-stack">
          <StatusPill>默认</StatusPill>
          <StatusPill tone="primary">等待处理</StatusPill>
          <StatusPill tone="success">已完成</StatusPill>
          <StatusPill tone="danger">失败</StatusPill>
        </div>
      </BaseCard>

      <BaseCard as="article" class="preview-panel">
        <h2>主题</h2>
        <p>默认跟随系统，也可以手动固定亮色或暗色。</p>
        <BaseThemeToggle />
        <div class="theme-swatches">
          <span class="swatch bg">背景</span>
          <span class="swatch card">卡片</span>
          <span class="swatch control">输入</span>
          <span class="swatch primary">主题</span>
        </div>
      </BaseCard>

      <BaseCard as="article" class="preview-panel">
        <h2>开关和多选</h2>
        <p>单个开关使用滑动组件，多选菜单继续使用原生复选框。</p>
        <div class="switch-stack">
          <BaseSwitch v-model="form.pollEnabled" label="启用轮询" />
          <BaseSwitch v-model="form.onlyNew" label="只订阅新作" />
          <BaseSwitch v-model="form.disabledToggle" label="不可编辑" disabled />
        </div>
        <div class="checkbox-grid">
          <label>
            <input v-model="form.notifyEvents" type="checkbox" value="actress">
            女优新番号
          </label>
          <label>
            <input v-model="form.notifyEvents" type="checkbox" value="mteam">
            MTeam 命中
          </label>
          <label>
            <input v-model="form.notifyEvents" type="checkbox" value="jellyfin">
            Jellyfin 入库
          </label>
        </div>
      </BaseCard>

      <BaseCard as="article" class="preview-panel wide">
        <h2>表单和分段</h2>
        <p>表单行、输入框、下拉框、说明文字和 tabs 统一收口。</p>
        <BaseTabs v-model="activeTab" :tabs="tabs" />
        <div class="form-grid">
          <FormField label="番号关键词" hint="例如 SNOS-250">
            <input v-model="form.keyword" type="text" placeholder="输入番号或女优名">
          </FormField>
          <FormField label="搜索类型">
            <select v-model="form.type">
              <option value="av">番号</option>
              <option value="actress">女优</option>
              <option value="maker">厂牌</option>
            </select>
          </FormField>
          <FormField label="备注" hint="用于展示 textarea 的默认视觉" wide>
            <textarea v-model="form.note" rows="3" placeholder="这里可以放策略说明或任务备注"></textarea>
          </FormField>
        </div>
      </BaseCard>

      <BaseCard as="article" class="preview-panel">
        <h2>提示条</h2>
        <div class="notice-stack">
          <NoticeBanner>普通提示：配置已保存。</NoticeBanner>
          <NoticeBanner tone="error">错误提示：外部服务连接失败。</NoticeBanner>
        </div>
      </BaseCard>

      <BaseCard as="article" class="preview-panel sample-card">
        <h2>业务卡片示例</h2>
        <p>模拟首页 / 任务页这类信息卡。</p>
        <strong>29</strong>
        <span>当前订阅总数</span>
        <StatusPill tone="primary">本周 +3</StatusPill>
      </BaseCard>
    </section>
  </section>
</template>

<script setup>
import { reactive, ref } from 'vue'
import { BaseButton, BaseCard, BaseSwitch, BaseTabs, BaseThemeToggle, FormField, NoticeBanner, PageHeader, StatusPill } from '../components/ui'

const activeTab = ref('buttons')
const tabs = [
  { label: '按钮', value: 'buttons', count: 7 },
  { label: '表单', value: 'form', count: 3 },
  { label: '状态', value: 'status', count: 4 }
]
const form = reactive({
  keyword: '',
  type: 'av',
  note: '',
  pollEnabled: true,
  onlyNew: false,
  disabledToggle: true,
  notifyEvents: ['actress', 'mteam']
})
</script>

<style scoped>
.ui-preview h2,
.ui-preview p,
.ui-preview strong,
.ui-preview span {
  margin: 0;
}

.preview-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--mm-layout-gap);
}

.preview-panel {
  display: grid;
  align-content: start;
  gap: var(--mm-space-4);
}

.preview-panel.wide {
  grid-column: 1 / -1;
}

.preview-panel h2 {
  color: var(--mm-text);
  font-size: var(--mm-font-size-section);
  font-weight: var(--mm-font-weight-semibold);
}

.preview-panel p {
  color: var(--mm-muted);
  font-size: var(--mm-font-size-body);
  line-height: 1.7;
}

.button-stack,
.pill-stack,
.notice-stack,
.switch-stack {
  display: flex;
  flex-wrap: wrap;
  gap: var(--mm-space-3);
  align-items: center;
}

.switch-stack {
  display: grid;
  justify-items: start;
}

.checkbox-grid {
  display: grid;
  gap: var(--mm-space-3);
}

.checkbox-grid label {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  min-height: 28px;
  color: var(--mm-text);
  font-weight: var(--mm-font-weight-medium);
}

.checkbox-grid input {
  width: 18px;
  height: 18px;
}

.theme-swatches {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: var(--mm-space-2);
}

.swatch {
  display: grid;
  place-items: center;
  min-height: 46px;
  border: 1px solid var(--mm-border);
  border-radius: var(--mm-radius-sm);
  color: var(--mm-text);
  font-size: var(--mm-font-size-sm);
  font-weight: var(--mm-font-weight-medium);
}

.swatch.bg {
  background: var(--mm-bg);
}

.swatch.card {
  background: var(--mm-card-bg);
}

.swatch.control {
  background: var(--mm-control-bg);
}

.swatch.primary {
  border-color: var(--mm-primary);
  background: var(--mm-primary);
  color: #fff;
}

.form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--mm-space-4);
}

.sample-card strong {
  color: var(--mm-text);
  font-size: 48px;
  font-weight: var(--mm-font-weight-semibold);
  line-height: 1;
}

.sample-card span {
  color: var(--mm-muted);
  font-size: var(--mm-font-size-sm);
}

@media (max-width: 900px) {
  .preview-grid,
  .form-grid {
    grid-template-columns: 1fr;
  }

  .preview-panel.wide {
    grid-column: auto;
  }
}
</style>
