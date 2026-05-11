<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  import InfoPanel from './InfoPanel.svelte';
  import { infoPanels, type InfoPanelEntry } from '$lib/stores/infoPanels';
  import type { NodeData, NetworkData } from '$lib/types';

  export let nodesById: Record<string, NodeData> = {};
  export let networksById: Record<string, NetworkData> = {};

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

  function networkFields(network: NetworkData): Array<{ label: string; value: string }> {
    const runtime = (network as unknown as Record<string, unknown>).runtime as
      | Record<string, unknown>
      | undefined;
    const bridge = runtime?.['bridge_name'];
    const driver = runtime?.['driver'];
    const createdAt = runtime?.['created_at'];
    return [
      { label: 'ID', value: String(network.id) },
      { label: 'Type', value: String(network.type ?? '—') },
      { label: 'Links', value: String(network.count ?? 0) },
      { label: 'Visible', value: network.visibility === false ? 'no' : 'yes' },
      ...(bridge ? [{ label: 'Bridge', value: String(bridge) }] : []),
      ...(driver ? [{ label: 'Driver', value: String(driver) }] : []),
      ...(createdAt ? [{ label: 'Created', value: String(createdAt) }] : []),
      { label: 'Position', value: `${network.left ?? 0}, ${network.top ?? 0}` },
    ];
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
          <dl class="grid grid-cols-[5.5rem_1fr] gap-x-3 gap-y-1.5">
            {#each nodeFields(node) as field}
              <dt class="text-[10px] uppercase tracking-[0.05em] text-gray-500">{field.label}</dt>
              <dd class="truncate font-mono text-[11px] text-gray-200" title={field.value}>{field.value}</dd>
            {/each}
          </dl>
        {:else}
          <div class="text-[11px] text-gray-500">Node no longer exists.</div>
        {/if}
      {:else}
        {@const network = networksById[String(entry.refId)]}
        {#if network}
          <dl class="grid grid-cols-[5.5rem_1fr] gap-x-3 gap-y-1.5">
            {#each networkFields(network) as field}
              <dt class="text-[10px] uppercase tracking-[0.05em] text-gray-500">{field.label}</dt>
              <dd class="truncate font-mono text-[11px] text-gray-200" title={field.value}>{field.value}</dd>
            {/each}
          </dl>
        {:else}
          <div class="text-[11px] text-gray-500">Network no longer exists.</div>
        {/if}
      {/if}
    </InfoPanel>
  {/each}
</div>
