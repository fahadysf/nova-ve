<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<!--
  Lab management (#262). Live instance inventory from /api/manage/labs:
  state, allocated vCPU/RAM and disk usage per lab, KPI totals, and admin lifecycle
  actions (stop = stop every running node; wipe = stop + wipe all).
-->
<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { RefreshCw, Square, Trash2 } from 'lucide-svelte';
  import PageHeader from '$lib/components/shell/PageHeader.svelte';
  import { apiRequest } from '$lib/api';
  import { authStore } from '$lib/stores/session';
  import { toastStore } from '$lib/stores/toasts';
  import { formatSize } from '$lib/services/recordingSearch';

  interface ManagedLab {
    path: string;
    file: string;
    name: string;
    author: string;
    description: string;
    state: 'running' | 'stopped';
    nodes_total: number;
    nodes_running: number;
    cpu_allocated: number;
    ram_allocated_mb: number;
    disk_bytes: number | null;
    lock: boolean;
  }

  interface ManagedLabsTotals {
    labs: number;
    running: number;
    nodes_total: number;
    nodes_running: number;
    cpu_allocated: number;
    ram_allocated_mb: number;
    disk_bytes: number | null;
  }

  let labs: ManagedLab[] = [];
  let totals: ManagedLabsTotals | null = null;
  let loading = false;
  let actionBusy: string | null = null;
  let actionTarget: { lab: ManagedLab; kind: 'stop' | 'wipe' } | null = null;
  let autoRefresh: ReturnType<typeof setInterval> | undefined;

  const REFRESH_MS = 5000;

  async function refresh() {
    loading = true;
    try {
      const data = await apiRequest<{ labs: ManagedLab[]; totals: ManagedLabsTotals }>(
        '/manage/labs'
      );
      labs = data.data.labs;
      totals = data.data.totals;
    } finally {
      loading = false;
    }
  }

  function isAdmin(): boolean {
    return $authStore.user?.role === 'admin';
  }

  function ramGb(mb: number): string {
    return `${(mb / 1024).toFixed(1)} GB`;
  }

  async function runAction() {
    const target = actionTarget;
    actionTarget = null;
    if (!target) return;
    actionBusy = `${target.kind}:${target.lab.path}`;
    try {
      await apiRequest(
        `/manage/labs${target.lab.path}/` +
          (target.kind === 'wipe' ? 'wipe' : 'stop'),
        { method: 'POST', body: {} }
      );
      toastStore.push(
        target.kind === 'wipe' ? `Lab ${target.lab.name} wiped.` : `Lab ${target.lab.name} stopped.`,
        'success'
      );
      await refresh();
    } catch {
      /* toasted */
    } finally {
      actionBusy = null;
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
    <PageHeader issue={262}>
      <span slot="kicker" class="text-[10px] uppercase tracking-[0.05em] text-gray-500">
        Labs
      </span>
      <span slot="title">Lab Management</span>
      <span slot="actions">
        <button
          class="inline-flex items-center gap-1.5 rounded-md border border-gray-700 px-3 py-2 text-[10px] uppercase tracking-[0.05em] text-gray-300 transition hover:border-blue-500 hover:text-white"
          on:click={() => void refresh()}
          data-testid="labs-refresh"
        >
          <RefreshCw class="h-3 w-3" />
          Refresh
        </button>
      </span>
    </PageHeader>

    <section class="grid grid-cols-2 gap-4 lg:grid-cols-5" data-testid="labs-kpis">
      <div class="rounded-xl border border-gray-800 bg-gray-900/95 px-4 py-3">
        <div class="text-[10px] uppercase tracking-[0.05em] text-gray-500">Labs running</div>
        <div class="mt-1 text-xl font-semibold text-gray-100">
          {totals ? `${totals.running} / ${totals.labs}` : '—'}
        </div>
      </div>
      <div class="rounded-xl border border-gray-800 bg-gray-900/95 px-4 py-3">
        <div class="text-[10px] uppercase tracking-[0.05em] text-gray-500">Nodes running</div>
        <div class="mt-1 text-xl font-semibold text-gray-100">
          {totals ? `${totals.nodes_running} / ${totals.nodes_total}` : '—'}
        </div>
      </div>
      <div class="rounded-xl border border-gray-800 bg-gray-900/95 px-4 py-3">
        <div class="text-[10px] uppercase tracking-[0.05em] text-gray-500">vCPU allocated</div>
        <div class="mt-1 text-xl font-semibold text-gray-100">{totals ? totals.cpu_allocated : '—'}</div>
      </div>
      <div class="rounded-xl border border-gray-800 bg-gray-900/95 px-4 py-3">
        <div class="text-[10px] uppercase tracking-[0.05em] text-gray-500">RAM allocated</div>
        <div class="mt-1 text-xl font-semibold text-gray-100">
          {totals ? ramGb(totals.ram_allocated_mb) : '—'}
        </div>
      </div>
      <div class="rounded-xl border border-gray-800 bg-gray-900/95 px-4 py-3">
        <div class="text-[10px] uppercase tracking-[0.05em] text-gray-500">Disk used</div>
        <div class="mt-1 text-xl font-semibold text-gray-100">
          {totals ? (totals.disk_bytes != null ? formatSize(totals.disk_bytes) : 'Unavailable') : '—'}
        </div>
      </div>
    </section>

    {#if loading && !labs.length}
      <div class="rounded-2xl border border-gray-800 bg-gray-900/95 px-5 py-10 text-center text-sm text-gray-500">
        Loading labs…
      </div>
    {:else}
      <section class="overflow-hidden rounded-2xl border border-gray-800 bg-gray-900/95" data-testid="labs-table">
        <table class="w-full text-left text-sm">
          <thead>
            <tr class="border-b border-gray-800 text-[10px] uppercase tracking-[0.05em] text-gray-500">
              <th class="px-5 py-2.5">Lab</th>
              <th class="px-3 py-2.5">State</th>
              <th class="px-3 py-2.5">Owner</th>
              <th class="px-3 py-2.5">Nodes</th>
              <th class="px-3 py-2.5">vCPU</th>
              <th class="px-3 py-2.5">Memory</th>
              <th class="px-3 py-2.5">Disk used</th>
              <th class="px-5 py-2.5 text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {#each labs as lab (lab.path)}
              <tr class="border-b border-gray-800/60 last:border-0 hover:bg-gray-800/40" data-testid={`labs-row-${lab.file}`}>
                <td class="px-5 py-3">
                  <div class="font-medium text-gray-100">{lab.name}</div>
                  <div class="font-mono text-[10px] text-gray-500">{lab.path}</div>
                </td>
                <td class="px-3 py-3">
                  <span class="inline-flex items-center gap-1.5 text-[12px]">
                    <span class={`h-1.5 w-1.5 rounded-full ${lab.state === 'running' ? 'bg-emerald-400' : 'bg-gray-600'}`}></span>
                    <span class={lab.state === 'running' ? 'text-gray-300' : 'text-gray-500'}>{lab.state}</span>
                  </span>
                </td>
                <td class="px-3 py-3 text-[12px] text-gray-400">{lab.author || '—'}</td>
                <td class="px-3 py-3 text-[12px] text-gray-300">
                  <span class={lab.nodes_running > 0 ? 'text-emerald-300' : ''}>{lab.nodes_running}</span>
                  <span class="text-gray-600"> / {lab.nodes_total}</span>
                </td>
                <td class="px-3 py-3 text-[12px] text-gray-300">{lab.cpu_allocated}</td>
                <td class="px-3 py-3 text-[12px] text-gray-300">{ramGb(lab.ram_allocated_mb)}</td>
                <td class="px-3 py-3 text-[12px] text-gray-300">{lab.disk_bytes != null ? formatSize(lab.disk_bytes) : 'Unavailable'}</td>
                <td class="px-5 py-3 text-right">
                  {#if isAdmin()}
                    <span class="inline-flex justify-end gap-1.5">
                      <button
                        class="rounded-md border border-gray-700 px-2.5 py-1.5 text-[10px] uppercase tracking-[0.05em] text-gray-300 transition hover:border-amber-400 hover:text-amber-200 disabled:opacity-40"
                        disabled={lab.state !== 'running' || actionBusy !== null}
                        on:click={() => (actionTarget = { lab, kind: 'stop' })}
                        data-testid={`labs-stop-${lab.file}`}
                      >
                        <Square class="mr-1 inline h-3 w-3" />
                        Stop
                      </button>
                      <button
                        class="rounded-md border border-red-500/40 px-2.5 py-1.5 text-[10px] uppercase tracking-[0.05em] text-red-200 transition hover:bg-red-500/10 disabled:opacity-40"
                        disabled={lab.nodes_total === 0 || actionBusy !== null}
                        on:click={() => (actionTarget = { lab, kind: 'wipe' })}
                        data-testid={`labs-wipe-${lab.file}`}
                      >
                        <Trash2 class="mr-1 inline h-3 w-3" />
                        Wipe
                      </button>
                    </span>
                  {:else}
                    <span class="text-[10px] text-gray-600" title="Lifecycle actions require an administrator">view only</span>
                  {/if}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </section>
    {/if}
  </div>
</div>

{#if actionTarget}
  <div class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" role="presentation" data-testid="lab-action-confirm">
    <div class="w-full max-w-sm rounded-2xl border border-gray-800 bg-gray-900 p-5 shadow-2xl">
      <h2 class="text-sm font-semibold text-gray-100">
        {actionTarget.kind === 'wipe' ? 'Wipe' : 'Stop'} {actionTarget.lab.name}?
      </h2>
      <p class="mt-2 text-[12px] text-gray-400">
        {#if actionTarget.kind === 'wipe'}
          Stops every node and deletes each node's work directory (disks, configs). Lab topology is kept.
        {:else}
          Stops every running node in this lab. Topology and disks are kept.
        {/if}
      </p>
      <div class="mt-4 flex justify-end gap-2">
        <button
          class="rounded-lg border border-gray-700 px-4 py-2 text-[12px] text-gray-300 transition hover:bg-gray-800"
          on:click={() => (actionTarget = null)}
          data-testid="lab-action-cancel"
        >Cancel</button>
        <button
          class="rounded-lg bg-red-600 px-4 py-2 text-[12px] font-medium text-white transition hover:bg-red-500"
          on:click={runAction}
          data-testid="lab-action-confirm-button"
        >
          {actionTarget.kind === 'wipe' ? 'Wipe lab' : 'Stop lab'}
        </button>
      </div>
    </div>
  </div>
{/if}
