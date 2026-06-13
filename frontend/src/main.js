import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { VueQueryPlugin } from '@tanstack/vue-query'
import './styles/tokens.css'
import { initTheme } from './lib/theme'
import App from './App.vue'
import { router } from './router'
import * as uiComponents from './components/ui'

initTheme()

const app = createApp(App)

for (const [name, component] of Object.entries(uiComponents)) {
  app.component(name, component)
}

app
  .use(createPinia())
  .use(VueQueryPlugin, {
    queryClientConfig: {
      defaultOptions: {
        queries: {
          retry: 1,
          refetchOnWindowFocus: false
        }
      }
    }
  })
  .use(router)
  .mount('#app')
