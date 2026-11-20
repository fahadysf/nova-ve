<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  import TopologyCanvas from '$lib/components/canvas/TopologyCanvas.svelte';

  export let labId = 'lab-second-console';
  export let consoleSelectorWindows: Array<{
    id: number;
    nodeName: string;
    isFront: boolean;
    isMinimized: boolean;
  }> = [];
  export let consoleMinimizedWindows: Array<{ id: number; nodeName: string }> = [];

  let consoleEvents = '';

  function handleConsole(event: CustomEvent<{ nodeId: number; mode: 'primary' | 'second' }>) {
    consoleEvents = consoleEvents
      ? `${consoleEvents}\n${event.detail.nodeId}:${event.detail.mode}`
      : `${event.detail.nodeId}:${event.detail.mode}`;
  }
</script>

<TopologyCanvas {labId} {consoleSelectorWindows} {consoleMinimizedWindows} on:console={handleConsole} />
<output data-testid="console-events">{consoleEvents}</output>
