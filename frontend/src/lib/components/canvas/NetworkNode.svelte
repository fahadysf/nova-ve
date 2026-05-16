<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  import NetworkPort from './NetworkPort.svelte';
  import { Info } from 'lucide-svelte';
  import { assignNetworkSlots, slotPosition } from '$lib/services/canvasEdges';
  import { infoPanels } from '$lib/stores/infoPanels';

  export let id: string | undefined = undefined;
  export let data: {
    label: string;
    icon?: string;
    count?: number;
    networkId?: number;
    linkIds?: string[];
    portPositionsByLinkId?: Record<string, { side: 'top' | 'right' | 'bottom' | 'left'; offset: number }>;
  };

  function openInfo(event: MouseEvent) {
    event.stopPropagation();
    if (typeof networkId === 'number') {
      infoPanels.open('network', networkId);
    }
  }

  $: networkId = (() => {
    if (typeof data.networkId === 'number') return data.networkId;
    if (typeof id === 'string' && id.startsWith('network')) {
      const n = Number(id.slice('network'.length));
      return Number.isFinite(n) ? n : 0;
    }
    return 0;
  })();

  // Dynamic slot rendering: one slot per declared link plus one always-open
  // slot for new connections. Slot assignment is purely a frontend rendering
  // concern — the backend stores no slot info on links.
  $: linkIds = data.linkIds ?? [];
  $: slotAssignments = assignNetworkSlots(linkIds);
  $: linkIdBySlot = new Map(Array.from(slotAssignments.entries()).map(([linkId, idx]) => [idx, linkId]));
  $: slotCount = linkIds.length + 1;
  $: openSlotIdx = linkIds.length;
  $: slots = Array.from({ length: slotCount }, (_, idx) => ({
    idx,
    linkId: linkIdBySlot.get(idx) ?? null,
    placement:
      linkIdBySlot.get(idx) && data.portPositionsByLinkId?.[linkIdBySlot.get(idx)!]
        ? data.portPositionsByLinkId[linkIdBySlot.get(idx)!]
        : slotPosition(idx, slotCount),
    isOpen: idx === openSlotIdx,
  }));
  // slotAssignments is consumed by canvasEdges.deriveEdges — referenced here
  // so the reactive dependency on linkIds re-evaluates the helper too.
  $: void slotAssignments;
</script>

<div
  class="relative min-w-[7.5rem] rounded-xl border border-blue-500 bg-blue-500/15 px-3 py-2 text-blue-100 shadow-lg shadow-black/20"
  data-testid="network-node"
  data-network-id={networkId}
  data-network-slot-count={slotCount}
>
  {#each slots as slot (slot.idx)}
    <NetworkPort
      {networkId}
      networkName={data.label}
      side={slot.placement.side}
      offset={slot.placement.offset}
      slotIndex={slot.idx}
      linkId={slot.linkId}
      isOpen={slot.isOpen}
    />
  {/each}

  <div class="flex items-start justify-between gap-2">
    <div class="min-w-0 flex-1 pb-3 pl-4 pr-1">
      <div class="break-words text-[10px] font-semibold leading-snug text-blue-100" title={data.label}>
        {data.label}
      </div>
    </div>
    <div class="flex shrink-0 items-center gap-1">
      <span class="rounded-full bg-blue-950/80 px-1.5 py-0.5 font-mono text-[9px] text-blue-100">
        {data.count ?? 0}
      </span>
    </div>
  </div>

  <button
    type="button"
    class="nodrag absolute bottom-1 left-1 inline-flex h-4 w-4 items-center justify-center rounded-full border border-blue-500/40 bg-blue-950/80 text-blue-200 transition hover:border-blue-300/70 hover:text-white"
    aria-label="Open network info panel"
    title="Open info panel"
    data-testid="network-info-button"
    on:click={openInfo}
    on:pointerdown|stopPropagation
  >
    <Info class="h-2.5 w-2.5" />
  </button>
</div>
