<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  import { onMount } from 'svelte';
  import type { LiveMacState } from '$lib/types';

  export let kind: 'interface' | 'network' = 'interface';
  export let interfaceName: string;
  export let plannedMac: string | null = null;
  export let liveMac: LiveMacState | null = null;
  export let peerLabel: string | null = null;
  export let status: 'up' | 'down' | 'unknown' = 'unknown';
  export let speed: string | null = null;
  export let mtu: string | null = null;
  export let duplex: string | null = null;
  export let anchorRect: DOMRect | null = null;
  export let placement: 'top' | 'right' | 'bottom' | 'left' = 'right';
  export let onClose: () => void = () => {};

  const GAP = 8;

  let popoverEl: HTMLElement | null = null;

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

  $: popoverPosition = (() => {
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

  $: statusDotClass = (() => {
    switch (status) {
      case 'up':
        return 'bg-emerald-400';
      case 'down':
        return 'bg-red-400';
      case 'unknown':
      default:
        return 'bg-gray-500';
    }
  })();

  function handleWindowMouseDown(event: MouseEvent) {
    if (!popoverEl) return;
    const target = event.target as Node | null;
    if (target && popoverEl.contains(target)) return;
    onClose();
  }

  function handleWindowKeyDown(event: KeyboardEvent) {
    if (event.key === 'Escape') {
      onClose();
    }
  }

  onMount(() => {
    if (typeof window === 'undefined') return;
    const timer = setTimeout(() => {
      window.addEventListener('mousedown', handleWindowMouseDown);
    }, 0);
    window.addEventListener('keydown', handleWindowKeyDown);
    return () => {
      clearTimeout(timer);
      window.removeEventListener('mousedown', handleWindowMouseDown);
      window.removeEventListener('keydown', handleWindowKeyDown);
    };
  });

  $: liveMacRow = (() => {
    if (!liveMac || liveMac.state === 'unavailable') {
      return { text: '—', className: 'text-gray-500', mismatch: false };
    }
    if (liveMac.state === 'mismatch') {
      return {
        text: `${liveMac.live_mac ?? ''} !`,
        className: 'text-amber-300 font-semibold',
        mismatch: true,
      };
    }
    return {
      text: liveMac.live_mac ?? '—',
      className: 'text-gray-100',
      mismatch: false,
    };
  })();
</script>

<div
  bind:this={popoverEl}
  use:portal
  class="fixed z-[2147483646] min-w-[220px] rounded-lg border border-gray-700 bg-gray-900/95 px-3 py-2 text-[11px] text-gray-100 shadow-xl shadow-black/40 backdrop-blur"
  style:left={`${popoverPosition.left}px`}
  style:top={`${popoverPosition.top}px`}
  style:transform={popoverPosition.transform}
  data-testid="port-info-popover"
  role="dialog"
  aria-label={`Port info for ${interfaceName}`}
>
  <div class="space-y-1.5">
    <div class="font-mono text-[12px] font-semibold text-gray-100" data-testid="popover-interface-name">
      {interfaceName}
    </div>

    {#if kind === 'interface'}
      <div class="flex items-baseline justify-between gap-3" data-testid="popover-planned-mac">
        <span class="text-[10px] uppercase tracking-[0.05em] text-gray-500">Planned</span>
        <span class="font-mono text-gray-300">{plannedMac ?? '—'}</span>
      </div>

      <div class="flex items-baseline justify-between gap-3">
        <span class="text-[10px] uppercase tracking-[0.05em] text-gray-500">Live</span>
        <span class={`font-mono ${liveMacRow.className}`} data-testid="popover-live-mac">
          {liveMacRow.text}
        </span>
      </div>
    {/if}

    <div class="flex items-baseline justify-between gap-3" data-testid="popover-peer">
      <span class="text-[10px] uppercase tracking-[0.05em] text-gray-500">Connected to</span>
      <span class="text-gray-200">{peerLabel ?? '—'}</span>
    </div>

    <div class="flex items-center justify-between gap-3" data-testid="popover-status">
      <span class="text-[10px] uppercase tracking-[0.05em] text-gray-500">Status</span>
      <span class="inline-flex items-center gap-1.5 text-gray-200">
        <span
          class={`inline-block h-2 w-2 rounded-full ${statusDotClass}`}
          data-testid="popover-status-dot"
          aria-hidden="true"
        ></span>
        <span>{status}</span>
      </span>
    </div>

    <div class="flex items-baseline justify-between gap-3" data-testid="popover-speed">
      <span class="text-[10px] uppercase tracking-[0.05em] text-gray-500">Speed</span>
      <span class="text-gray-200">{speed ?? '—'}</span>
    </div>

    <div class="flex items-baseline justify-between gap-3" data-testid="popover-mtu">
      <span class="text-[10px] uppercase tracking-[0.05em] text-gray-500">MTU</span>
      <span class="text-gray-200">{mtu ?? '—'}</span>
    </div>

    <div class="flex items-baseline justify-between gap-3" data-testid="popover-duplex">
      <span class="text-[10px] uppercase tracking-[0.05em] text-gray-500">Duplex</span>
      <span class="text-gray-200">{duplex ?? '—'}</span>
    </div>
  </div>
</div>
