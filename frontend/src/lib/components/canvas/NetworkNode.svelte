<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  import NetworkPort from './NetworkPort.svelte';
  import { assignNetworkSlots, slotPosition } from '$lib/services/canvasEdges';

  export let id: string | undefined = undefined;
  export let data: {
    label: string;
    icon?: string;
    count?: number;
    networkId?: number;
    linkIds?: string[];
  };

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
  $: slotCount = linkIds.length + 1;
  $: openSlotIdx = linkIds.length;
  $: slots = Array.from({ length: slotCount }, (_, idx) => ({
    idx,
    placement: slotPosition(idx, slotCount),
    isOpen: idx === openSlotIdx,
  }));
  // slotAssignments is consumed by canvasEdges.deriveEdges — referenced here
  // so the reactive dependency on linkIds re-evaluates the helper too.
  $: void slotAssignments;
</script>

<div
  class="relative min-w-[7.5rem] rounded-full border border-blue-500 bg-blue-500/15 px-3 py-1.5 text-blue-100 shadow-lg shadow-black/20"
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
      isOpen={slot.isOpen}
    />
  {/each}

  <div class="flex items-center justify-between gap-2">
    <div class="min-w-0">
      <div class="text-[8px] uppercase tracking-[0.18em] text-blue-200/80">Network</div>
      <div class="truncate text-[10px] font-semibold tracking-tight">{data.label}</div>
    </div>
    <span class="rounded-full bg-blue-950/80 px-1.5 py-0.5 font-mono text-[9px] text-blue-100">
      {data.count ?? 0}
    </span>
  </div>
</div>
