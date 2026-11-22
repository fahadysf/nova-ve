<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<!-- Session recordings browser + viewer (#291). Per-lab list of recorded
     telnet console sessions with an ANSI-stripped log viewer supporting
     substring and regex find, plus .cast / raw .log downloads. -->
<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { ChevronDown, ChevronUp, Download, RefreshCw, X } from 'lucide-svelte';
  import { apiGetData } from '$lib/api';
  import {
    buildSegments,
    findMatches,
    formatDuration,
    formatSize
  } from '$lib/services/recordingSearch';

  export let labId: string;
  export let onClose: () => void;

  interface Recording {
    id: string;
    node_id: number;
    filename: string;
    title: string;
    started_at: string;
    duration: number;
    cast_size: number;
    log_size: number;
    in_progress: boolean;
  }

  let recordings: Recording[] = [];
  let loading = true;
  let error = '';
  let selected: Recording | null = null;
  let content = '';
  let truncated = false;
  let contentLoading = false;
  let query = '';
  let regexMode = false;
  let currentMatch = 0;

  async function load() {
    loading = true;
    error = '';
    try {
      recordings = await apiGetData<Recording[]>(`/api/labs/${labId}/recordings`);
    } catch (err) {
      error = err instanceof Error ? err.message : 'Failed to load recordings.';
    } finally {
      loading = false;
    }
  }

  onMount(load);

  async function select(recording: Recording) {
    selected = recording;
    contentLoading = true;
    content = '';
    query = '';
    currentMatch = 0;
    try {
      const data = await apiGetData<{ text: string; truncated: boolean }>(
        `/api/labs/${labId}/recordings/${recording.id}/content`
      );
      content = data.text;
      truncated = data.truncated;
    } catch (err) {
      content = err instanceof Error ? err.message : 'Failed to load recording.';
    } finally {
      contentLoading = false;
    }
  }

  $: result = findMatches(content, query, regexMode);
  $: matches = result.matches;
  $: segments = buildSegments(content, matches);
  $: if (currentMatch > 0 && currentMatch >= matches.length) currentMatch = 0;

  async function jump(delta: number) {
    if (!matches.length) return;
    currentMatch = (currentMatch + delta + matches.length) % matches.length;
    await tick();
    document.getElementById(`rec-match-${currentMatch}`)?.scrollIntoView({ block: 'center' });
  }

  function handleKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape') onClose();
    if (event.key === 'Enter' && event.target instanceof HTMLInputElement && event.target.id === 'recording-find') {
      jump(event.shiftKey ? -1 : 1);
    }
  }
</script>

<svelte:window on:keydown={handleKeydown} />

<div
  class="fixed inset-0 z-[100] flex items-center justify-center bg-black/70 p-4"
  role="dialog"
  aria-modal="true"
  aria-label="Session recordings"
  data-testid="recordings-panel"
>
  <button class="absolute inset-0 cursor-default" aria-label="Close recordings" on:click={onClose}></button>
  <div class="relative flex h-[80vh] w-full max-w-6xl flex-col overflow-hidden rounded-2xl border border-gray-700 bg-gray-900 shadow-2xl">
    <div class="flex shrink-0 items-center justify-between border-b border-gray-800 px-4 py-3">
      <div>
        <h2 class="text-sm font-semibold text-gray-100">Session recordings</h2>
        <p class="mt-0.5 text-[11px] text-gray-500">
          Telnet console sessions are recorded automatically. Recordings include everything shown and typed, including passwords.
        </p>
      </div>
      <div class="flex items-center gap-2">
        <button class="rounded-md p-1.5 text-gray-400 transition hover:bg-gray-800 hover:text-gray-100" title="Refresh list" on:click={load}>
          <RefreshCw class="h-4 w-4" />
        </button>
        <button class="rounded-md p-1.5 text-gray-400 transition hover:bg-gray-800 hover:text-gray-100" aria-label="Close" on:click={onClose}>
          <X class="h-4 w-4" />
        </button>
      </div>
    </div>

    <div class="flex min-h-0 flex-1">
      <div class="flex w-72 shrink-0 flex-col border-r border-gray-800">
        <div class="min-h-0 flex-1 overflow-y-auto">
          {#if loading}
            <div class="p-4 text-xs text-gray-500">Loading recordings…</div>
          {:else if error}
            <div class="p-4 text-xs text-red-300">{error}</div>
          {:else if recordings.length === 0}
            <div class="p-4 text-xs leading-relaxed text-gray-500">
              No recordings yet. Open a node's telnet console in this lab — the session is recorded automatically and appears here.
            </div>
          {:else}
            {#each recordings as recording (recording.id)}
              <button
                type="button"
                class={`block w-full border-b border-gray-800/60 px-3 py-2 text-left transition ${selected?.id === recording.id ? 'bg-blue-500/10' : 'hover:bg-gray-800/50'}`}
                on:click={() => select(recording)}
                data-testid="recording-entry"
              >
                <div class="flex items-center justify-between gap-2">
                  <span class="truncate text-xs font-medium text-gray-200">{recording.title || `Node ${recording.node_id}`}</span>
                  {#if recording.in_progress}
                    <span class="flex shrink-0 items-center gap-1 text-[9px] font-semibold uppercase tracking-wider text-red-400">
                      <span class="h-1.5 w-1.5 animate-pulse rounded-full bg-red-500"></span>REC
                    </span>
                  {/if}
                </div>
                <div class="mt-0.5 text-[11px] text-gray-500">{new Date(recording.started_at).toLocaleString()}</div>
                <div class="mt-0.5 font-mono text-[10px] text-gray-600">
                  {formatDuration(recording.duration)} · {formatSize(recording.log_size)}
                </div>
              </button>
            {/each}
          {/if}
        </div>
      </div>

      <div class="flex min-w-0 flex-1 flex-col">
        {#if !selected}
          <div class="flex flex-1 items-center justify-center p-6 text-center text-xs text-gray-600">
            Select a recording to view its log. Use the download buttons on a recording for the asciinema replay (.cast) or the raw log.
          </div>
        {:else}
          <div class="flex shrink-0 flex-wrap items-center gap-2 border-b border-gray-800 px-3 py-2">
            <div class="min-w-0 flex-1">
              <div class="truncate text-xs font-medium text-gray-200">{selected.title || selected.filename}</div>
              <div class="text-[10px] text-gray-500">
                {new Date(selected.started_at).toLocaleString()} · {formatDuration(selected.duration)}
                {#if truncated}<span class="text-yellow-500"> · showing last 512 KB</span>{/if}
              </div>
            </div>
            <a
              href={`/api/labs/${labId}/recordings/${selected.id}/cast`}
              class="inline-flex items-center gap-1 rounded-md border border-gray-700 px-2 py-1 text-[11px] text-gray-300 transition hover:border-blue-500 hover:text-white"
              title="Download asciinema replay (.cast) — play with asciinema-player or the asciinema CLI"
              data-testid="download-cast"
            ><Download class="h-3 w-3" />.cast</a>
            <a
              href={`/api/labs/${labId}/recordings/${selected.id}/log`}
              class="inline-flex items-center gap-1 rounded-md border border-gray-700 px-2 py-1 text-[11px] text-gray-300 transition hover:border-blue-500 hover:text-white"
              title="Download the raw console output log"
              data-testid="download-log"
            ><Download class="h-3 w-3" />raw log</a>
            <button class="rounded-md p-1 text-gray-400 hover:text-gray-100" title="Reload log" on:click={() => selected && select(selected)}>
              <RefreshCw class="h-3.5 w-3.5" />
            </button>
          </div>

          <div class="flex shrink-0 items-center gap-2 border-b border-gray-800 px-3 py-2">
            <input
              id="recording-find"
              type="text"
              bind:value={query}
              placeholder={regexMode ? 'Find (regular expression)…' : 'Find in log…'}
              class="w-56 rounded-md border border-gray-700 bg-gray-950 px-2 py-1 text-xs text-gray-200 outline-none focus:border-blue-500"
              data-testid="recording-find-input"
            />
            <label class="flex cursor-pointer items-center gap-1 text-[11px] text-gray-400" title="Treat the query as a case-insensitive regular expression">
              <input type="checkbox" bind:checked={regexMode} class="accent-blue-500" data-testid="regex-toggle" />
              regex
            </label>
            <span class="text-[11px] text-gray-500" data-testid="match-count">
              {#if result.error}
                <span class="text-red-400">invalid regex</span>
              {:else if query}
                {matches.length ? `${currentMatch + 1}/${matches.length}` : 'no matches'}
              {/if}
            </span>
            <button class="rounded p-1 text-gray-400 hover:text-gray-100 disabled:opacity-30" disabled={!matches.length} aria-label="Previous match" on:click={() => jump(-1)}>
              <ChevronUp class="h-4 w-4" />
            </button>
            <button class="rounded p-1 text-gray-400 hover:text-gray-100 disabled:opacity-30" disabled={!matches.length} aria-label="Next match" on:click={() => jump(1)}>
              <ChevronDown class="h-4 w-4" />
            </button>
          </div>

          <div class="min-h-0 flex-1 overflow-auto bg-gray-950 p-3">
            {#if contentLoading}
              <div class="text-xs text-gray-500">Loading log…</div>
            {:else}
              <pre class="whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed text-gray-300">{#each segments as segment}{#if segment.matchIndex >= 0}<mark
                        id={segment.matchIndex === currentMatch ? `rec-match-${segment.matchIndex}` : undefined}
                        class={segment.matchIndex === currentMatch ? 'bg-blue-500/60 text-white' : 'bg-yellow-500/30 text-yellow-100'}
                      >{segment.text}</mark>{:else}{segment.text}{/if}{/each}</pre>
            {/if}
          </div>
        {/if}
      </div>
    </div>
  </div>
</div>
