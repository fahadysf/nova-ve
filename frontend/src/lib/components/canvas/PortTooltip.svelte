<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  import type { LiveMacState } from '$lib/types';

  export let interfaceName: string;
  export let liveMac: LiveMacState | undefined = undefined;
  export let placement: 'top' | 'right' | 'bottom' | 'left' = 'right';
  export let anchorRect: DOMRect | null = null;
  export let plannedMac: string | undefined = undefined;

  const GAP = 6;

  // Portal action: relocate the rendered node to document.body so the tooltip
  // escapes the canvas/port containers without forcing a layout shift.
  function portal(node: HTMLElement) {
    if (typeof document !== 'undefined') {
      document.body.appendChild(node);
    }
    return {
      destroy() {
        if (node.parentNode) {
          node.parentNode.removeChild(node);
        }
      },
    };
  }

  $: tooltipPosition = (() => {
    if (!anchorRect) {
      return { left: 0, top: 0, transform: 'translate(0, 0)' };
    }
    switch (placement) {
      case 'top':
        return {
          left: anchorRect.left + anchorRect.width / 2,
          top: anchorRect.top - GAP,
          transform: 'translate(-50%, -100%)',
        };
      case 'bottom':
        return {
          left: anchorRect.left + anchorRect.width / 2,
          top: anchorRect.bottom + GAP,
          transform: 'translate(-50%, 0)',
        };
      case 'left':
        return {
          left: anchorRect.left - GAP,
          top: anchorRect.top + anchorRect.height / 2,
          transform: 'translate(-100%, -50%)',
        };
      case 'right':
      default:
        return {
          left: anchorRect.right + GAP,
          top: anchorRect.top + anchorRect.height / 2,
          transform: 'translate(0, -50%)',
        };
    }
  })();

  $: resolvedPlanned = liveMac?.planned_mac ?? plannedMac ?? '';
</script>

<div
  use:portal
  class="pointer-events-none fixed z-[2147483646] whitespace-nowrap rounded-md border border-gray-700 bg-gray-900/95 px-2 py-1 text-[10px] font-mono text-gray-100 shadow-lg shadow-black/30 backdrop-blur"
  style:left={`${tooltipPosition.left}px`}
  style:top={`${tooltipPosition.top}px`}
  style:transform={tooltipPosition.transform}
  data-testid="port-tooltip"
>
  <div class="font-mono">{interfaceName}</div>
  {#if resolvedPlanned}
    <div class="text-gray-400">Planned: {resolvedPlanned}</div>
  {/if}
  {#if liveMac}
    {#if liveMac.state === 'mismatch'}
      <div class="text-amber-300 font-semibold" data-testid="live-mac-row">
        Live: {liveMac.live_mac ?? ''} !
      </div>
    {:else if liveMac.state === 'confirmed'}
      <div class="text-gray-100" data-testid="live-mac-row">
        Live: {liveMac.live_mac ?? ''}
      </div>
    {:else}
      <div class="text-gray-500" data-testid="live-mac-row">
        Live: unavailable{liveMac.reason ? ` — ${liveMac.reason}` : ''}
      </div>
    {/if}
  {/if}
</div>
