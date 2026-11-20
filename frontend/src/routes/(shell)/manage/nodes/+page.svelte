<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<!--
  Node management (#263). Static preview: every running node across labs
  with per-node resource usage.
-->
<script lang="ts">
  import PageHeader from '$lib/components/shell/PageHeader.svelte';

  const NODES = [
    { lab: 'ST-Demo', name: 'vm-linux', template: 'alpine', type: 'docker', cpu: 3, ramMb: 256, diskGb: 0.1, console: 'telnet' },
    { lab: 'ST-Demo', name: 'iol-rtr', template: 'iol', type: 'iol', cpu: 11, ramMb: 512, diskGb: 0.0, console: 'telnet' },
    { lab: 'ST-Demo', name: 'dyn-c7200', template: 'c7200', type: 'dynamips', cpu: 38, ramMb: 512, diskGb: 0.3, console: 'telnet' },
    { lab: 'arc-lab', name: 'veos-leaf1', template: 'veos', type: 'qemu', cpu: 22, ramMb: 2048, diskGb: 1.8, console: 'telnet' },
    { lab: 'arc-lab', name: 'veos-leaf2', template: 'veos', type: 'qemu', cpu: 19, ramMb: 2048, diskGb: 1.8, console: 'telnet' },
    { lab: 'arc-lab', name: 'vyos-wan', template: 'vyos', type: 'qemu', cpu: 8, ramMb: 1024, diskGb: 0.9, console: 'telnet' },
  ];

  const typeChip: Record<string, string> = {
    qemu: 'border-sky-500/40 text-sky-300',
    docker: 'border-blue-500/40 text-blue-300',
    iol: 'border-violet-500/40 text-violet-300',
    dynamips: 'border-amber-500/40 text-amber-300',
  };
</script>

<div class="p-6">
  <div class="space-y-6">
    <PageHeader issue={263}>
      <span slot="kicker" class="text-[10px] uppercase tracking-[0.05em] text-gray-500">
        Management · Preview data
      </span>
      <span slot="title">Running Nodes</span>
      <span slot="actions">
        <button
          class="rounded-md border border-gray-700 px-3 py-2 text-[10px] uppercase tracking-[0.05em] text-gray-300 transition hover:border-blue-500 hover:text-white"
          title="Planned — stop every running node"
          disabled
        >Stop all nodes</button>
      </span>
    </PageHeader>

    <section class="overflow-hidden rounded-2xl border border-gray-800 bg-gray-900/95" data-testid="nodes-table">
      <table class="w-full text-left text-sm">
        <thead>
          <tr class="border-b border-gray-800 text-[10px] uppercase tracking-[0.05em] text-gray-500">
            <th class="px-5 py-2.5">Node</th>
            <th class="px-3 py-2.5">Lab</th>
            <th class="px-3 py-2.5">Template</th>
            <th class="px-3 py-2.5">Type</th>
            <th class="px-3 py-2.5">CPU</th>
            <th class="px-3 py-2.5">Memory</th>
            <th class="px-3 py-2.5">Disk</th>
            <th class="px-3 py-2.5">Console</th>
            <th class="px-5 py-2.5 text-right">Actions</th>
          </tr>
        </thead>
        <tbody>
          {#each NODES as node (node.name)}
            <tr class="border-b border-gray-800/60 last:border-0 hover:bg-gray-800/40">
              <td class="px-5 py-3 font-medium text-gray-100">{node.name}</td>
              <td class="px-3 py-3 text-[12px] text-gray-400">{node.lab}</td>
              <td class="px-3 py-3 font-mono text-[12px] text-gray-300">{node.template}</td>
              <td class="px-3 py-3">
                <span class={`rounded-full border px-1.5 py-px text-[9px] uppercase tracking-[0.05em] ${typeChip[node.type] ?? 'border-gray-600 text-gray-400'}`}>{node.type}</span>
              </td>
              <td class="px-3 py-3">
                <div class="flex items-center gap-2">
                  <div class="h-1 w-14 overflow-hidden rounded-full bg-gray-800">
                    <div class={`h-full rounded-full ${node.cpu > 30 ? 'bg-amber-400' : 'bg-emerald-400'}`} style={`width: ${Math.min(100, node.cpu * 2)}%`}></div>
                  </div>
                  <span class="text-[12px] text-gray-300">{node.cpu}%</span>
                </div>
              </td>
              <td class="px-3 py-3 text-[12px] text-gray-300">{node.ramMb} MB</td>
              <td class="px-3 py-3 text-[12px] text-gray-300">{node.diskGb.toFixed(1)} GB</td>
              <td class="px-3 py-3 text-[12px] text-gray-400">{node.console}</td>
              <td class="px-5 py-3 text-right">
                <button class="rounded-md border border-gray-700 px-2.5 py-1.5 text-[10px] uppercase tracking-[0.05em] text-gray-300 transition hover:border-blue-500 hover:text-white" title="Planned" disabled>Console</button>
                <button class="ml-2 rounded-md border border-red-500/40 px-2.5 py-1.5 text-[10px] uppercase tracking-[0.05em] text-red-200 transition hover:bg-red-500/10" title="Planned" disabled>Stop</button>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </section>
  </div>
</div>
