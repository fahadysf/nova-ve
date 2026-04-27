<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import PortLayer from './PortLayer.svelte';
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
    highlightedInterfaceIndex?: number | null;
  };

  const dispatch = createEventDispatcher<{
    'port:mousedown': { nodeId: number; interfaceIndex: number; port: PortPosition; event: MouseEvent };
    'port:mouseup': { nodeId: number; interfaceIndex: number; port: PortPosition; event: MouseEvent };
    'port:mouseenter': { nodeId: number; interfaceIndex: number; port: PortPosition; event: MouseEvent };
    'port:mouseleave': { nodeId: number; interfaceIndex: number; port: PortPosition; event: MouseEvent };
    'port:dragend': { nodeId: number; interfaceIndex: number; port: PortPosition };
  }>();

  $: running = data.status === 2;
  $: transitioning = data.transientStatus === 'starting' || data.transientStatus === 'stopping';
  $: statusLabel = data.transientStatus ?? (running ? 'running' : 'stopped');
  $: typeLabel = (data.type || 'node').toUpperCase();
  $: interfaces = data.interfaces ?? [];
  $: nodeId = data.nodeId ?? 0;
</script>

<div
  class={`relative min-w-[9.5rem] rounded-xl border px-3 py-2 text-gray-100 shadow-lg shadow-black/20 ${
    transitioning
      ? 'border-amber-300/80 bg-gray-900'
      : running
        ? 'border-emerald-500/80 bg-gray-900'
        : 'border-gray-700 bg-gray-800/95'
  }`}
>
  <div class="flex items-start gap-2">
    <span
      class={`mt-0.5 h-2.5 w-2.5 shrink-0 rounded-full ${
        transitioning ? 'bg-amber-300' : running ? 'bg-emerald-500' : 'bg-gray-500'
      }`}
    ></span>
    <div class="min-w-0 flex-1">
      <div class="text-[9px] uppercase tracking-[0.05em] text-gray-500">{typeLabel}</div>
      <div class="mt-1 truncate text-[11px] font-semibold tracking-tight text-gray-100">{data.label}</div>
      <div class="mt-1 flex items-center justify-between gap-2">
        <span class="truncate font-mono text-[10px] text-gray-400">{data.template || data.console || 'template'}</span>
        <span
          class={`rounded-full px-1.5 py-0.5 text-[9px] uppercase tracking-[0.05em] ${
            transitioning
              ? 'bg-amber-300/20 text-amber-200'
              : running
                ? 'bg-emerald-500/20 text-emerald-200'
                : 'bg-gray-900 text-gray-300'
          }`}
        >
          {statusLabel}
        </span>
      </div>
    </div>
  </div>

  <PortLayer
    {nodeId}
    {interfaces}
    highlightedInterfaceIndex={data.highlightedInterfaceIndex ?? null}
    on:port:mousedown={(e) => dispatch('port:mousedown', e.detail)}
    on:port:mouseup={(e) => dispatch('port:mouseup', e.detail)}
    on:port:mouseenter={(e) => dispatch('port:mouseenter', e.detail)}
    on:port:mouseleave={(e) => dispatch('port:mouseleave', e.detail)}
    on:port:dragend={(e) => dispatch('port:dragend', e.detail)}
  />
</div>
