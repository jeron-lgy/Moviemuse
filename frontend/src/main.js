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
          primary: '#FF385C',
          error: '#FF385C',
          success: '#222222',
          warning: '#E00B41',
          info: '#6A6A6A',
          secondary: '#222222',
          background: '#FFFFFF',
          surface: '#ffffff'
        }
      }
    }
  },
  defaults: {
    VBtn: {
      rounded: 'sm',
      height: 42
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
