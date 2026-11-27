<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  import InfoFields from './InfoFields.svelte';
  import InfoPanel from './InfoPanel.svelte';
  import NetworkInfoContent from './NetworkInfoContent.svelte';
  import { infoPanels, type InfoPanelEntry } from '$lib/stores/infoPanels';
  import type { NodeData, NetworkData } from '$lib/types';

  export let nodesById: Record<string, NodeData> = {};
  export let networksById: Record<string, NetworkData> = {};
  export let labId = '';
  export let zenMode = false;

  function nodeFields(node: NodeData): Array<{ label: string; value: string }> {
    const runtime = (node as unknown as Record<string, unknown>).runtime as
      | Record<string, unknown>
      | undefined;
    const ipsRaw = runtime?.['ip_addresses'];
    const pid = runtime?.['pid'];
    const ips = Array.isArray(ipsRaw)
      ? (ipsRaw as unknown[]).map((entry) => String(entry)).join(', ')
      : '';
    return [
      { label: 'ID', value: String(node.id) },
      { label: 'Type', value: String(node.type ?? '—') },
      { label: 'Template', value: String(node.template ?? '—') },
      { label: 'Console', value: String(node.console ?? '—') },
      { label: 'Image', value: String(node.image ?? '—') },
      { label: 'CPU', value: `${node.cpu ?? 0} vCPU` },
      { label: 'RAM', value: `${node.ram ?? 0} MB` },
      { label: 'NICs', value: String(node.interfaces?.length ?? node.ethernet ?? 0) },
      { label: 'Status', value: node.status === 2 ? 'running' : 'stopped' },
      ...(pid != null ? [{ label: 'PID', value: String(pid) }] : []),
      ...(ips ? [{ label: 'IPs', value: ips }] : []),
    ];
  }

  // #318: entering Zen hides the panels that were already open. Opening one
  // from a menu while in Zen raises it above this mark, which shows it.
  let zenBaseline = Infinity;
  let wasZen = false;
  $: {
    if (zenMode && !wasZen) {
      zenBaseline = Math.max(0, ...$infoPanels.map((entry) => entry.zIndex));
    }
    wasZen = zenMode;
  }

  function titleFor(entry: InfoPanelEntry): { title: string; subtitle: string } {
    if (entry.kind === 'node') {
      const node = nodesById[String(entry.refId)];
      return { title: node?.name ?? `node #${entry.refId}`, subtitle: 'Node' };
    }
    const network = networksById[String(entry.refId)];
    return { title: network?.name ?? `network #${entry.refId}`, subtitle: 'Network' };
  }
</script>

<div class="pointer-events-none fixed inset-0 z-30">
  {#each $infoPanels as entry (entry.id)}
    {@const meta = titleFor(entry)}
    {@const concealed = zenMode && entry.zIndex <= zenBaseline}
    <div hidden={concealed} class:contents={!concealed}>
      <InfoPanel
        id={entry.id}
        title={meta.title}
        subtitle={meta.subtitle}
        position={entry.position}
        zIndex={entry.zIndex}
        onClose={(id) => infoPanels.close(id)}
        onMove={(id, x, y) => infoPanels.move(id, x, y)}
        onFocus={(id) => infoPanels.bringToFront(id)}
      >
        {#if entry.kind === 'node'}
          {@const node = nodesById[String(entry.refId)]}
          {#if node}
            <InfoFields fields={nodeFields(node)} />
          {:else}
            <div class="text-gray-500">Node no longer exists.</div>
          {/if}
        {:else}
          {@const network = networksById[String(entry.refId)]}
          {#if network}
            <NetworkInfoContent {labId} {network} active={!concealed} />
          {:else}
            <div class="text-gray-500">Network no longer exists.</div>
          {/if}
        {/if}
      </InfoPanel>
    </div>
  {/each}
</div>
