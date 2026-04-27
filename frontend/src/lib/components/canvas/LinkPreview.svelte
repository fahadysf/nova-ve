<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  import { dragLinkStore } from '$lib/stores/dragLink';

  /** Optional anchor (in screen coords) for the source port. */
  export let sourceAnchor: { x: number; y: number } | null = null;

  $: snapshot = $dragLinkStore;
  $: visible =
    snapshot.state === 'dragging' ||
    snapshot.state === 'near_target' ||
    snapshot.state === 'port_pressed';
  $: pointer = snapshot.pointer;

  $: pathD = (() => {
    if (!sourceAnchor || !pointer) return '';
    const sx = sourceAnchor.x;
    const sy = sourceAnchor.y;
    const tx = pointer.x;
    const ty = pointer.y;
    // Simple smoothed S-curve; the production router supplies the final shape.
    const dx = (tx - sx) * 0.4;
    return `M ${sx} ${sy} C ${sx + dx} ${sy}, ${tx - dx} ${ty}, ${tx} ${ty}`;
  })();
</script>

{#if visible && pathD}
  <svg
    class="pointer-events-none fixed inset-0 z-40 h-full w-full"
    aria-hidden="true"
    xmlns="http://www.w3.org/2000/svg"
  >
    <path
      d={pathD}
      fill="none"
      stroke={snapshot.state === 'near_target' ? '#34d399' : '#60a5fa'}
      stroke-width="2"
      stroke-dasharray="6 4"
      opacity="0.85"
    />
    {#if snapshot.state === 'near_target' && pointer}
      <circle cx={pointer.x} cy={pointer.y} r="6" fill="none" stroke="#34d399" stroke-width="2" />
    {/if}
  </svg>
{/if}
