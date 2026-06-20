<template>
  <section class="login-view">
    <BaseCard class="login-card">
      <div class="login-brand">
        <span><img :src="'/static/icons/android-chrome-192x192.png'" alt=""></span>
        <div>
          <strong>MovieMuse</strong>
          <em>控制台登录</em>
        </div>
      </div>

      <form class="login-form" @submit.prevent="login">
        <FormField label="用户名">
          <input v-model.trim="username" autocomplete="username" autofocus>
        </FormField>
        <FormField label="密码">
          <SecretInput v-model="password" autocomplete="current-password" />
        </FormField>
        <NoticeBanner v-if="errorMessage" tone="error">{{ errorMessage }}</NoticeBanner>
        <BaseButton variant="primary" type="submit" size="lg" :disabled="loading">
          {{ loading ? '登录中' : '登录' }}
        </BaseButton>
      </form>
    </BaseCard>
  </section>
</template>

<script setup>
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { postJson } from '../lib/api'
import { BaseButton, BaseCard, FormField, NoticeBanner, SecretInput } from '../components/ui'

const route = useRoute()
const router = useRouter()
const username = ref('admin')
const password = ref('admin')
const loading = ref(false)
const errorMessage = ref('')

async function login() {
  loading.value = true
  errorMessage.value = ''
  try {
    await postJson('/api/auth/login', { username: username.value, password: password.value }, { skipAuthRedirect: true })
    const redirect = typeof route.query.redirect === 'string' && route.query.redirect.startsWith('/')
      ? route.query.redirect
      : '/dashboard'
    await router.replace(redirect)
  } catch (error) {
    errorMessage.value = error.message || '登录失败'
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-view {
  display: grid;
  min-height: 100vh;
  place-items: center;
  padding: 24px;
  background: var(--mm-surface);
}

.login-card {
  display: grid;
  gap: 26px;
  width: min(420px, calc(100vw - 48px));
  max-width: 100%;
  padding: 30px;
  overflow: hidden;
}

.login-brand {
  display: grid;
  grid-template-columns: 54px minmax(0, 1fr);
  align-items: center;
  gap: 14px;
}

.login-brand span {
  display: grid;
  place-items: center;
  width: 54px;
  height: 54px;
  border-radius: 16px;
  background: var(--mm-primary-soft);
}

.login-brand img {
  width: 40px;
  height: 40px;
}

.login-brand strong {
  display: block;
  font-size: 24px;
  font-weight: var(--mm-font-weight-semibold);
}

.login-brand em {
  display: block;
  margin-top: 4px;
  color: var(--mm-muted);
  font-style: normal;
}

.login-form {
  display: grid;
  gap: 16px;
  min-width: 0;
}

.login-form :deep(.mm-field),
.login-form :deep(.mm-secret-input) {
  min-width: 0;
}

.login-form :deep(.mm-notice) {
  overflow-wrap: anywhere;
}

input {
  width: 100%;
  max-width: 100%;
  min-height: 44px;
  padding: 0 14px;
  border: 1px solid var(--mm-border);
  border-radius: 14px;
  background: var(--mm-control-bg);
  color: var(--mm-text);
  font: inherit;
}
</style>
