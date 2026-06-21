import { defineStore } from 'pinia'

export const useUiStore = defineStore('ui', {
  state: () => ({
    sidebarCollapsed: false,
    openGroups: {
      subscriptions: true
    }
  }),
  actions: {
    toggleSidebar() {
      this.sidebarCollapsed = !this.sidebarCollapsed
    },
    toggleGroup(key) {
      this.openGroups[key] = !this.openGroups[key]
    }
  }
})
