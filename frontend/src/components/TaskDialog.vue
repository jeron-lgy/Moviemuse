<template>
  <div class="modal-mask" @click.self="$emit('close')">
    <BaseCard class="task-dialog" >
      <button class="modal-close" type="button" @click="$emit('close')">×</button>
      <h2>{{ title }}</h2>
      <div class="dialog-body">
        <slot />
      </div>
      <footer class="modal-actions">
        <slot name="actions" />
      </footer>
    </BaseCard>
  </div>
</template>

<script setup>
defineEmits(['close'])
defineProps({
  title: {
    type: String,
    required: true
  }
})
</script>

<style scoped>
.modal-mask {
  position: fixed;
  inset: 0;
  z-index: 60;
  display: grid;
  place-items: center;
  padding: 24px;
  background: rgba(34, 34, 34, .45);
}

.task-dialog {
  position: relative;
  width: min(980px, 100%);
  max-height: 88vh;
  overflow: auto;
  padding: 28px;
}

.task-dialog h2 {
  margin: 0;
  font-size: 26px;
  font-weight: 650;
}

.dialog-body {
  display: grid;
  gap: 18px;
  margin-top: 22px;
}

.modal-close {
  position: absolute;
  top: 14px;
  right: 16px;
  border: 0;
  background: transparent;
  color: var(--mm-muted);
  font-size: 24px;
  cursor: pointer;
}

.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 24px;
}
</style>
