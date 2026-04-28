<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  import { createEventDispatcher, getContext, onDestroy } from 'svelte';
  import type { Readable } from 'svelte/store';
  import { Handle, Position, useNodesData, useStore } from '@xyflow/svelte';
  import { formatInterfaceName } from '$lib/services/interfaceNaming';
  import type { LiveMacState, NodeInterface, PortPosition } from '$lib/types';
  import { dragLinkStore, getDragLinkSnapshot } from '$lib/stores/dragLink';
  import { togglePortInfo } from '$lib/stores/portInfo';
  import PortTooltip from './PortTooltip.svelte';

  // US-079: a click is a mousedown+mouseup pair within this time/distance
  // window. Anything longer or further is a drag and falls through to the
  // dragLinkStore flow.
  const CLICK_MAX_MS = 200;
  const CLICK_MAX_DIST_PX = 4;

  export let interfaceData: NodeInterface;
  export let position: PortPosition;
  export let nodeId: number;
  export let interfaceIndex = interfaceData.index ?? 0;
  export let isSource = false;
  export let highlighted = false;
  export let liveMac: LiveMacState | undefined = undefined;
  export let interfaceNamingScheme: string | null | undefined = undefined;

  const dispatch = createEventDispatcher<{
    'port:mousedown': { event: MouseEvent; nodeId: number; interfaceIndex: number; port: PortPosition };
    'port:mouseup': { event: MouseEvent; nodeId: number; interfaceIndex: number; port: PortPosition };
    'port:mouseenter': { event: MouseEvent; nodeId: number; interfaceIndex: number; port: PortPosition };
    'port:mouseleave': { event: MouseEvent; nodeId: number; interfaceIndex: number; port: PortPosition };
    'port:dragstart': { event: MouseEvent; nodeId: number; interfaceIndex: number };
    'port:click': { nodeId: number; interfaceIndex: number; anchorRect: DOMRect | null };
  }>();

  let hoverTimer: ReturnType<typeof setTimeout> | null = null;
  let showTooltip = false;
  let portHandle: HTMLSpanElement | null = null;
  let anchorRect: DOMRect | null = null;
  // US-079: track hover for the 50% scale-up. The transform lives on the inner
  // <span> so the wrapper's hit-test geometry stays the same.
  let isHovered = false;
  // US-079: click-vs-drag detection state. Captured on mousedown, consulted on
  // mouseup. ``clickStartCoords`` doubles as a "did we receive mousedown" flag.
  let clickStartTs = 0;
  let clickStartCoords: { x: number; y: number } | null = null;
  let currentNodeData: { interface_naming_scheme?: string | null } | null = null;
  let unsubscribeCurrentNode: (() => void) | null = null;

  $: stylePosition = (() => {
    const offsetPct = `${(position.offset * 100).toFixed(2)}%`;
    switch (position.side) {
      case 'top':
        return `top: 0; left: ${offsetPct}; transform: translate(-50%, -50%);`;
      case 'right':
        return `right: 0; top: ${offsetPct}; transform: translate(50%, -50%);`;
      case 'bottom':
        return `bottom: 0; left: ${offsetPct}; transform: translate(-50%, 50%);`;
      case 'left':
      default:
        return `left: 0; top: ${offsetPct}; transform: translate(-50%, -50%);`;
    }
  })();

  $: tooltipPlacement = (() => {
    switch (position.side) {
      case 'top':
        return 'top' as const;
      case 'bottom':
        return 'bottom' as const;
      case 'right':
        return 'right' as const;
      case 'left':
      default:
        return 'left' as const;
    }
  })();

  $: hasMismatch = liveMac?.state === 'mismatch';

  // Only render the XYFlow Handle when we are actually inside a SvelteFlow
  // component tree. Outside that context (e.g. unit tests) the Handle calls
  // useStore() and throws; the guard makes Port test-safe in isolation.
  let insideFlow = false;
  const flowNodeId = getContext<string | undefined>('svelteflow__node_id');
  let currentNodeStore: Readable<{ data: { interface_naming_scheme?: string | null } } | null> | null = null;
  try {
    useStore();
    insideFlow = true;
    if (flowNodeId) {
      currentNodeStore = useNodesData(flowNodeId) as Readable<{
        data: { interface_naming_scheme?: string | null };
      } | null>;
    }
  } catch {
    insideFlow = false;
  }
  if (currentNodeStore) {
    unsubscribeCurrentNode = currentNodeStore.subscribe((value) => {
      currentNodeData = value?.data ?? null;
    });
  }

  $: resolvedInterfaceNamingScheme =
    interfaceNamingScheme !== undefined
      ? interfaceNamingScheme
      : currentNodeData?.interface_naming_scheme;
  $: renderedInterfaceName =
    formatInterfaceName(resolvedInterfaceNamingScheme, interfaceIndex) || interfaceData.name;

  $: xyPosition = (() => {
    switch (position.side) {
      case 'top': return Position.Top;
      case 'right': return Position.Right;
      case 'bottom': return Position.Bottom;
      case 'left':
      default: return Position.Left;
    }
  })();

  function clearHoverTimer() {
    if (hoverTimer !== null) {
      clearTimeout(hoverTimer);
      hoverTimer = null;
    }
  }

  function captureAnchorRect() {
    anchorRect = portHandle ? portHandle.getBoundingClientRect() : null;
  }

  function handleMouseEnter(event: MouseEvent) {
    clearHoverTimer();
    isHovered = true;
    hoverTimer = setTimeout(() => {
      captureAnchorRect();
      showTooltip = true;
    }, 250);
    dispatch('port:mouseenter', { event, nodeId, interfaceIndex, port: position });

    const snap = getDragLinkSnapshot();
    if (snap.state === 'idle' || snap.state === 'confirming') return;
    if (snap.source && snap.source.kind === 'interface' && snap.source.nodeId === nodeId) return;
    dragLinkStore.move({
      pointer: snap.pointer ?? { x: event.clientX, y: event.clientY },
      target: {
        kind: 'interface',
        nodeId,
        interfaceIndex,
        port: position,
        interfaceName: renderedInterfaceName,
        plannedMac: interfaceData.planned_mac ?? null,
      },
      nearTarget: true,
    });
  }

  function handleMouseLeave(event: MouseEvent) {
    clearHoverTimer();
    isHovered = false;
    showTooltip = false;
    dispatch('port:mouseleave', { event, nodeId, interfaceIndex, port: position });

    const snap = getDragLinkSnapshot();
    if (snap.state === 'near_target') {
      dragLinkStore.leaveTargetZone();
    }
  }

  function handleMouseDown(event: MouseEvent) {
    if (event.button !== 0) return;

    if (!event.shiftKey) {
      event.stopPropagation();
      event.preventDefault();
    }

    clearHoverTimer();
    showTooltip = false;

    if (!event.shiftKey) {
      clickStartTs = Date.now();
      clickStartCoords = { x: event.clientX, y: event.clientY };
      dragLinkStore.start({
        source: {
          kind: 'interface',
          nodeId,
          interfaceIndex,
          port: position,
          interfaceName: renderedInterfaceName,
          plannedMac: interfaceData.planned_mac ?? null,
        },
        pointer: { x: event.clientX, y: event.clientY },
      });
    }

    dispatch('port:mousedown', { event, nodeId, interfaceIndex, port: position });
  }

  function handleMouseUp(event: MouseEvent) {
    dispatch('port:mouseup', { event, nodeId, interfaceIndex, port: position });

    const startCoords = clickStartCoords;
    const startTs = clickStartTs;
    clickStartCoords = null;
    clickStartTs = 0;
    if (startCoords) {
      const elapsed = Date.now() - startTs;
      const dist = Math.hypot(event.clientX - startCoords.x, event.clientY - startCoords.y);
      if (elapsed <= CLICK_MAX_MS && dist <= CLICK_MAX_DIST_PX) {
        const snapBeforeCancel = getDragLinkSnapshot();
        if (snapBeforeCancel.state === 'port_pressed') {
          dragLinkStore.cancel();
        }
        captureAnchorRect();
        const rect = anchorRect;
        dispatch('port:click', { nodeId, interfaceIndex, anchorRect: rect });
        togglePortInfo({
          kind: 'interface',
          nodeId,
          interfaceIndex,
          anchorRect: rect,
          interfaceName: renderedInterfaceName,
          plannedMac: interfaceData.planned_mac ?? null,
        });
        return;
      }
    }

    const snap = getDragLinkSnapshot();
    if (snap.state !== 'dragging' && snap.state !== 'near_target' && snap.state !== 'port_pressed') {
      return;
    }
    if (!snap.source) {
      dragLinkStore.cancel();
      return;
    }
    if (snap.source.kind === 'interface' && snap.source.nodeId === nodeId) {
      dragLinkStore.cancel();
      return;
    }
    dragLinkStore.move({
      pointer: { x: event.clientX, y: event.clientY },
      target: {
        kind: 'interface',
        nodeId,
        interfaceIndex,
        port: position,
        interfaceName: renderedInterfaceName,
        plannedMac: interfaceData.planned_mac ?? null,
      },
      nearTarget: true,
    });
    dragLinkStore.release({
      pointer: { x: event.clientX, y: event.clientY },
      targetReachable: true,
    });
  }

  onDestroy(() => {
    unsubscribeCurrentNode?.();
  });
</script>

<div
  class="absolute z-10"
  style={stylePosition}
  data-port-node-id={nodeId}
  data-port-interface-index={interfaceIndex}
  data-port-side={position.side}
  data-port-offset={position.offset.toFixed(4)}
  data-port-live-state={liveMac?.state ?? ''}
  role="button"
  tabindex="-1"
  aria-label={`Port ${renderedInterfaceName}`}
  on:mousedown={handleMouseDown}
  on:mouseup={handleMouseUp}
  on:mouseenter={handleMouseEnter}
  on:mouseleave={handleMouseLeave}
>
  {#if insideFlow}
    <Handle
      type="source"
      position={xyPosition}
      id={`iface-${interfaceIndex}`}
      class="!h-1 !w-1 !border-0 !bg-transparent !opacity-0 !pointer-events-none"
    />
  {/if}
  <span
    bind:this={portHandle}
    class={`port-handle block h-2.5 w-2.5 rounded-full border border-gray-950 ${
      isHovered ? 'port-handle-hover' : ''
    } ${
      highlighted
        ? 'bg-emerald-300 ring-2 ring-emerald-300/60 shadow-[0_0_8px_rgba(110,231,183,0.55)]'
        : isSource
          ? 'bg-blue-400'
          : 'bg-gray-400 hover:bg-blue-300'
    }`}
    data-testid="port-handle"
  ></span>
  {#if hasMismatch}
    <span
      class="pointer-events-none absolute -right-0.5 -top-0.5 h-1.5 w-1.5 rounded-full bg-amber-300 ring-1 ring-amber-100/80 shadow-[0_0_4px_rgba(252,211,77,0.7)]"
      data-testid="port-mismatch-dot"
      aria-hidden="true"
    ></span>
  {/if}
  {#if showTooltip}
    <PortTooltip
      interfaceName={renderedInterfaceName}
      plannedMac={interfaceData.planned_mac ?? undefined}
      liveMac={liveMac}
      placement={tooltipPlacement}
      anchorRect={anchorRect}
    />
  {/if}
</div>

<style>
  .port-handle {
    transition:
      transform 90ms ease-out,
      background-color 150ms ease-out,
      box-shadow 150ms ease-out;
    transform-origin: center center;
    transform: scale(1);
  }
  .port-handle.port-handle-hover {
    transform: scale(1.5);
  }
</style>
