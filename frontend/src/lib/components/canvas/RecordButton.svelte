<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<!-- On-demand console recording toggle (#291): hollow red circle when
     idle, pulsating solid red circle while recording. -->
<script lang="ts">
  export let recording = false;
  export let disabled = false;
  export let onToggle: () => void;
</script>

<button
  type="button"
  class={`inline-flex items-center justify-center rounded-md border px-1.5 py-[3px] transition ${
    recording
      ? 'border-red-500/60 bg-red-500/10'
      : 'border-gray-700 bg-gray-950 hover:border-red-500/60'
  }`}
  aria-pressed={recording}
  aria-label={recording ? 'Stop recording' : 'Start recording'}
  title={recording
    ? 'Stop recording — the .cast and raw log are saved with start/stop timestamps'
    : 'Record this console session (asciinema .cast + raw log)'}
  data-testid="record-toggle"
  {disabled}
  on:click={onToggle}
  on:pointerdown={(event) => event.stopPropagation()}
>
  <span
    class={`block h-2.5 w-2.5 rounded-full ${
      recording ? 'animate-pulse bg-red-500' : 'border-[1.5px] border-red-500'
    }`}
  ></span>
</button>
