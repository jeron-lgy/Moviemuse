import { createApp } from 'vue'
import { createVuetify } from 'vuetify'
import * as components from 'vuetify/components'
import * as directives from 'vuetify/directives'
import 'vuetify/styles'
import '@mdi/font/css/materialdesignicons.css'
import App from './App.vue'

const vuetify = createVuetify({
  components,
  directives,
  icons: {
    defaultSet: 'mdi'
  },
  theme: {
    defaultTheme: 'mediaToolbox',
    themes: {
      mediaToolbox: {
        dark: false,
        colors: {
          primary: '#0f8f83',
          secondary: '#8b4dff',
          background: '#f6f7fc',
          surface: '#ffffff'
        }
      }
    }
  },
  defaults: {
    VBtn: {
      rounded: 'lg',
      height: 46
    },
    VTextField: {
      variant: 'outlined',
      density: 'comfortable',
      hideDetails: 'auto',
      bgColor: 'white'
    },
    VSelect: {
      variant: 'outlined',
      density: 'comfortable',
      hideDetails: 'auto',
      bgColor: 'white'
    },
    VTextarea: {
      variant: 'outlined',
      density: 'comfortable',
      hideDetails: 'auto',
      bgColor: 'white'
    }
  }
})

createApp(App).use(vuetify).mount('#app')
