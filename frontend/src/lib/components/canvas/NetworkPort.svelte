<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { dragLinkStore, getDragLinkSnapshot, type Side } from '$lib/stores/dragLink';
  import { togglePortInfo } from '$lib/stores/portInfo';

  export let networkId: number;
  export let networkName: string | undefined = undefined;
  export let side: Side;
  export let offset = 0.5;

  const CLICK_MAX_MS = 200;
  const CLICK_MAX_DIST_PX = 4;

  const dispatch = createEventDispatcher<{
    'port:click': {
      kind: 'network';
      networkId: number;
      side: Side;
      offset: number;
      anchorRect: DOMRect | null;
    };
  }>();

  let isHovered = false;
  let portHandle: HTMLSpanElement | null = null;
  let clickStartTs = 0;
  let clickStartCoords: { x: number; y: number } | null = null;

  $: stylePosition = (() => {
    const offsetPct = `${(offset * 100).toFixed(2)}%`;
    switch (side) {
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

  function handleMouseDown(event: MouseEvent) {
    if (event.button !== 0) return;

    if (!event.shiftKey) {
      event.stopPropagation();
      event.preventDefault();
      clickStartTs = Date.now();
      clickStartCoords = { x: event.clientX, y: event.clientY };
      dragLinkStore.start({
        source: {
          kind: 'network',
          networkId,
          side,
          offset,
          networkName,
        },
        pointer: { x: event.clientX, y: event.clientY },
      });
    }
  }

  function handleMouseEnter(event: MouseEvent) {
    isHovered = true;
    const snap = getDragLinkSnapshot();
    if (snap.state === 'idle' || snap.state === 'confirming') return;
    if (snap.source && snap.source.kind === 'network' && snap.source.networkId === networkId) {
      return;
    }
    dragLinkStore.move({
      pointer: snap.pointer ?? { x: event.clientX, y: event.clientY },
      target: {
        kind: 'network',
        networkId,
        side,
        offset,
        networkName,
      },
      nearTarget: true,
    });
  }

  function handleMouseLeave() {
    isHovered = false;
    const snap = getDragLinkSnapshot();
    if (snap.state === 'near_target') {
      dragLinkStore.leaveTargetZone();
    }
  }

  function handleMouseUp(event: MouseEvent) {
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
        const rect = portHandle ? portHandle.getBoundingClientRect() : null;
        dispatch('port:click', { kind: 'network', networkId, side, offset, anchorRect: rect });
        togglePortInfo({
          kind: 'network',
          networkId,
          side,
          offset,
          anchorRect: rect,
          networkName: networkName ?? null,
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
    if (snap.source.kind === 'network' && snap.source.networkId === networkId) {
      dragLinkStore.cancel();
      return;
    }
    dragLinkStore.move({
      pointer: { x: event.clientX, y: event.clientY },
      target: {
        kind: 'network',
        networkId,
        side,
        offset,
        networkName,
      },
      nearTarget: true,
    });
    dragLinkStore.release({
      pointer: { x: event.clientX, y: event.clientY },
      targetReachable: true,
    });
  }

  $: snapshot = $dragLinkStore;
  $: highlighted = (() => {
    const target = snapshot.target;
    if (!target || target.kind !== 'network') return false;
    return target.networkId === networkId && target.side === side;
  })();
</script>

<div
  class="absolute z-10"
  style={stylePosition}
  data-network-port-id={networkId}
  data-network-port-side={side}
  data-network-port-offset={offset.toFixed(4)}
  role="button"
  tabindex="-1"
  aria-label={`Network ${networkName ?? networkId} ${side} port`}
  on:mousedown={handleMouseDown}
  on:mouseup={handleMouseUp}
  on:mouseenter={handleMouseEnter}
  on:mouseleave={handleMouseLeave}
>
  <span
    bind:this={portHandle}
    class={`port-handle block h-2.5 w-2.5 rounded-full border border-gray-950 ${
      isHovered ? 'port-handle-hover' : ''
    } ${
      highlighted
        ? 'bg-emerald-300 ring-2 ring-emerald-300/60 shadow-[0_0_8px_rgba(110,231,183,0.55)]'
        : 'bg-blue-400 hover:bg-blue-300'
    }`}
    data-testid="network-port-handle"
  ></span>
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
