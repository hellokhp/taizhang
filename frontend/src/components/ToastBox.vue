<script setup>
import { toastBox } from '../stores/bid'
</script>

<template>
  <div id="toast" :class="{on: toastBox.on}">{{ toastBox.msg }}</div>
</template>
