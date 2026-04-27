<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  import { onDestroy } from 'svelte';
  import {
    defaultScheduler,
    type Obstacle,
    type PortRef,
    type RouteResult,
    type Style,
  } from '$lib/services/router';
  import type { LinkStyle } from '$lib/types';

  // SvelteFlow edge props (subset we use).
  export let id: string;
  export let sourceX: number;
  export let sourceY: number;
  export let targetX: number;
  export let targetY: number;
  export let sourcePosition: 'top' | 'right' | 'bottom' | 'left' = 'right';
  export let targetPosition: 'top' | 'right' | 'bottom' | 'left' = 'left';
  export let style: string | undefined = undefined;
  export let markerEnd: string | undefined = undefined;
  export let markerStart: string | undefined = undefined;
  export let label: string | undefined = undefined;
  export let labelStyle: string | undefined = undefined;
  /** Carrier for v2 link metadata. May contain `style_override` and `obstacles`. */
  export let data: {
    style_override?: LinkStyle | null;
    default_style?: LinkStyle;
    obstacles?: Obstacle[];
  } | undefined = undefined;

  let result: RouteResult | null = null;
  let lastKey = '';

  function resolveStyle(): Style {
    const override = data?.style_override ?? null;
    if (override) return override;
    return (data?.default_style ?? 'orthogonal') as Style;
  }

  $: from = {
    x: sourceX,
    y: sourceY,
    side: sourcePosition,
  } satisfies PortRef;
  $: to = {
    x: targetX,
    y: targetY,
    side: targetPosition,
  } satisfies PortRef;
  $: obstacles = data?.obstacles ?? [];
  $: style_resolved = resolveStyle();

  // Recompute when any input changes. We dedupe with a stable key so toggling
  // unrelated props doesn't kick off a new abortable compute.
  $: {
    const key = JSON.stringify({
      f: from,
      t: to,
      s: style_resolved,
      o: obstacles,
    });
    if (key !== lastKey) {
      lastKey = key;
      void scheduleRoute();
    }
  }

  async function scheduleRoute() {
    try {
      const r = await defaultScheduler.route(id, {
        from,
        to,
        obstacles,
        style: style_resolved,
      });
      result = r;
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') {
        return;
      }
      // Fall back to a straight line; never throw out of the edge component.
      result = {
        d: `M ${from.x},${from.y} L ${to.x},${to.y}`,
        bends: 0,
        lengthPx: Math.hypot(to.x - from.x, to.y - from.y),
      };
    }
  }

  onDestroy(() => {
    defaultScheduler.cancel(id);
  });

  $: dAttr = result?.d ?? `M ${from.x},${from.y} L ${to.x},${to.y}`;
  $: midX = (from.x + to.x) / 2;
  $: midY = (from.y + to.y) / 2;
</script>

<path
  {id}
  class="svelte-flow__edge-path"
  d={dAttr}
  style={style ?? ''}
  marker-start={markerStart}
  marker-end={markerEnd}
  fill="none"
/>

{#if label}
  <text
    x={midX}
    y={midY}
    text-anchor="middle"
    dominant-baseline="middle"
    style={labelStyle ?? ''}
    class="svelte-flow__edge-label"
  >
    {label}
  </text>
{/if}
