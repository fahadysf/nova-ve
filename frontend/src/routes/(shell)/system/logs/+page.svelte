<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<!--
  Logs page: a switch between two views. Runtime logs (#265) tail a node's
  stdout; the Activity log (#297) is the searchable user-activity trail.
-->
<script lang="ts">
  import PageHeader from '$lib/components/shell/PageHeader.svelte';
  import ActivityLog from '$lib/components/system/ActivityLog.svelte';
  import RuntimeLog from '$lib/components/system/RuntimeLog.svelte';

  type LogView = 'runtime' | 'activity';

  let view: LogView = 'runtime';

  const VIEWS: ReadonlyArray<{ key: LogView; label: string }> = [
    { key: 'runtime', label: 'Runtime logs' },
    { key: 'activity', label: 'Activity log' }
  ];
</script>

<div class="p-6">
  <div class="space-y-6">
    <PageHeader issue={view === 'activity' ? 297 : 265}>
      <span slot="kicker" class="text-[10px] uppercase tracking-[0.05em] text-gray-500">
        System
      </span>
      <span slot="title">Logs</span>
    </PageHeader>

    <div class="flex gap-1" role="tablist" aria-label="Log views">
      {#each VIEWS as option (option.key)}
        <button
          role="tab"
          aria-selected={view === option.key}
          class={`rounded-lg px-3 py-1.5 text-[12px] transition ${
            view === option.key
              ? 'border border-blue-500/50 bg-blue-500/10 text-blue-200'
              : 'border border-transparent text-gray-500 hover:bg-gray-800/60 hover:text-gray-300'
          }`}
          on:click={() => (view = option.key)}
          data-testid={`logs-view-${option.key}`}
        >{option.label}</button>
      {/each}
    </div>

    {#if view === 'activity'}
      <ActivityLog />
    {:else}
      <RuntimeLog />
    {/if}
  </div>
</div>
