<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<!--
  System status (#265). Live host telemetry from /api/status: resource
  gauges, node counters by runtime type, and host facts. Per-lab rows
  come from /api/manage/labs totals.
-->
<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import PageHeader from '$lib/components/shell/PageHeader.svelte';
  import { apiRequest } from '$lib/api';

  interface StatusData {
    cpu: string;
    vCPU: string;
    cpu_percent: number;
    mem: string;
    swap: string;
    mem_used_gb: number;
    mem_total_gb: number;
    disk_used_gb: number;
    disk_total_gb: number;
    kernel: string;
    hostname: string;
    uptime_seconds: number;
    version: string;
    qemu: number;
    docker: number;
    iol: number;
    dynamips: number;
    vpcs: number;
    labs_dir: string;
    images_dir: string;
  }

  let status: StatusData | null = null;
  let labTotals: { running: number; labs: number; cpu_allocated: number; ram_allocated_mb: number } | null = null;
  let autoRefresh: ReturnType<typeof setInterval> | undefined;

  function pct(part: number, total: number): number {
    if (!total) return 0;
    return Math.min(100, Math.round((part / total) * 100));
  }

  function gaugeColor(p: number): string {
    return p > 70 ? 'bg-red-400' : p > 45 ? 'bg-amber-400' : 'bg-emerald-400';
  }

  function fmtUptime(seconds: number): string {
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    const mins = Math.floor((seconds % 3600) / 60);
    return `${days}d ${hours}h ${mins}m`;
  }

  async function refresh() {
    const [statusData, labData] = await Promise.all([
      apiRequest<StatusData>('/status'),
      apiRequest<{ totals: { running: number; labs: number; cpu_allocated: number; ram_allocated_mb: number } }>('/manage/labs')
    ]);
    status = statusData.data;
    labTotals = labData.data.totals;
  }

  onMount(() => {
    void refresh();
    autoRefresh = setInterval(() => void refresh().catch(() => {}), 5000);
  });

  onDestroy(() => {
    if (autoRefresh) clearInterval(autoRefresh);
  });

  $: gauges = status
    ? [
        { label: 'CPU', pct: Math.round(status.cpu_percent), detail: `${status.vCPU} logical cores` },
        {
          label: 'Memory',
          pct: Math.round(Number(status.mem)),
          detail: `${status.mem_used_gb} / ${status.mem_total_gb} GB`
        },
        { label: 'Swap', pct: Math.round(Number(status.swap)), detail: 'host swap' },
        {
          label: 'Disk',
          pct: pct(status.disk_used_gb, status.disk_total_gb),
          detail: `${status.disk_used_gb} / ${status.disk_total_gb} GB`
        }
      ]
    : [];
</script>

<div class="p-6">
  <div class="space-y-6">
    <PageHeader issue={265}>
      <span slot="kicker" class="text-[10px] uppercase tracking-[0.05em] text-gray-500">
        System
      </span>
      <span slot="title">Status</span>
    </PageHeader>

    {#if status}
      <section class="grid grid-cols-2 gap-4 lg:grid-cols-4" data-testid="status-gauges">
        {#each gauges as gauge (gauge.label)}
          <div class="rounded-xl border border-gray-800 bg-gray-900/95 px-4 py-4">
            <div class="flex items-baseline justify-between">
              <span class="text-[10px] uppercase tracking-[0.05em] text-gray-500">{gauge.label}</span>
              <span class="text-lg font-semibold text-gray-100">{gauge.pct}%</span>
            </div>
            <div class="mt-2 h-1.5 overflow-hidden rounded-full bg-gray-800">
              <div class={`h-full rounded-full ${gaugeColor(gauge.pct)}`} style={`width: ${gauge.pct}%`}></div>
            </div>
            <div class="mt-2 text-[11px] text-gray-500">{gauge.detail}</div>
          </div>
        {/each}
      </section>

      <section class="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div class="rounded-2xl border border-gray-800 bg-gray-900/95 p-5" data-testid="status-node-counts">
          <div class="text-[10px] uppercase tracking-[0.05em] text-gray-500">Nodes by type</div>
          <dl class="mt-3 space-y-2">
            {#each [['qemu', status.qemu], ['docker', status.docker], ['iol', status.iol], ['dynamips', status.dynamips]] as [type, count] (type)}
              <div class="flex items-center justify-between text-sm">
                <dt class="font-mono text-gray-300">{type}</dt>
                <dd class="text-gray-400">
                  <span class="text-emerald-300">{count}</span> running
                </dd>
              </div>
            {/each}
          </dl>
          {#if labTotals}
            <div class="mt-4 border-t border-gray-800/70 pt-3 text-[12px] text-gray-500">
              {labTotals.running} / {labTotals.labs} labs running · {labTotals.cpu_allocated} vCPU ·
              {(labTotals.ram_allocated_mb / 1024).toFixed(1)} GB allocated
            </div>
          {/if}
        </div>
        <div class="rounded-2xl border border-gray-800 bg-gray-900/95 p-5" data-testid="status-host">
          <div class="text-[10px] uppercase tracking-[0.05em] text-gray-500">Host</div>
          <dl class="mt-3 space-y-2">
            {#each [['Version', status.version], ['Hostname', status.hostname], ['Kernel', status.kernel], ['Uptime', fmtUptime(status.uptime_seconds)], ['Labs dir', status.labs_dir], ['Images dir', status.images_dir]] as [key, value] (key)}
              <div class="flex items-center justify-between gap-4 text-sm">
                <dt class="shrink-0 text-gray-500">{key}</dt>
                <dd class="truncate font-mono text-[12px] text-gray-300" title={value}>{value}</dd>
              </div>
            {/each}
          </dl>
        </div>
      </section>
    {:else}
      <div class="rounded-2xl border border-gray-800 bg-gray-900/95 px-5 py-10 text-center text-sm text-gray-500">
        Loading telemetry…
      </div>
    {/if}
  </div>
</div>
