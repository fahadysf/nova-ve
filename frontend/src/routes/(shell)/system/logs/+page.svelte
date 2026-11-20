<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<!--
  Log viewer (#265). Picks a node runtime stdout log, tails it with a
  line-count and substring filter, and supports manual + periodic refresh.
-->
<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { RefreshCw } from 'lucide-svelte';
  import PageHeader from '$lib/components/shell/PageHeader.svelte';
  import { apiRequest } from '$lib/api';

  interface LogEntry {
    name: string;
    label: string;
    kind: string;
    lab_id: string;
    node_id: number | null;
    path: string;
    size: number;
    mtime: number;
  }

  interface LogTail {
    log: LogEntry;
    lines: number;
    pattern: string;
    content: string;
  }

  let logs: LogEntry[] = [];
  let selectedPath = '';
  let tailLines = 200;
  let pattern = '';
  let content = '';
  let loadedLines = 0;
  let loading = false;
  let autoRefresh: ReturnType<typeof setInterval> | undefined;

  const TAIL_OPTIONS = [100, 200, 500, 1000];

  async function loadList() {
    const data = await apiRequest<{ logs: LogEntry[] }>('/system/logs');
    logs = data.data.logs;
    if (!selectedPath && logs.length > 0) {
      selectedPath = logs[0].path;
      await loadTail();
    }
  }

  async function loadTail() {
    if (!selectedPath) return;
    loading = true;
    try {
      const data = await apiRequest<LogTail>(
        `/system/logs/tail?path=${encodeURIComponent(selectedPath)}&lines=${tailLines}&pattern=${encodeURIComponent(pattern)}`
      );
      content = data.data.content;
      loadedLines = data.data.lines;
    } finally {
      loading = false;
    }
  }

  function togglePolling(event: Event) {
    const checkbox = event.target as HTMLInputElement;
    if (autoRefresh) {
      clearInterval(autoRefresh);
      autoRefresh = undefined;
    }
    if (checkbox.checked) {
      autoRefresh = setInterval(() => void loadTail().catch(() => {}), 2000);
    }
  }

  onMount(() => {
    void loadList();
  });

  onDestroy(() => {
    if (autoRefresh) clearInterval(autoRefresh);
  });

  $: selected = logs.find((log) => log.path === selectedPath) ?? null;
</script>

<div class="p-6">
  <div class="space-y-6">
    <PageHeader issue={265}>
      <span slot="kicker" class="text-[10px] uppercase tracking-[0.05em] text-gray-500">
        System
      </span>
      <span slot="title">Logs</span>
      <span slot="actions">
        <button
          class="inline-flex items-center gap-1.5 rounded-md border border-gray-700 px-3 py-2 text-[10px] uppercase tracking-[0.05em] text-gray-300 transition hover:border-blue-500 hover:text-white"
          on:click={() => void loadTail()}
          data-testid="logs-refresh"
        >
          <RefreshCw class="h-3 w-3" />
          Refresh tail
        </button>
      </span>
    </PageHeader>

    <div class="flex flex-wrap items-center gap-2">
      <select
        class="min-w-64 rounded-lg border border-gray-700 bg-gray-950 px-3 py-1.5 text-[12px] text-gray-300 focus:border-blue-500 focus:outline-none"
        bind:value={selectedPath}
        on:change={() => void loadTail()}
        data-testid="logs-file-select"
      >
        {#if logs.length === 0}
          <option value="">No runtime logs available</option>
        {/if}
        {#each logs as log (log.path)}
          <option value={log.path}>{log.name} ({log.kind})</option>
        {/each}
      </select>
      <select
        class="rounded-lg border border-gray-700 bg-gray-950 px-2 py-1.5 text-[12px] text-gray-300 focus:border-blue-500 focus:outline-none"
        bind:value={tailLines}
        on:change={() => void loadTail()}
        data-testid="logs-lines-select"
      >
        {#each TAIL_OPTIONS as option (option)}
          <option value={option}>{option} lines</option>
        {/each}
      </select>
      <input
        class="w-56 rounded-lg border border-gray-700 bg-gray-950 px-3 py-1.5 text-[12px] text-gray-200 placeholder-gray-600 focus:border-blue-500 focus:outline-none"
        placeholder="Filter pattern…"
        bind:value={pattern}
        on:change={() => void loadTail()}
        data-testid="logs-pattern"
      />
      <label class="flex items-center gap-1.5 text-[11px] text-gray-500">
        <input
          type="checkbox"
          class="h-3.5 w-3.5 rounded border-gray-700 bg-gray-950"
          on:change={togglePolling}
          data-testid="logs-autorefresh"
        />
        Auto (2s)
      </label>
      {#if selected}
        <span class="ml-auto font-mono text-[10px] text-gray-600" title={selected.path}>
          {selected.lab_id} · {selected.size} bytes
        </span>
      {/if}
    </div>

    <section class="overflow-hidden rounded-2xl border border-gray-800 bg-gray-950" data-testid="logs-panel">
      {#if content}
        <pre class="max-h-[60vh] overflow-auto p-4 font-mono text-[11px] leading-relaxed text-gray-300">{content}</pre>
      {:else}
        <div class="px-5 py-10 text-center text-sm text-gray-600">
          {loading ? 'Loading…' : 'Select a log file to view its tail.'}
        </div>
      {/if}
    </section>
    {#if content}
      <div class="text-right text-[10px] text-gray-600" data-testid="logs-line-count">{loadedLines} lines shown</div>
    {/if}
  </div>
</div>
