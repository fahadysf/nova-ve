<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  import { createEventDispatcher, getContext, onDestroy } from 'svelte';
  import { clampToPerimeter, placeInterfaces } from '$lib/services/portLayout';
  import type { NodeInterface, PortPosition } from '$lib/types';
  import Port from './Port.svelte';

  export let nodeId: number;
  export let interfaces: NodeInterface[] = [];
  export let highlightedInterfaceIndex: number | null = null;

  type PortPositionPersist = (
    nodeId: number,
    interfaceIndex: number,
    port: PortPosition
  ) => void;
  const persistFromContext = getContext<PortPositionPersist | undefined>(
    'nova-ve:port-position-persist'
  );

  const dispatch = createEventDispatcher<{
    'port:mousedown': { nodeId: number; interfaceIndex: number; port: PortPosition; event: MouseEvent };
    'port:mouseup': { nodeId: number; interfaceIndex: number; port: PortPosition; event: MouseEvent };
    'port:mouseenter': { nodeId: number; interfaceIndex: number; port: PortPosition; event: MouseEvent };
    'port:mouseleave': { nodeId: number; interfaceIndex: number; port: PortPosition; event: MouseEvent };
    'port:dragend': { nodeId: number; interfaceIndex: number; port: PortPosition };
  }>();

  let layerEl: HTMLDivElement | null = null;
  let dragging: { interfaceIndex: number; previewPort: PortPosition } | null = null;
  let pendingPushTimers: Map<number, ReturnType<typeof setTimeout>> = new Map();

  const PUSH_DEBOUNCE_MS = 250;

  $: defaults = placeInterfaces(interfaces.length);
  $: resolvedPorts = interfaces.map((iface, index) => {
    const persisted = iface.port_position ?? null;
    return persisted ?? defaults[index] ?? ({ side: 'top', offset: 0 } as PortPosition);
  });

  function nodeBox(): { x: number; y: number; w: number; h: number } | null {
    if (!layerEl) return null;
    const rect = layerEl.getBoundingClientRect();
    return { x: rect.left, y: rect.top, w: rect.width, h: rect.height };
  }

  function schedulePersistence(interfaceIndex: number, port: PortPosition) {
    const existing = pendingPushTimers.get(interfaceIndex);
    if (existing) clearTimeout(existing);
    const timer = setTimeout(() => {
      pendingPushTimers.delete(interfaceIndex);
      if (persistFromContext) {
        persistFromContext(nodeId, interfaceIndex, port);
      }
      dispatch('port:dragend', { nodeId, interfaceIndex, port });
    }, PUSH_DEBOUNCE_MS);
    pendingPushTimers.set(interfaceIndex, timer);
  }

  function handlePortMouseDown(detail: {
    event: MouseEvent;
    nodeId: number;
    interfaceIndex: number;
    port: PortPosition;
  }) {
    dispatch('port:mousedown', detail);

    if (!detail.event.shiftKey) {
      return;
    }

    dragging = { interfaceIndex: detail.interfaceIndex, previewPort: detail.port };

    const onMove = (event: MouseEvent) => {
      const box = nodeBox();
      if (!box || !dragging) return;
      const port = clampToPerimeter({ x: event.clientX, y: event.clientY }, box);
      dragging = { interfaceIndex: dragging.interfaceIndex, previewPort: port };
    };

    const onUp = () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
      if (dragging) {
        const { interfaceIndex, previewPort } = dragging;
        schedulePersistence(interfaceIndex, previewPort);
      }
      dragging = null;
    };

    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  }

  function effectivePort(index: number): PortPosition {
    if (dragging && dragging.interfaceIndex === index) return dragging.previewPort;
    return resolvedPorts[index];
  }

  onDestroy(() => {
    for (const timer of pendingPushTimers.values()) clearTimeout(timer);
    pendingPushTimers.clear();
  });
</script>

<div bind:this={layerEl} class="pointer-events-none absolute inset-0">
  {#each interfaces as iface, index (iface.index ?? index)}
    {@const port = effectivePort(index)}
    {@const interfaceIndex = iface.index ?? index}
    <div class="pointer-events-auto absolute inset-0">
      <Port
        interfaceData={iface}
        position={port}
        {nodeId}
        {interfaceIndex}
        highlighted={highlightedInterfaceIndex === interfaceIndex}
        on:port:mousedown={(e) => handlePortMouseDown(e.detail)}
        on:port:mouseup={(e) => dispatch('port:mouseup', e.detail)}
        on:port:mouseenter={(e) => dispatch('port:mouseenter', e.detail)}
        on:port:mouseleave={(e) => dispatch('port:mouseleave', e.detail)}
      />
    </div>
  {/each}
</div>
