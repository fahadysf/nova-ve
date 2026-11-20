<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<!-- System status (#265). Static preview: gauges + node-type counters. -->
<script lang="ts">
  import PageHeader from '$lib/components/shell/PageHeader.svelte';

  const GAUGES = [
    { label: 'CPU', pct: 24, detail: '12 cores · 2.9 / 12.0 in use' },
    { label: 'Memory', pct: 31, detail: '9.9 / 32.0 GB' },
    { label: 'Swap', pct: 0, detail: '0.0 / 8.0 GB' },
    { label: 'Disk', pct: 27, detail: '135 / 501 GB' },
  ];

  const NODE_COUNTS = [
    { type: 'qemu', running: 3, stopped: 2 },
    { type: 'docker', running: 1, stopped: 0 },
    { type: 'iol', running: 1, stopped: 0 },
    { type: 'dynamips', running: 1, stopped: 1 },
  ];

  function gaugeColor(pct: number): string {
    return pct > 70 ? 'bg-red-400' : pct > 45 ? 'bg-amber-400' : 'bg-emerald-400';
  }

  const HOST = [
    ['Version', '0.4.0 (dev)'],
    ['Server id', 'nve-9f3a-2c41'],
    ['Kernel', '6.8.0-45-generic'],
    ['Uptime', '9d 4h 12m'],
    ['Backend', 'nova-ve-backend · active'],
    ['Console gateway', 'guacamole · active'],
  ];
</script>

<div class="p-6">
  <div class="space-y-6">
    <PageHeader issue={265}>
      <span slot="kicker" class="text-[10px] uppercase tracking-[0.05em] text-gray-500">
        System · Preview data
      </span>
      <span slot="title">Status</span>
    </PageHeader>

    <section class="grid grid-cols-2 gap-4 lg:grid-cols-4" data-testid="status-gauges">
      {#each GAUGES as gauge (gauge.label)}
        <div class="rounded-xl border border-gray-800 bg-gray-900/95 px-4 py-4">
          <div class="flex items-baseline justify-between">
            <span class="text-[10px] uppercase tracking-[0.05em] text-gray-500">{gauge.label}</span>
            <span class="text-lg font-semibold text-gray-100">{gauge.pct}%</span>
          </div>
          <div class="mt-2 h-1.5 overflow-hidden rounded-full bg-gray-800">
            <div class="h-full rounded-full {gaugeColor(gauge.pct)}" style="width: {gauge.pct}%"></div>
          </div>
          <div class="mt-2 text-[11px] text-gray-500">{gauge.detail}</div>
        </div>
      {/each}
    </section>

    <section class="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <div class="rounded-2xl border border-gray-800 bg-gray-900/95 p-5" data-testid="status-node-counts">
        <div class="text-[10px] uppercase tracking-[0.05em] text-gray-500">Nodes by type</div>
        <dl class="mt-3 space-y-2">
          {#each NODE_COUNTS as row (row.type)}
            <div class="flex items-center justify-between text-sm">
              <dt class="font-mono text-gray-300">{row.type}</dt>
              <dd class="text-gray-400">{row.running} running · {row.stopped} stopped</dd>
            </div>
          {/each}
        </dl>
      </div>
      <div class="rounded-2xl border border-gray-800 bg-gray-900/95 p-5" data-testid="status-host">
        <div class="text-[10px] uppercase tracking-[0.05em] text-gray-500">Host</div>
        <dl class="mt-3 space-y-2">
          {#each HOST as [key, value]}
            <div class="flex items-center justify-between text-sm">
              <dt class="text-gray-500">{key}</dt>
              <dd class="font-mono text-[12px] text-gray-300">{value}</dd>
            </div>
          {/each}
        </dl>
      </div>
    </section>
  </div>
</div>
