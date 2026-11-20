<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<!--
  Node management (#263). Live cross-lab node inventory from
  /api/manage/nodes with usage from the runtime registry, filters
  (lab/template/state), aggregate header, and per-row stop action.
-->
<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { ExternalLink, RefreshCw, Square } from 'lucide-svelte';
  import PageHeader from '$lib/components/shell/PageHeader.svelte';
  import { apiRequest } from '$lib/api';
  import { toastStore } from '$lib/stores/toasts';

  interface ManagedNode {
    lab_path: string;
    lab_name: string;
    node_id: number;
    name: string;
    type: string;
    template: string;
    image: string;
    status: number;
    state: 'running' | 'stopped';
    console: string;
    url: string | null;
    cpu_allocated: number | null;
    ram_allocated_mb: number | null;
    cpu_usage: number;
    ram_usage: number;
    disk_usage: number;
  }

  let nodes: ManagedNode[] = [];
  let totals: { nodes: number; running: number; cpu_allocated: number; ram_allocated_mb: number } | null = null;
  let loading = false;
  let stateFilter: 'running' | 'stopped' | 'all' = 'running';
  let labFilter = '';
  let templateFilter = '';
  let typeFilter = '';
  let stopTarget: ManagedNode | null = null;
  let busyNode: string | null = null;
  let autoRefresh: ReturnType<typeof setInterval> | undefined;

  const REFRESH_MS = 5000;
  const TYPE_OPTIONS = ['qemu', 'docker', 'iol', 'dynamips'];

  const typeChip: Record<string, string> = {
    qemu: 'border-sky-500/40 text-sky-300',
    docker: 'border-blue-500/40 text-blue-300',
    iol: 'border-violet-500/40 text-violet-300',
    dynamips: 'border-amber-500/40 text-amber-300'
  };

  async function refresh() {
    loading = true;
    try {
      const params = new URLSearchParams();
      params.set('state', stateFilter);
      if (labFilter.trim()) params.set('lab', labFilter.trim());
      if (templateFilter.trim()) params.set('template', templateFilter.trim());
      if (typeFilter) params.set('node_type', typeFilter);
      const data = await apiRequest<{ nodes: ManagedNode[]; totals: { nodes: number; running: number; cpu_allocated: number; ram_allocated_mb: number } }>(
        `/manage/nodes?${params.toString()}`
      );
      nodes = data.data.nodes;
      totals = data.data.totals;
    } finally {
      loading = false;
    }
  }

  function ramGb(mb: number | null): string {
    if (mb === null || mb === undefined) return '\u2014';
    return `${(mb / 1024).toFixed(1)} GB`;
  }

  function fmtCpu(value: number): string {
    return `${Number(value ?? 0).toFixed(0)}%`;
  }

  async function stopNode() {
    const node = stopTarget;
    stopTarget = null;
    if (!node) return;
    busyNode = `${node.lab_path}:${node.node_id}`;
    try {
      await apiRequest(
        `/labs${node.lab_path}/nodes/${node.node_id}/stop`,
        { method: 'GET', body: null }
      );
      toastStore.push(`Node ${node.name} stopped.`, 'success');
      await refresh();
    } catch {
      /* toasted */
    } finally {
      busyNode = null;
    }
  }

  onMount(() => {
    void refresh();
    autoRefresh = setInterval(() => void refresh(), REFRESH_MS);
  });

  onDestroy(() => {
    if (autoRefresh) clearInterval(autoRefresh);
  });
</script>

<div class="p-6">
  <div class="space-y-6">
    <PageHeader issue={263}>
      <span slot="kicker" class="text-[10px] uppercase tracking-[0.05em] text-gray-500">
        Management
      </span>
      <span slot="title">Running Nodes</span>
      <span slot="actions">
        <button
          class="inline-flex items-center gap-1.5 rounded-md border border-gray-700 px-3 py-2 text-[10px] uppercase tracking-[0.05em] text-gray-300 transition hover:border-blue-500 hover:text-white"
          on:click={() => void refresh()}
          data-testid="nodes-refresh"
        >
          <RefreshCw class="h-3 w-3" />
          Refresh
        </button>
      </span>
    </PageHeader>

    <section class="grid grid-cols-2 gap-4 lg:grid-cols-4" data-testid="nodes-kpis">
      <div class="rounded-xl border border-gray-800 bg-gray-900/95 px-4 py-3">
        <div class="text-[10px] uppercase tracking-[0.05em] text-gray-500">Nodes listed</div>
        <div class="mt-1 text-xl font-semibold text-gray-100">{totals ? totals.nodes : '\u2014'}</div>
      </div>
      <div class="rounded-xl border border-gray-800 bg-gray-900/95 px-4 py-3">
        <div class="text-[10px] uppercase tracking-[0.05em] text-gray-500">Running</div>
        <div class="mt-1 text-xl font-semibold text-gray-100">{totals ? totals.running : '\u2014'}</div>
      </div>
      <div class="rounded-xl border border-gray-800 bg-gray-900/95 px-4 py-3">
        <div class="text-[10px] uppercase tracking-[0.05em] text-gray-500">vCPU allocated</div>
        <div class="mt-1 text-xl font-semibold text-gray-100">{totals ? totals.cpu_allocated : '\u2014'}</div>
      </div>
      <div class="rounded-xl border border-gray-800 bg-gray-900/95 px-4 py-3">
        <div class="text-[10px] uppercase tracking-[0.05em] text-gray-500">RAM allocated</div>
        <div class="mt-1 text-xl font-semibold text-gray-100">{totals ? ramGb(totals.ram_allocated_mb) : '\u2014'}</div>
      </div>
    </section>

    <div class="flex flex-wrap items-center gap-2">
      <select
        class="rounded-lg border border-gray-700 bg-gray-950 px-2 py-1.5 text-[12px] text-gray-300 focus:border-blue-500 focus:outline-none"
        bind:value={stateFilter}
        on:change={() => void refresh()}
        data-testid="nodes-state-filter"
      >
        <option value="running">Running</option>
        <option value="stopped">Stopped</option>
        <option value="all">All states</option>
      </select>
      <input
        class="w-44 rounded-lg border border-gray-700 bg-gray-950 px-3 py-1.5 text-[12px] text-gray-200 placeholder-gray-600 focus:border-blue-500 focus:outline-none"
        placeholder="Filter by lab…"
        bind:value={labFilter}
        on:change={() => void refresh()}
        data-testid="nodes-lab-filter"
      />
      <input
        class="w-44 rounded-lg border border-gray-700 bg-gray-950 px-3 py-1.5 text-[12px] text-gray-200 placeholder-gray-600 focus:border-blue-500 focus:outline-none"
        placeholder="Filter by template…"
        bind:value={templateFilter}
        on:change={() => void refresh()}
        data-testid="nodes-template-filter"
      />
      <select
        class="rounded-lg border border-gray-700 bg-gray-950 px-2 py-1.5 text-[12px] text-gray-300 focus:border-blue-500 focus:outline-none"
        bind:value={typeFilter}
        on:change={() => void refresh()}
        data-testid="nodes-type-filter"
      >
        <option value="">All types</option>
        {#each TYPE_OPTIONS as option (option)}
          <option value={option}>{option}</option>
        {/each}
      </select>
    </div>

    {#if loading && !nodes.length}
      <div class="rounded-2xl border border-gray-800 bg-gray-900/95 px-5 py-10 text-center text-sm text-gray-500">
        Loading nodes…
      </div>
    {:else}
      <section class="overflow-hidden rounded-2xl border border-gray-800 bg-gray-900/95" data-testid="nodes-table">
        <table class="w-full text-left text-sm">
          <thead>
            <tr class="border-b border-gray-800 text-[10px] uppercase tracking-[0.05em] text-gray-500">
              <th class="px-5 py-2.5">Node</th>
              <th class="px-3 py-2.5">Lab</th>
              <th class="px-3 py-2.5">Type</th>
              <th class="px-3 py-2.5">State</th>
              <th class="px-3 py-2.5">CPU</th>
              <th class="px-3 py-2.5">RAM</th>
              <th class="px-3 py-2.5">Disk</th>
              <th class="px-5 py-2.5 text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {#each nodes as node (`${node.lab_path}:${node.node_id}`)}
              <tr class="border-b border-gray-800/60 last:border-0 hover:bg-gray-800/40" data-testid={`node-row-${node.name}`}>
                <td class="px-5 py-3">
                  <div class="font-medium text-gray-100">{node.name}</div>
                  <div class="font-mono text-[10px] text-gray-500">{node.template || node.type} \u00b7 {node.image || ''}</div>
                </td>
                <td class="px-3 py-3 text-[12px] text-gray-400" title={node.lab_path}>{node.lab_name}</td>
                <td class="px-3 py-3">
                  <span class={`rounded-full border px-1.5 py-px text-[9px] uppercase tracking-[0.05em] ${typeChip[node.type] || 'border-gray-600 text-gray-400'}`}>{node.type}</span>
                </td>
                <td class="px-3 py-3">
                  <span class="inline-flex items-center gap-1.5 text-[12px]">
                    <span class={`h-1.5 w-1.5 rounded-full ${node.state === 'running' ? 'bg-emerald-400' : 'bg-gray-600'}`}></span>
                    <span class={node.state === 'running' ? 'text-gray-300' : 'text-gray-500'}>{node.state}</span>
                  </span>
                </td>
                <td class="px-3 py-3 text-[12px] text-gray-300">{fmtCpu(node.cpu_usage)}</td>
                <td class="px-3 py-3 text-[12px] text-gray-300">{ramGb(node.ram_allocated_mb)}</td>
                <td class="px-3 py-3 text-[12px] text-gray-300">
                  {node.disk_usage ? `${(node.disk_usage / 1024 ** 3).toFixed(2)} GB` : '\u2014'}
                </td>
                <td class="px-5 py-3 text-right">
                  <span class="inline-flex justify-end gap-1.5">
                    {#if node.url}
                      <a
                        href={node.url}
                        target="_blank"
                        rel="noreferrer"
                        class="rounded-md border border-gray-700 px-2.5 py-1.5 text-[10px] uppercase tracking-[0.05em] text-gray-300 transition hover:border-blue-500 hover:text-white"
                        title="Open console"
                      >
                        <ExternalLink class="mr-1 inline h-3 w-3" />
                        Console
                      </a>
                    {/if}
                    {#if node.state === 'running'}
                      <button
                        class="rounded-md border border-red-500/40 px-2.5 py-1.5 text-[10px] uppercase tracking-[0.05em] text-red-200 transition hover:bg-red-500/10 disabled:opacity-40"
                        disabled={busyNode !== null}
                        on:click={() => (stopTarget = node)}
                        data-testid={`node-stop-${node.name}`}
                      >
                        <Square class="mr-1 inline h-3 w-3" />
                        Stop
                      </button>
                    {/if}
                  </span>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </section>
    {/if}
  </div>
</div>

{#if stopTarget}
  <div class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" role="presentation" data-testid="node-stop-confirm">
    <div class="w-full max-w-sm rounded-2xl border border-gray-800 bg-gray-900 p-5 shadow-2xl">
      <h2 class="text-sm font-semibold text-gray-100">Stop {stopTarget.name}?</h2>
      <p class="mt-2 text-[12px] text-gray-400">
        Stops this node in {stopTarget.lab_name}. Topology and disk overlays are kept.
      </p>
      <div class="mt-4 flex justify-end gap-2">
        <button
          class="rounded-lg border border-gray-700 px-4 py-2 text-[12px] text-gray-300 transition hover:bg-gray-800"
          on:click={() => (stopTarget = null)}
          data-testid="node-stop-cancel"
        >Cancel</button>
        <button
          class="rounded-lg bg-red-600 px-4 py-2 text-[12px] font-medium text-white transition hover:bg-red-500"
          on:click={stopNode}
          data-testid="node-stop-confirm-button"
        >Stop node</button>
      </div>
    </div>
  </div>
{/if}
