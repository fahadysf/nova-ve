<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  import { onDestroy } from 'svelte';
  import { X } from 'lucide-svelte';

  export let id: string;
  export let title: string;
  export let subtitle: string = '';
  export let position: { x: number; y: number };
  export let zIndex: number = 100;
  export let onClose: (id: string) => void;
  export let onMove: (id: string, x: number, y: number) => void;
  export let onFocus: (id: string) => void;

  // Drag state — pointer-down on the header captures pointer events so the
  // drag survives crossing element boundaries. Clamping keeps at least 48px
  // of the header inside the viewport so a panel cannot be dragged fully
  // off-screen.
  let dragging = false;
  let pointerId = -1;
  let originX = 0;
  let originY = 0;
  let originPanelX = 0;
  let originPanelY = 0;
  let headerEl: HTMLDivElement | null = null;
  const MIN_VISIBLE = 48;

  function onPointerDown(event: PointerEvent) {
    if (event.button !== 0) return;
    onFocus(id);
    if (event.target instanceof Element && event.target.closest('[data-info-panel-close]')) {
      return;
    }
    dragging = true;
    pointerId = event.pointerId;
    originX = event.clientX;
    originY = event.clientY;
    originPanelX = position.x;
    originPanelY = position.y;
    headerEl?.setPointerCapture(pointerId);
    event.preventDefault();
  }

  function onPointerMove(event: PointerEvent) {
    if (!dragging || event.pointerId !== pointerId) return;
    const dx = event.clientX - originX;
    const dy = event.clientY - originY;
    const next = clampToViewport(originPanelX + dx, originPanelY + dy);
    onMove(id, next.x, next.y);
  }

  function endDrag(event: PointerEvent) {
    if (event.pointerId !== pointerId) return;
    dragging = false;
    try {
      headerEl?.releasePointerCapture(pointerId);
    } catch {
      /* already released */
    }
    pointerId = -1;
  }

  function clampToViewport(x: number, y: number): { x: number; y: number } {
    if (typeof window === 'undefined') return { x, y };
    const maxX = window.innerWidth - MIN_VISIBLE;
    const maxY = window.innerHeight - MIN_VISIBLE;
    return {
      x: Math.max(-1 * (320 - MIN_VISIBLE), Math.min(x, maxX)),
      y: Math.max(0, Math.min(y, maxY)),
    };
  }

  onDestroy(() => {
    if (dragging && headerEl && pointerId >= 0) {
      try {
        headerEl.releasePointerCapture(pointerId);
      } catch {
        /* swallow */
      }
    }
  });
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
  class="pointer-events-auto absolute w-[18rem] overflow-hidden rounded-xl border border-gray-700 bg-gray-950/95 text-gray-100 shadow-xl shadow-black/40 backdrop-blur"
  style="left: {position.x}px; top: {position.y}px; z-index: {zIndex};"
  role="dialog"
  aria-label={title}
  tabindex="-1"
  data-testid="info-panel"
  data-info-panel-id={id}
  on:pointerdown={() => onFocus(id)}
>
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div
    bind:this={headerEl}
    role="presentation"
    class="flex cursor-grab items-center justify-between gap-2 border-b border-gray-800 bg-gray-900/80 px-3 py-2 active:cursor-grabbing"
    on:pointerdown={onPointerDown}
    on:pointermove={onPointerMove}
    on:pointerup={endDrag}
    on:pointercancel={endDrag}
  >
    <div class="min-w-0">
      <div class="text-[9px] uppercase tracking-[0.18em] text-gray-500">{subtitle || 'Info'}</div>
      <div class="truncate text-[12px] font-semibold tracking-tight text-gray-100">{title}</div>
    </div>
    <button
      type="button"
      data-info-panel-close
      class="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-full border border-gray-800 bg-gray-900 text-gray-400 transition hover:border-blue-500/40 hover:text-white"
      aria-label="Close info panel"
      on:click={() => onClose(id)}
    >
      <X class="h-3 w-3" />
    </button>
  </div>

  <div class="max-h-[calc(100vh-12rem)] overflow-y-auto px-3 py-2 text-[11px] text-gray-200">
    <slot />
  </div>
</div>
