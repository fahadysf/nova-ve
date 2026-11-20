<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { placeInterfaces } from '$lib/services/portLayout';
  import type { NodeInterface, PortPosition } from '$lib/types';
  import Port from './Port.svelte';

  type NodeConnectionPointRef = { linkId: string; endpointKey: 'from' | 'to' };

  export let nodeId: number;
  export let nodeName: string | undefined = undefined;
  export let interfaces: NodeInterface[] = [];
  export let connectedInterfaceIndexes: number[] = [];
  export let portPositionsByInterfaceIndex: Record<string, PortPosition> = {};
  export let portRefsByInterfaceIndex: Record<string, NodeConnectionPointRef> = {};
  export let highlightedInterfaceIndex: number | null = null;
  export let highlightedNewConnection = false;

  const dispatch = createEventDispatcher<{
    'port:mousedown': { nodeId: number; interfaceIndex: number; port: PortPosition; event: MouseEvent };
    'port:mouseup': { nodeId: number; interfaceIndex: number; port: PortPosition; event: MouseEvent };
    'port:mouseenter': { nodeId: number; interfaceIndex: number; port: PortPosition; event: MouseEvent };
    'port:mouseleave': { nodeId: number; interfaceIndex: number; port: PortPosition; event: MouseEvent };
  }>();

  let layerEl: HTMLDivElement | null = null;

  // Connection points are fully automatic (#258): the canvas computes each
  // link's port (facing side toward the peer, spread with a minimum gap) and
  // unconnected interfaces fall back to the canonical perimeter distribution.
  // The legacy per-interface persisted position is no longer honored.
  $: defaults = placeInterfaces(interfaces.length);
  $: connectedSet = new Set(connectedInterfaceIndexes);
  $: resolvedPorts = interfaces.map((iface, index) => {
    const interfaceIndex = iface.index ?? index;
    const linkPosition = portPositionsByInterfaceIndex[String(interfaceIndex)];
    if (linkPosition) return linkPosition;
    return defaults[index] ?? ({ side: 'top', offset: 0 } as PortPosition);
  });
  $: visibleInterfaceEntries = interfaces
    .map((iface, index) => ({
      iface,
      index,
      interfaceIndex: iface.index ?? index,
      connected: connectedSet.has(iface.index ?? index) || (iface.network_id ?? 0) > 0,
    }))
    .filter((entry) => entry.connected);
  $: firstAvailableEntry =
    interfaces
      .map((iface, index) => ({
        iface,
        index,
        interfaceIndex: iface.index ?? index,
        connected: connectedSet.has(iface.index ?? index) || (iface.network_id ?? 0) > 0,
      }))
      .find((entry) => !entry.connected) ?? null;

  function handlePortMouseDown(detail: {
    event: MouseEvent;
    nodeId: number;
    interfaceIndex: number;
    port: PortPosition;
  }) {
    dispatch('port:mousedown', detail);
  }
</script>

<div bind:this={layerEl} class="pointer-events-none absolute inset-0">
  {#each visibleInterfaceEntries as entry (entry.interfaceIndex)}
    {@const port = resolvedPorts[entry.index]}
    <div class="pointer-events-auto absolute inset-0">
      <Port
        interfaceData={entry.iface}
        position={port}
        {nodeId}
        {nodeName}
        interfaceIndex={entry.interfaceIndex}
        connectionPointRef={portRefsByInterfaceIndex[String(entry.interfaceIndex)]}
        connected={true}
        highlighted={highlightedInterfaceIndex === entry.interfaceIndex}
        on:port:mousedown={(e) => handlePortMouseDown(e.detail)}
        on:port:mouseup={(e) => dispatch('port:mouseup', e.detail)}
        on:port:mouseenter={(e) => dispatch('port:mouseenter', e.detail)}
        on:port:mouseleave={(e) => dispatch('port:mouseleave', e.detail)}
      />
    </div>
  {/each}
  {#if firstAvailableEntry}
    {@const port = resolvedPorts[firstAvailableEntry.index]}
    <div class="pointer-events-auto absolute inset-0">
      <Port
        interfaceData={firstAvailableEntry.iface}
        position={port}
        {nodeId}
        {nodeName}
        interfaceIndex={firstAvailableEntry.interfaceIndex}
        connected={false}
        newConnectionSlot={true}
        highlighted={highlightedNewConnection}
        on:port:mousedown={(e) => handlePortMouseDown(e.detail)}
        on:port:mouseup={(e) => dispatch('port:mouseup', e.detail)}
        on:port:mouseenter={(e) => dispatch('port:mouseenter', e.detail)}
        on:port:mouseleave={(e) => dispatch('port:mouseleave', e.detail)}
      />
    </div>
  {/if}
</div>
