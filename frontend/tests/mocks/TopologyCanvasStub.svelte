<!--
  Copyright (c) 2026 Fahad Yousuf <fahadysuf@gmail.com>
  SPDX-License-Identifier: Apache-2.0

  Test double for TopologyCanvas in lab-editor page tests: renders a button
  that fires the canvas' `console` event with a node the test planted on
  globalThis, plus the instance surface the page touches.
-->
<script lang="ts">
  import { createEventDispatcher } from 'svelte';

  export let labId: string = '';
  export let zenMode = false;
  export let consoleSelectorWindows: unknown = undefined;
  export let consoleMinimizedWindows: unknown = undefined;

  const dispatch = createEventDispatcher();

  function fireConsole() {
    const holder = globalThis as unknown as { __externalConsoleTestNode?: unknown };
    dispatch('console', { node: holder.__externalConsoleTestNode });
  }

  export function pendingLayoutIds(): { nodeIds: Set<number>; networkIds: Set<number> } {
    return { nodeIds: new Set(), networkIds: new Set() };
  }
</script>

<div data-testid="topology-canvas-stub" data-zen-mode={zenMode}>
  <button type="button" data-testid="canvas-console-button" on:click={fireConsole}>
    Fire console
  </button>
</div>
