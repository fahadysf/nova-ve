<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { Handle, Position } from '@xyflow/svelte';
  import { Container, Info, Router, Server } from 'lucide-svelte';
  import PortLayer from './PortLayer.svelte';
  import { infoPanels } from '$lib/stores/infoPanels';
  import type { NodeInterface, PortPosition } from '$lib/types';

  type NodeConnectionPointRef = { linkId: string; endpointKey: 'from' | 'to' };

  export let data: {
    label: string;
    icon?: string;
    status?: number;
    transientStatus?: 'starting' | 'stopping';
    type?: string;
    template?: string;
    console?: string;
    nodeId?: number;
    interfaces?: NodeInterface[];
    connectedInterfaceIndexes?: number[];
    portPositionsByInterfaceIndex?: Record<string, PortPosition>;
    portRefsByInterfaceIndex?: Record<string, NodeConnectionPointRef>;
    highlightedInterfaceIndex?: number | null;
    highlightedNewConnection?: boolean;
  };

  const dispatch = createEventDispatcher<{
    'port:mousedown': { nodeId: number; interfaceIndex: number; port: PortPosition; event: MouseEvent };
    'port:mouseup': { nodeId: number; interfaceIndex: number; port: PortPosition; event: MouseEvent };
    'port:mouseenter': { nodeId: number; interfaceIndex: number; port: PortPosition; event: MouseEvent };
    'port:mouseleave': { nodeId: number; interfaceIndex: number; port: PortPosition; event: MouseEvent };
  }>();

  function openInfo(event: MouseEvent) {
    event.stopPropagation();
    if (typeof nodeId === 'number') {
      infoPanels.open('node', nodeId);
    }
  }

  // Flat neutral card face (#254): type is carried by the icon glyph only —
  // no type/template text on the node face.
  const typeIcons = { docker: Container, iol: Router, dynamips: Router } as const;
  $: typeIcon = (data.type && typeIcons[data.type as keyof typeof typeIcons]) || Server;

  $: running = data.status === 2;
  $: transitioning = data.transientStatus === 'starting' || data.transientStatus === 'stopping';
  $: interfaces = data.interfaces ?? [];
  $: nodeId = data.nodeId ?? 0;
</script>

<div
  class="nb-card group relative min-w-[10.5rem] rounded-lg border border-[var(--card-canvas-border)] bg-[var(--card-canvas-bg)] px-3.5 py-2 transition-colors hover:border-[var(--card-canvas-border-hover)] hover:bg-[var(--card-canvas-bg-hover)]"
  data-testid="custom-node-card"
>
  <Handle
    type="source"
    position={Position.Right}
    id="default"
    class="!h-1 !w-1 !border-0 !bg-transparent !opacity-0 !pointer-events-none"
  />
  <Handle
    type="target"
    position={Position.Left}
    id="default"
    class="!h-1 !w-1 !border-0 !bg-transparent !opacity-0 !pointer-events-none"
  />
  <div class="flex items-center gap-2.5 py-1.5 pr-2">
    <div
      class="flex h-11 w-11 shrink-0 items-center justify-center rounded-lg bg-[var(--card-canvas-icon-bg)] text-[var(--card-canvas-sub-fg)] transition-colors group-hover:bg-[var(--card-canvas-icon-bg-hover)] group-hover:text-[var(--card-canvas-name-fg)]"
    >
      <svelte:component this={typeIcon} class="h-5 w-5" />
    </div>
    <div class="flex min-w-0 flex-col justify-center leading-tight">
      <div class="flex items-center gap-1.5" title={data.label}>
        <span
          data-testid="node-status-dot"
          class={`h-2 w-2 shrink-0 rounded-full transition-colors ${
            transitioning
              ? 'animate-pulse bg-amber-300'
              : running
                ? 'bg-emerald-400'
                : 'bg-gray-500'
          }`}
        ></span>
        <span class="truncate text-[13px] font-normal text-[var(--card-canvas-name-fg)]">
          {data.label}
        </span>
      </div>
      <div class="mt-0.5 whitespace-nowrap text-[11px] font-normal text-[var(--card-canvas-sub-fg)]">
        {interfaces.length} NIC{interfaces.length === 1 ? '' : 's'} · {data.console ?? 'telnet'}
      </div>
    </div>
  </div>

  <PortLayer
    {nodeId}
    nodeName={data.label}
    {interfaces}
    connectedInterfaceIndexes={data.connectedInterfaceIndexes ?? []}
    portPositionsByInterfaceIndex={data.portPositionsByInterfaceIndex ?? {}}
    portRefsByInterfaceIndex={data.portRefsByInterfaceIndex ?? {}}
    highlightedInterfaceIndex={data.highlightedInterfaceIndex ?? null}
    highlightedNewConnection={data.highlightedNewConnection ?? false}
    on:port:mousedown={(e) => dispatch('port:mousedown', e.detail)}
    on:port:mouseup={(e) => dispatch('port:mouseup', e.detail)}
    on:port:mouseenter={(e) => dispatch('port:mouseenter', e.detail)}
    on:port:mouseleave={(e) => dispatch('port:mouseleave', e.detail)}
  />

  <button
    type="button"
    class="nodrag absolute bottom-1 right-1 inline-flex h-3 w-3 items-center justify-center rounded-full border border-[var(--card-canvas-border)] bg-[var(--card-canvas-bg-hover)] text-[var(--card-canvas-sub-fg)] transition-colors hover:border-[var(--card-canvas-border-hover)] hover:text-[var(--card-canvas-name-fg)]"
    aria-label="Open node info panel"
    title="Open info panel"
    data-testid="node-info-button"
    on:click={openInfo}
    on:pointerdown|stopPropagation
  >
    <Info class="h-2 w-2" />
  </button>
</div>

<style>
  /* Selection halo (#254) — Svelte Flow marks the wrapping element with
     .selected; ring the card itself so the halo hugs the rounded box. */
  :global(.svelte-flow__node.selected) .nb-card {
    box-shadow: 0 0 0 2px var(--card-canvas-halo);
  }
</style>
