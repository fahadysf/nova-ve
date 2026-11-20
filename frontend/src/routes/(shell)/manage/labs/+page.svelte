<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<!--
  Lab management (#262). Static preview: running/stopped lab instances with
  aggregate resource usage and lifecycle actions.
-->
<script lang="ts">
  import PageHeader from '$lib/components/shell/PageHeader.svelte';

  const LABS = [
    {
      name: 'ST-Demo',
      path: '/st-demo/ST-Demo.json',
      state: 'running',
      owner: 'uicheck',
      nodes: 4,
      vcpu: 5,
      ramGb: 6.5,
      diskGb: 2.1,
    },
    {
      name: 'arc-lab',
      path: '/edge/arc-lab.json',
      state: 'running',
      owner: 'admin',
      nodes: 6,
      vcpu: 9,
      ramGb: 18.0,
      diskGb: 7.4,
    },
    {
      name: 'switch-lab',
      path: '/training/switch-lab.json',
      state: 'stopped',
      owner: 'nettrain',
      nodes: 3,
      vcpu: 0,
      ramGb: 0,
      diskGb: 1.2,
    },
  ];

  $: runningCount = LABS.filter((l) => l.state === 'running').length;
  $: totalVcpu = LABS.reduce((sum, l) => sum + l.vcpu, 0);
  $: totalRam = LABS.reduce((sum, l) => sum + l.ramGb, 0);
</script>

<div class="p-6">
  <div class="space-y-6">
    <PageHeader issue={262}>
      <span slot="kicker" class="text-[10px] uppercase tracking-[0.05em] text-gray-500">
        Management · Preview data
      </span>
      <span slot="title">Running Labs</span>
      <span slot="actions">
        <button
          class="rounded-md border border-gray-700 px-3 py-2 text-[10px] uppercase tracking-[0.05em] text-gray-300 transition hover:border-blue-500 hover:text-white"
          title="Planned — stop every running lab"
          disabled
        >Stop all</button>
      </span>
    </PageHeader>

    <section class="grid grid-cols-1 gap-4 sm:grid-cols-3">
      <div class="rounded-xl border border-gray-800 bg-gray-900/95 px-4 py-3">
        <div class="text-[10px] uppercase tracking-[0.05em] text-gray-500">Labs running</div>
        <div class="mt-1 text-xl font-semibold text-gray-100">{runningCount} / {LABS.length}</div>
      </div>
      <div class="rounded-xl border border-gray-800 bg-gray-900/95 px-4 py-3">
        <div class="text-[10px] uppercase tracking-[0.05em] text-gray-500">vCPU in use</div>
        <div class="mt-1 text-xl font-semibold text-gray-100">{totalVcpu}</div>
      </div>
      <div class="rounded-xl border border-gray-800 bg-gray-900/95 px-4 py-3">
        <div class="text-[10px] uppercase tracking-[0.05em] text-gray-500">Memory in use</div>
        <div class="mt-1 text-xl font-semibold text-gray-100">{totalRam.toFixed(1)} GB</div>
      </div>
    </section>

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
            <th class="px-3 py-2.5">Disk</th>
            <th class="px-5 py-2.5 text-right">Actions</th>
          </tr>
        </thead>
        <tbody>
          {#each LABS as lab (lab.path)}
            <tr class="border-b border-gray-800/60 last:border-0 hover:bg-gray-800/40">
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
              <td class="px-3 py-3 text-[12px] text-gray-400">{lab.owner}</td>
              <td class="px-3 py-3 text-[12px] text-gray-300">{lab.nodes}</td>
              <td class="px-3 py-3 text-[12px] text-gray-300">{lab.vcpu}</td>
              <td class="px-3 py-3 text-[12px] text-gray-300">{lab.ramGb.toFixed(1)} GB</td>
              <td class="px-3 py-3 text-[12px] text-gray-300">{lab.diskGb.toFixed(1)} GB</td>
              <td class="px-5 py-3 text-right">
                <button class="rounded-md border border-gray-700 px-2.5 py-1.5 text-[10px] uppercase tracking-[0.05em] text-gray-300 transition hover:border-blue-500 hover:text-white" title="Planned" disabled>Open</button>
                <button class="ml-2 rounded-md border border-red-500/40 px-2.5 py-1.5 text-[10px] uppercase tracking-[0.05em] text-red-200 transition hover:bg-red-500/10" title="Planned" disabled>Stop</button>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </section>
  </div>
</div>
