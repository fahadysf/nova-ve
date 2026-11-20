<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<!-- Shared page header for shell pages (#260): kicker + title + description
     with room for actions and a trailing issue reference. -->
<script lang="ts">
  /** Tracking issue shown as a subtle reference chip when provided. */
  export let issue: number | null = null;
</script>

<div class="flex flex-wrap items-start justify-between gap-4 rounded-2xl border border-gray-800 bg-gray-900/95 px-5 py-4">
  <div class="min-w-0">
    <slot name="kicker" />
    <h1 class="mt-1.5 text-xl font-semibold tracking-tight text-gray-100"><slot name="title" /></h1>
    {#if issue}
      <a
        class="mt-2 inline-block text-[10px] uppercase tracking-[0.05em] text-gray-500 transition hover:text-gray-300"
        href={`https://github.com/fahadysf/nova-ve/issues/${issue}`}
        target="_blank"
        rel="noreferrer"
      >tracked in #{issue}</a>
    {/if}
  </div>
  <div class="flex flex-wrap items-center gap-2">
    <slot name="actions" />
  </div>
</div>
