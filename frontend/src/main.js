import { createApp } from 'vue'
import App from '@/App.vue'
import router from '@/router'
import '@/styles/main.css'
import '@/styles/table-layout.css'

createApp(App).use(router).mount('#app')
