<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<!-- System logs (#265). Static preview: file picker + pattern + tail view. -->
<script lang="ts">
  import PageHeader from '$lib/components/shell/PageHeader.svelte';

  const LOG_FILES = ['backend.log', 'caddy-access.log', 'guacamole.log', 'nova-ve-sweep.log'];
  let selected = LOG_FILES[0];
  let pattern = '';
  let lineCount = 200;

  const SAMPLE_LINES = [
    { ts: '14:02:11', level: 'INFO', msg: 'GET /api/labs/st-demo/ST-Demo.json 200 (uicheck)' },
    { ts: '14:02:09', level: 'INFO', msg: 'lab st-demo: topology reconciliation — no drift' },
    { ts: '14:01:58', level: 'WARN', msg: 'node vm-linux console attach retry 1/3' },
    { ts: '14:01:58', level: 'INFO', msg: 'node vm-linux console attached (telnet :5001)' },
    { ts: '14:01:40', level: 'INFO', msg: 'link lnk_004 created net=net-bridge node=iol-rtr iface=1' },
    { ts: '14:01:22', level: 'ERROR', msg: 'host_net: bridge nove9930n1 recreate took 2.4s (slow path)' },
  ];

  $: query = pattern.trim().toLowerCase();
  $: lines = SAMPLE_LINES.filter((l) => !query || l.msg.toLowerCase().includes(query));

  const levelColor: Record<string, string> = {
    INFO: 'text-gray-400',
    WARN: 'text-amber-300',
    ERROR: 'text-red-300',
  };
</script>

<div class="p-6">
  <div class="space-y-6">
    <PageHeader issue={265}>
      <span slot="kicker" class="text-[10px] uppercase tracking-[0.05em] text-gray-500">
        System · Preview data
      </span>
      <span slot="title">Logs</span>
    </PageHeader>

    <section class="flex flex-wrap items-center gap-2 rounded-2xl border border-gray-800 bg-gray-900/95 px-4 py-3">
      <select bind:value={selected} class="rounded-lg border border-gray-700 bg-gray-950 px-2 py-1.5 text-[12px] text-gray-200 outline-none focus:border-blue-500" data-testid="logs-file-select">
        {#each LOG_FILES as file}
          <option value={file}>{file}</option>
        {/each}
      </select>
      <input type="search" placeholder="Search pattern…" bind:value={pattern} class="w-56 rounded-lg border border-gray-700 bg-gray-950 px-3 py-1.5 text-[12px] text-gray-200 outline-none focus:border-blue-500" data-testid="logs-pattern" />
      <select bind:value={lineCount} class="rounded-lg border border-gray-700 bg-gray-950 px-2 py-1.5 text-[12px] text-gray-200 outline-none focus:border-blue-500">
        {#each [100, 200, 500, 1000] as count}
          <option value={count}>{count} lines</option>
        {/each}
      </select>
    </section>

    <section class="overflow-hidden rounded-2xl border border-gray-800 bg-gray-950/70" data-testid="logs-viewer">
      <div class="max-h-[28rem] overflow-y-auto p-4 font-mono text-[11px] leading-relaxed">
        {#each lines as line (line.ts + line.msg)}
          <div class="flex gap-3">
            <span class="text-gray-600">{line.ts}</span>
            <span class={`w-10 shrink-0 ${levelColor[line.level]}`}>{line.level}</span>
            <span class="text-gray-300">{line.msg}</span>
          </div>
        {/each}
        {#if lines.length === 0}
          <div class="py-8 text-center text-gray-600">No lines match the pattern.</div>
        {/if}
      </div>
    </section>
  </div>
</div>
