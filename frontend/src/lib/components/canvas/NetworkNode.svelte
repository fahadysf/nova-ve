<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  import NetworkPort from './NetworkPort.svelte';
  import { Info, Waypoints } from 'lucide-svelte';
  import { assignNetworkSlots, slotPosition } from '$lib/services/canvasEdges';
  import { infoPanels } from '$lib/stores/infoPanels';

  type NetworkConnectionPointRef = { endpointKey: 'from' | 'to' };

  export let id: string | undefined = undefined;
  export let data: {
    label: string;
    icon?: string;
    count?: number;
    networkId?: number;
    linkIds?: string[];
    portPositionsByLinkId?: Record<string, { side: 'top' | 'right' | 'bottom' | 'left'; offset: number }>;
    portRefsByLinkId?: Record<string, NetworkConnectionPointRef>;
    /** Canvas-computed position for the dotted open slot (#258). */
    openSlotPosition?: { side: 'top' | 'right' | 'bottom' | 'left'; offset: number } | null;
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
      idx === openSlotIdx && data.openSlotPosition
        ? data.openSlotPosition
        : linkIdBySlot.get(idx) && data.portPositionsByLinkId?.[linkIdBySlot.get(idx)!]
          ? data.portPositionsByLinkId[linkIdBySlot.get(idx)!]
          : slotPosition(idx, slotCount),
    isOpen: idx === openSlotIdx,
  }));
  // slotAssignments is consumed by canvasEdges.deriveEdges — referenced here
  // so the reactive dependency on linkIds re-evaluates the helper too.
  $: void slotAssignments;
</script>

<div
  class="group relative min-w-[8rem] rounded-lg border border-[var(--card-canvas-border)] bg-[var(--card-canvas-bg)] transition-colors hover:border-[var(--card-canvas-border-hover)] hover:bg-[var(--card-canvas-bg-hover)]"
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
      endpointKey={slot.linkId ? data.portRefsByLinkId?.[slot.linkId]?.endpointKey : undefined}
      isOpen={slot.isOpen}
    />
  {/each}

  <!-- Frame-header look (#254): quiet band, name in the primary role and the
       connection count as a secondary line — network pills stay neutral
       grays, color is reserved for link state. -->
  <div class="flex items-center justify-between gap-2 py-2.5 pl-3.5 pr-4">
    <div class="flex min-w-0 items-center gap-2">
      <div
        class="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-[var(--card-canvas-icon-bg)] text-[var(--card-canvas-sub-fg)] transition-colors group-hover:bg-[var(--card-canvas-icon-bg-hover)] group-hover:text-[var(--card-canvas-name-fg)]"
      >
        <Waypoints class="h-5 w-5" />
      </div>
      <div class="flex min-w-0 flex-col justify-center leading-tight">
        <div
          class="truncate text-[12px] font-medium text-[var(--card-canvas-name-fg)]"
          title={data.label}
        >
          {data.label}
        </div>
        <div class="mt-0.5 whitespace-nowrap text-[11px] font-normal text-[var(--card-canvas-sub-fg)]">
          {data.count ?? 0} connection{(data.count ?? 0) === 1 ? '' : 's'}
        </div>
      </div>
    </div>
  </div>

  <button
    type="button"
    class="nodrag absolute bottom-1 right-1 inline-flex h-3 w-3 items-center justify-center rounded-full border border-[var(--card-canvas-border)] bg-[var(--card-canvas-bg-hover)] text-[var(--card-canvas-sub-fg)] transition-colors hover:border-[var(--card-canvas-border-hover)] hover:text-[var(--card-canvas-name-fg)]"
    aria-label="Open network info panel"
    title="Open info panel"
    data-testid="network-info-button"
    on:click={openInfo}
    on:pointerdown|stopPropagation
  >
    <Info class="h-2 w-2" />
  </button>
</div>
