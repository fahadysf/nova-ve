<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import type { LiveMacState, NodeInterface, PortPosition } from '$lib/types';
  import PortTooltip from './PortTooltip.svelte';

  export let interfaceData: NodeInterface;
  export let position: PortPosition;
  export let nodeId: number;
  export let interfaceIndex = interfaceData.index ?? 0;
  export let isSource = false;
  export let highlighted = false;
  export let liveMac: LiveMacState | undefined = undefined;

  const dispatch = createEventDispatcher<{
    'port:mousedown': { event: MouseEvent; nodeId: number; interfaceIndex: number; port: PortPosition };
    'port:mouseup': { event: MouseEvent; nodeId: number; interfaceIndex: number; port: PortPosition };
    'port:mouseenter': { event: MouseEvent; nodeId: number; interfaceIndex: number; port: PortPosition };
    'port:mouseleave': { event: MouseEvent; nodeId: number; interfaceIndex: number; port: PortPosition };
    'port:dragstart': { event: MouseEvent; nodeId: number; interfaceIndex: number };
  }>();

  let hoverTimer: ReturnType<typeof setTimeout> | null = null;
  let showTooltip = false;
  let portHandle: HTMLSpanElement | null = null;
  let anchorRect: DOMRect | null = null;

  // SvelteFlow positions ports on the perimeter of a (relatively positioned)
  // wrapper. We use percentage offsets that map directly to the side.
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

  // Map the docked side of the port to the placement of its tooltip.
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
    hoverTimer = setTimeout(() => {
      captureAnchorRect();
      showTooltip = true;
    }, 250);
    dispatch('port:mouseenter', { event, nodeId, interfaceIndex, port: position });
  }

  function handleMouseLeave(event: MouseEvent) {
    clearHoverTimer();
    showTooltip = false;
    dispatch('port:mouseleave', { event, nodeId, interfaceIndex, port: position });
  }

  function handleMouseDown(event: MouseEvent) {
    if (event.button !== 0) return;
    clearHoverTimer();
    showTooltip = false;
    dispatch('port:mousedown', { event, nodeId, interfaceIndex, port: position });
  }

  function handleMouseUp(event: MouseEvent) {
    dispatch('port:mouseup', { event, nodeId, interfaceIndex, port: position });
  }
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
  aria-label={`Port ${interfaceData.name}`}
  on:mousedown={handleMouseDown}
  on:mouseup={handleMouseUp}
  on:mouseenter={handleMouseEnter}
  on:mouseleave={handleMouseLeave}
>
  <span
    bind:this={portHandle}
    class={`block h-2.5 w-2.5 rounded-full border border-gray-950 transition ${
      highlighted
        ? 'bg-emerald-300 ring-2 ring-emerald-300/60 shadow-[0_0_8px_rgba(110,231,183,0.55)]'
        : isSource
          ? 'bg-blue-400'
          : 'bg-gray-400 hover:bg-blue-300'
    }`}
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
      interfaceName={interfaceData.name}
      plannedMac={interfaceData.planned_mac ?? undefined}
      liveMac={liveMac}
      placement={tooltipPlacement}
      anchorRect={anchorRect}
    />
  {/if}
</div>
