<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  export let fields: Array<{ label: string; value: string }> = [];
</script>

<dl class="grid grid-cols-[6.5rem_1fr] gap-x-3 gap-y-1">
  {#each fields as field}
    <dt class="text-gray-400">{field.label}</dt>
    <dd class="min-w-0 tabular-nums text-gray-100 [overflow-wrap:anywhere]">{field.value}</dd>
  {/each}
</dl>
