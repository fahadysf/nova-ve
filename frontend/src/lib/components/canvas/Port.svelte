<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { fade } from 'svelte/transition';
  import type { NodeInterface, PortPosition } from '$lib/types';

  export let interfaceData: NodeInterface;
  export let position: PortPosition;
  export let nodeId: number;
  export let interfaceIndex = interfaceData.index ?? 0;
  export let isSource = false;
  export let highlighted = false;

  const dispatch = createEventDispatcher<{
    'port:mousedown': { event: MouseEvent; nodeId: number; interfaceIndex: number; port: PortPosition };
    'port:mouseup': { event: MouseEvent; nodeId: number; interfaceIndex: number; port: PortPosition };
    'port:mouseenter': { event: MouseEvent; nodeId: number; interfaceIndex: number; port: PortPosition };
    'port:mouseleave': { event: MouseEvent; nodeId: number; interfaceIndex: number; port: PortPosition };
    'port:dragstart': { event: MouseEvent; nodeId: number; interfaceIndex: number };
  }>();

  let hoverTimer: ReturnType<typeof setTimeout> | null = null;
  let showTooltip = false;

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

  $: tooltipPlacement = (() => {
    switch (position.side) {
      case 'top':
        return 'bottom: calc(100% + 6px); left: 50%; transform: translateX(-50%);';
      case 'bottom':
        return 'top: calc(100% + 6px); left: 50%; transform: translateX(-50%);';
      case 'right':
        return 'left: calc(100% + 6px); top: 50%; transform: translateY(-50%);';
      case 'left':
      default:
        return 'right: calc(100% + 6px); top: 50%; transform: translateY(-50%);';
    }
  })();

  function clearHoverTimer() {
    if (hoverTimer !== null) {
      clearTimeout(hoverTimer);
      hoverTimer = null;
    }
  }

  function handleMouseEnter(event: MouseEvent) {
    clearHoverTimer();
    hoverTimer = setTimeout(() => {
      showTooltip = true;
    }, 100);
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
  role="button"
  tabindex="-1"
  aria-label={`Port ${interfaceData.name}`}
  on:mousedown={handleMouseDown}
  on:mouseup={handleMouseUp}
  on:mouseenter={handleMouseEnter}
  on:mouseleave={handleMouseLeave}
>
  <span
    class={`block h-2.5 w-2.5 rounded-full border border-gray-950 transition ${
      highlighted
        ? 'bg-emerald-300 ring-2 ring-emerald-300/60 shadow-[0_0_8px_rgba(110,231,183,0.55)]'
        : isSource
          ? 'bg-blue-400'
          : 'bg-gray-400 hover:bg-blue-300'
    }`}
  ></span>
  {#if showTooltip}
    <div
      class="pointer-events-none absolute z-20 whitespace-nowrap rounded-md border border-gray-700 bg-gray-900/95 px-2 py-1 text-[10px] font-mono text-gray-100 shadow-lg shadow-black/30 backdrop-blur"
      style={tooltipPlacement}
      transition:fade={{ duration: 80 }}
    >
      <div>{interfaceData.name}</div>
      {#if interfaceData.planned_mac}
        <div class="text-gray-400">{interfaceData.planned_mac}</div>
      {/if}
    </div>
  {/if}
</div>
