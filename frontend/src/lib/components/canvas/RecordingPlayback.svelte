<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  import { onMount } from 'svelte';
  import type { Player } from 'asciinema-player';

  export let url: string;

  let container: HTMLDivElement;
  let loading = true;
  let error = '';

  onMount(() => {
    const controller = new AbortController();
    let player: Player | undefined;

    async function load() {
      try {
        const response = await fetch(url, { signal: controller.signal, credentials: 'same-origin' });
        if (!response.ok || !response.headers.get('content-type')?.includes('application/x-asciicast')) {
          throw new Error('Could not load the recording for playback.');
        }

        const [{ create }] = await Promise.all([
          import('asciinema-player'),
          import('asciinema-player/dist/bundle/asciinema-player.css')
        ]);
        if (!controller.signal.aborted) {
          player = create({ data: response }, container, { controls: true, fit: 'width', preload: true });
        }
      } catch (err) {
        if (!controller.signal.aborted) {
          error = err instanceof Error ? err.message : 'Could not start playback.';
        }
      } finally {
        if (!controller.signal.aborted) loading = false;
      }
    }

    void load();
    return () => {
      controller.abort();
      player?.dispose();
    };
  });
</script>

{#if loading}
  <p role="status" class="text-xs text-gray-400">Loading playback…</p>
{:else if error}
  <p role="alert" class="text-xs text-red-300">{error}</p>
{/if}
<div class="w-full" bind:this={container}></div>
