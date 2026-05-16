<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { Handle, Position } from '@xyflow/svelte';
  import { Info } from 'lucide-svelte';
  import PortLayer from './PortLayer.svelte';
  import { infoPanels } from '$lib/stores/infoPanels';
  import type { NodeInterface, PortPosition } from '$lib/types';

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
    highlightedInterfaceIndex?: number | null;
    highlightedNewConnection?: boolean;
  };

  const dispatch = createEventDispatcher<{
    'port:mousedown': { nodeId: number; interfaceIndex: number; port: PortPosition; event: MouseEvent };
    'port:mouseup': { nodeId: number; interfaceIndex: number; port: PortPosition; event: MouseEvent };
    'port:mouseenter': { nodeId: number; interfaceIndex: number; port: PortPosition; event: MouseEvent };
    'port:mouseleave': { nodeId: number; interfaceIndex: number; port: PortPosition; event: MouseEvent };
    'port:dragend': { nodeId: number; interfaceIndex: number; port: PortPosition };
  }>();

  function openInfo(event: MouseEvent) {
    event.stopPropagation();
    if (typeof nodeId === 'number') {
      infoPanels.open('node', nodeId);
    }
  }

  $: running = data.status === 2;
  $: transitioning = data.transientStatus === 'starting' || data.transientStatus === 'stopping';
  $: interfaces = data.interfaces ?? [];
  $: nodeId = data.nodeId ?? 0;
</script>

<div
  class={`relative min-w-[9.5rem] rounded-xl border px-3 py-2.5 text-gray-100 shadow-lg shadow-black/20 ${
    transitioning
      ? 'border-amber-300/80 bg-gray-900'
      : running
        ? 'border-emerald-500/80 bg-gray-900'
        : 'border-gray-700 bg-gray-800/95'
  }`}
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
  <div class="pb-3 pl-4 pr-1">
    <div
      class="min-w-0 flex-1 break-words text-[10px] font-semibold leading-snug text-gray-100"
      title={data.label}
    >
      {data.label}
    </div>
  </div>

  <PortLayer
    {nodeId}
    nodeName={data.label}
    {interfaces}
    connectedInterfaceIndexes={data.connectedInterfaceIndexes ?? []}
    highlightedInterfaceIndex={data.highlightedInterfaceIndex ?? null}
    highlightedNewConnection={data.highlightedNewConnection ?? false}
    on:port:mousedown={(e) => dispatch('port:mousedown', e.detail)}
    on:port:mouseup={(e) => dispatch('port:mouseup', e.detail)}
    on:port:mouseenter={(e) => dispatch('port:mouseenter', e.detail)}
    on:port:mouseleave={(e) => dispatch('port:mouseleave', e.detail)}
    on:port:dragend={(e) => dispatch('port:dragend', e.detail)}
  />

  <button
    type="button"
    class="nodrag absolute bottom-1 left-1 inline-flex h-4 w-4 items-center justify-center rounded-full border border-gray-700 bg-gray-900/80 text-gray-400 transition hover:border-blue-500/40 hover:text-white"
    aria-label="Open node info panel"
    title="Open info panel"
    data-testid="node-info-button"
    on:click={openInfo}
    on:pointerdown|stopPropagation
  >
    <Info class="h-3 w-3" />
  </button>
</div>
