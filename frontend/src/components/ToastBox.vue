<script setup>
import { toastBox } from '@/stores/taizhang/bid'
</script>

<template>
  <div id="toast" :class="{on: toastBox.on}">{{ toastBox.msg }}</div>
</template>
