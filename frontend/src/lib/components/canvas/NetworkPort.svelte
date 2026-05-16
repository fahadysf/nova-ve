<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  import { createEventDispatcher, getContext } from 'svelte';
  import { Handle, Position } from '@xyflow/svelte';
  import { dragLinkStore, getDragLinkSnapshot, type Side } from '$lib/stores/dragLink';
  import { togglePortInfo } from '$lib/stores/portInfo';
  import { slotHandleId } from '$lib/services/canvasEdges';
  import { clampToPerimeter } from '$lib/services/portLayout';
  import type { PortPosition } from '$lib/types';

  export let networkId: number;
  export let networkName: string | undefined = undefined;
  export let side: Side;
  export let offset = 0.5;
  export let slotIndex: number;
  export let linkId: string | null = null;
  export let isOpen = false;

  const CLICK_MAX_MS = 200;
  const CLICK_MAX_DIST_PX = 4;

  const dispatch = createEventDispatcher<{
    'port:click': {
      kind: 'network';
      networkId: number;
      side: Side;
      offset: number;
      slotIndex: number;
      anchorRect: DOMRect | null;
    };
  }>();

  let isHovered = false;
  let portHandle: HTMLSpanElement | null = null;
  let clickStartTs = 0;
  let clickStartCoords: { x: number; y: number } | null = null;
  let draggingPosition: PortPosition | null = null;

  type PersistConnectionPointPosition = (
    target: { kind: 'network'; networkId: number; linkId: string },
    port: PortPosition
  ) => void;
  type ConnectionPointContextMenu = (
    target: { kind: 'network'; networkId: number; linkId: string },
    event: MouseEvent
  ) => void;
  const persistConnectionPointPosition = getContext<PersistConnectionPointPosition | undefined>(
    'nova-ve:connection-point-position-persist'
  );
  const openConnectionPointMenu = getContext<ConnectionPointContextMenu | undefined>(
    'nova-ve:connection-point-context-menu'
  );

  $: handleId = slotHandleId(networkId, slotIndex);

  $: effectivePosition = draggingPosition ?? ({ side, offset } as PortPosition);
  $: effectiveSide = effectivePosition.side;
  $: effectiveOffset = effectivePosition.offset;

  $: xyPosition = (() => {
    switch (effectiveSide) {
      case 'top':
        return Position.Top;
      case 'right':
        return Position.Right;
      case 'bottom':
        return Position.Bottom;
      case 'left':
      default:
        return Position.Left;
    }
  })();

  $: stylePosition = (() => {
    const offsetPct = `${(effectiveOffset * 100).toFixed(2)}%`;
    switch (effectiveSide) {
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

    if (event.shiftKey && !isOpen && linkId) {
      const root = portHandle?.closest('[data-testid="network-node"]');
      const rect = root?.getBoundingClientRect();
      if (!rect) return;
      event.stopPropagation();
      event.preventDefault();

      const onMove = (moveEvent: MouseEvent) => {
        draggingPosition = clampToPerimeter(
          { x: moveEvent.clientX, y: moveEvent.clientY },
          { x: rect.left, y: rect.top, w: rect.width, h: rect.height }
        );
      };
      const onUp = () => {
        window.removeEventListener('mousemove', onMove);
        window.removeEventListener('mouseup', onUp);
        if (draggingPosition) {
          persistConnectionPointPosition?.(
            { kind: 'network', networkId, linkId },
            draggingPosition
          );
        }
        draggingPosition = null;
      };
      window.addEventListener('mousemove', onMove);
      window.addEventListener('mouseup', onUp);
      return;
    }

    if (!event.shiftKey) {
      event.stopPropagation();
      event.preventDefault();
      clickStartTs = Date.now();
      clickStartCoords = { x: event.clientX, y: event.clientY };
      dragLinkStore.start({
        source: {
          kind: 'network',
          networkId,
          side: effectiveSide,
          offset: effectiveOffset,
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
        side: effectiveSide,
        offset: effectiveOffset,
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
        dispatch('port:click', { kind: 'network', networkId, side: effectiveSide, offset: effectiveOffset, slotIndex, anchorRect: rect });
        togglePortInfo({
          kind: 'network',
          networkId,
          side: effectiveSide,
          offset: effectiveOffset,
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
        side: effectiveSide,
        offset: effectiveOffset,
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
    return target.networkId === networkId && target.side === effectiveSide;
  })();

  function handleContextMenu(event: MouseEvent) {
    if (isOpen || !linkId) return;
    event.preventDefault();
    event.stopPropagation();
    openConnectionPointMenu?.({ kind: 'network', networkId, linkId }, event);
  }
</script>

<div
  class="absolute z-10"
  style={stylePosition}
  data-network-port-id={networkId}
  data-network-port-side={effectiveSide}
  data-network-port-offset={effectiveOffset.toFixed(4)}
  data-network-port-slot={slotIndex}
  data-network-port-open={isOpen ? 'true' : 'false'}
  role="button"
  tabindex="-1"
  aria-label={`Network ${networkName ?? networkId} slot ${slotIndex}${isOpen ? ' (open)' : ''}`}
  on:mousedown={handleMouseDown}
  on:mouseup={handleMouseUp}
  on:contextmenu={handleContextMenu}
  on:mouseenter={handleMouseEnter}
  on:mouseleave={handleMouseLeave}
>
  <Handle
    type="source"
    position={xyPosition}
    id={handleId}
    class="!h-1 !w-1 !border-0 !bg-transparent !opacity-0 !pointer-events-none"
  />
  <Handle
    type="target"
    position={xyPosition}
    id={handleId}
    class="!h-1 !w-1 !border-0 !bg-transparent !opacity-0 !pointer-events-none"
  />
  <span
    bind:this={portHandle}
    class={`port-handle block h-2.5 w-2.5 rounded-full border ${
      isOpen ? 'border-dashed border-blue-300/80' : 'border-gray-950'
    } ${isHovered ? 'port-handle-hover' : ''} ${
      highlighted
        ? 'bg-emerald-300 ring-2 ring-emerald-300/60 shadow-[0_0_8px_rgba(110,231,183,0.55)]'
        : isOpen
          ? 'bg-blue-950/60 hover:bg-blue-700/70'
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
