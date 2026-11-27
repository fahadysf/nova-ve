<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { Handle, Position } from '@xyflow/svelte';
  import { Container, Info, Play, Router, Server, Square } from 'lucide-svelte';
  import PortLayer from './PortLayer.svelte';
  import { infoPanels } from '$lib/stores/infoPanels';
  import { dragLinkStore } from '$lib/stores/dragLink';
  import type { NodeInterface, PortPosition } from '$lib/types';

  type NodeConnectionPointRef = { linkId: string; endpointKey: 'from' | 'to' };

  export let data: {
    label: string;
    iconSources?: string[];
    status?: number;
    transientStatus?: 'starting' | 'stopping';
    onLifecycleAction?: (nodeId: number, action: 'start' | 'stop') => void;
    type?: string;
    template?: string;
    console?: string;
    nodeId?: number;
    interfaces?: NodeInterface[];
    connectedInterfaceIndexes?: number[];
    portPositionsByInterfaceIndex?: Record<string, PortPosition>;
    portRefsByInterfaceIndex?: Record<string, NodeConnectionPointRef>;
    /** Canvas-computed position for the dotted new-connection slot (#258). */
    newConnectionPortPosition?: PortPosition | null;
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

  function runLifecycle(event: MouseEvent) {
    event.stopPropagation();
    if (!transitioning && typeof nodeId === 'number') {
      data.onLifecycleAction?.(nodeId, running ? 'stop' : 'start');
    }
  }

  // Flat neutral card face (#254): type is carried by the icon glyph only —
  // no type/template text on the node face.
  const typeIcons = { docker: Container, iol: Router, dynamips: Router } as const;
  $: typeIcon = (data.type && typeIcons[data.type as keyof typeof typeIcons]) || Server;

  let failedIconSources = new Set<string>();
  let loadedIconSource: string | undefined;
  $: iconSource = data.iconSources?.find((source) => !failedIconSources.has(source));

  function onIconError(event: Event) {
    const image = event.currentTarget as HTMLImageElement;
    const source = image.getAttribute('src');
    if (source) failedIconSources = new Set([...failedIconSources, source]);
  }

  $: running = data.status === 2;
  $: transitioning = data.transientStatus === 'starting' || data.transientStatus === 'stopping';
  $: interfaces = data.interfaces ?? [];
  $: nodeId = data.nodeId ?? 0;

  // #274: run-state outline on the icon container, in addition to the
  // status dot. Ring colors track the dot palette (amber-300 transient,
  // emerald-400 running, gray-500 stopped); box-shadow keeps the card's
  // layout and hit area untouched.
  $: iconStateRing = transitioning
    ? 'shadow-[0_0_0_2px_#fcd34d]'
    : running
      ? 'shadow-[0_0_0_2px_#34d399]'
      : 'shadow-[0_0_0_2px_#6b7280]';
</script>

<div
  class="nb-card group relative min-w-[10.5rem] rounded-lg border border-[var(--card-canvas-border)] bg-[var(--card-canvas-bg)] px-3.5 py-2 transition-colors hover:border-[var(--card-canvas-border-hover)] hover:bg-[var(--card-canvas-bg-hover)]"
  data-testid="custom-node-card"
  data-link-dragging={['port_pressed', 'dragging', 'near_target'].includes($dragLinkStore.state)}
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
      data-testid="node-icon-container"
      class={`flex h-11 w-11 shrink-0 items-center justify-center rounded-lg bg-[var(--card-canvas-icon-bg)] text-[var(--card-canvas-sub-fg)] transition-colors group-hover:bg-[var(--card-canvas-icon-bg-hover)] group-hover:text-[var(--card-canvas-name-fg)] ${iconStateRing}`}
    >
      {#if iconSource}
        {#key iconSource}
          <img
            src={iconSource}
            alt=""
            draggable="false"
            class="h-6 w-6 select-none object-contain"
            class:hidden={loadedIconSource !== iconSource}
            on:load={() => (loadedIconSource = iconSource)}
            on:error={onIconError}
          />
        {/key}
      {/if}
      {#if !iconSource || loadedIconSource !== iconSource}
        <svelte:component this={typeIcon} class="h-6 w-6" />
      {/if}
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
    newConnectionPortPosition={data.newConnectionPortPosition ?? null}
    highlightedInterfaceIndex={data.highlightedInterfaceIndex ?? null}
    highlightedNewConnection={data.highlightedNewConnection ?? false}
    on:port:mousedown={(e) => dispatch('port:mousedown', e.detail)}
    on:port:mouseup={(e) => dispatch('port:mouseup', e.detail)}
    on:port:mouseenter={(e) => dispatch('port:mouseenter', e.detail)}
    on:port:mouseleave={(e) => dispatch('port:mouseleave', e.detail)}
  />

  {#if data.onLifecycleAction}
    <button
      type="button"
      class="node-lifecycle nodrag absolute right-1 top-1 inline-flex h-6 w-6 items-center justify-center rounded-md border border-[var(--card-canvas-border)] bg-[var(--card-canvas-bg)] text-[var(--card-canvas-sub-fg)] hover:text-[var(--card-canvas-name-fg)] disabled:cursor-wait"
      class:node-lifecycle-pending={transitioning}
      disabled={transitioning}
      aria-label={`${transitioning ? (data.transientStatus === 'starting' ? 'Starting' : 'Stopping') : (running ? 'Stop' : 'Start')} ${data.label}`}
      title={transitioning ? (data.transientStatus === 'starting' ? 'Starting' : 'Stopping') : (running ? 'Stop node' : 'Start node')}
      data-testid="node-lifecycle-button"
      on:click={runLifecycle}
      on:pointerdown|stopPropagation
      on:mousedown|stopPropagation
      on:mouseup|stopPropagation
    >
      {#if transitioning}
        <span class="h-2 w-2 animate-pulse rounded-full bg-amber-300" aria-hidden="true"></span>
      {:else if running}
        <Square class="h-3 w-3" aria-hidden="true" />
      {:else}
        <Play class="h-3 w-3" aria-hidden="true" />
      {/if}
    </button>
  {/if}

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
  /* Keep the connection points mounted: Svelte Flow measures these anchors. */
  @media (hover: hover) and (pointer: fine) {
    .nb-card:not(:hover):not(:focus-within) .node-lifecycle:not(.node-lifecycle-pending) {
      opacity: 0;
    }
    .nb-card:where(:not(:hover):not(:focus-within):not([data-link-dragging='true'])) :global(.port-affordance) {
      opacity: 0;
    }
    :global(.svelte-flow__node:focus-within) .nb-card :global(.port-affordance) {
      opacity: 1;
    }
  }
  /* Selection halo (#254) — Svelte Flow marks the wrapping element with
     .selected; ring the card itself so the halo hugs the rounded box. */
  :global(.svelte-flow__node.selected) .nb-card {
    box-shadow: 0 0 0 2px var(--card-canvas-halo);
  }
</style>
