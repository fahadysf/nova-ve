<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  import { onDestroy, onMount, tick } from 'svelte';
  import { ChevronDown, ChevronUp, Download, RefreshCw, Trash2, X } from 'lucide-svelte';
  import { apiGetData, apiRequest } from '$lib/api';
  import RecordingPlayback from './RecordingPlayback.svelte';
  import { buildSegments, findMatches, formatDuration, formatSize } from '$lib/services/recordingSearch';

  export let labId: string;
  export let labName: string;
  export let nodeId: number | null = null;
  export let nodeName: string | undefined = undefined;
  export let onClose: () => void;

  interface Recording {
    id: string;
    node_id: number;
    node_name?: string;
    console?: 'primary' | 'second' | null;
    filename: string;
    title: string;
    started_at: string;
    stopped_at: string | null;
    duration: number;
    cast_size: number;
    log_size: number;
    in_progress: boolean;
  }

  const buttonClass = 'inline-flex items-center gap-1.5 rounded-md border border-gray-700 px-2 py-1 text-xs text-gray-300 hover:border-blue-500 hover:text-white aria-pressed:border-blue-500 aria-pressed:bg-blue-500/15 aria-pressed:text-white disabled:opacity-40';
  let dialog: HTMLDialogElement;
  let closeButton: HTMLButtonElement;
  let deleteDialog: HTMLDialogElement;
  let cancelDeleteButton: HTMLButtonElement;
  let deleteTarget: Recording | null = null;
  let deleting = false;
  let deleteError = '';
  let deleteStatus = '';
  let recordings: Recording[] = [];
  let loading = true;
  let error = '';
  let nodeFilter = '';
  let selected: Recording | null = null;
  let view: 'playback' | 'transcript' = 'playback';
  let content = '';
  let contentError = '';
  let truncated = false;
  let contentLoading = false;
  let contentRequest: AbortController | undefined;
  const listRequest = new AbortController();
  let query = '';
  let regexMode = false;
  let currentMatch = 0;
  let transcript: HTMLDivElement;
  let selectedUrl = '';

  $: baseUrl = `/api/labs/${labId.split('/').filter(Boolean).map(encodeURIComponent).join('/')}/recordings`;
  $: selectedUrl = selected ? `${baseUrl}/${encodeURIComponent(selected.id)}` : '';
  $: nodeOptions = [...new Map([...recordings].reverse().map((recording) => [recording.node_id, getNodeName(recording)])).entries()]
    .sort((a, b) => a[1].localeCompare(b[1]) || a[0] - b[0]);
  $: visibleRecordings = nodeFilter
    ? recordings.filter((recording) => String(recording.node_id) === nodeFilter)
    : recordings;

  function getNodeName(recording: Recording): string {
    return recording.node_name || recording.title.split(' / ').at(-1) || `Node ${recording.node_id}`;
  }

  function setRecordings(entries: Recording[]) {
    recordings = entries;
    if (nodeFilter && !entries.some((recording) => String(recording.node_id) === nodeFilter)) {
      nodeFilter = '';
    }
  }

  async function load() {
    loading = true;
    error = '';
    try {
      const query = nodeId === null ? '' : `?node_id=${nodeId}`;
      const entries = await apiGetData<Recording[]>(`${baseUrl}${query}`, {
        signal: listRequest.signal, retry: 0, suppressToast: true
      });
      setRecordings(entries);
      if (selected) {
        const updated = recordings.find((recording) => recording.id === selected?.id);
        if (updated) {
          selected = updated;
          if (view === 'transcript') void loadTranscript(updated);
        } else {
          clearSelection();
        }
      }
    } catch (err) {
      if (!listRequest.signal.aborted) {
        error = err instanceof Error ? err.message : 'Failed to load recordings.';
      }
    } finally {
      loading = false;
    }
  }

  onMount(() => {
    dialog.showModal();
    closeButton.focus();
    void load();
  });

  onDestroy(() => {
    listRequest.abort();
    contentRequest?.abort();
    if (deleteDialog?.open) deleteDialog.close();
    if (dialog?.open) dialog.close();
  });

  function close() {
    dialog.close();
    onClose();
  }

  function clearSelection() {
    contentRequest?.abort();
    selected = null;
  }

  function askToDelete(recording: Recording) {
    deleteTarget = recording;
    deleteError = '';
    deleteStatus = '';
    deleteDialog.showModal();
    cancelDeleteButton.focus();
  }

  function cancelDelete() {
    if (deleting) return;
    deleteDialog.close();
    deleteTarget = null;
  }

  async function deleteRecording() {
    if (!deleteTarget || deleting) return;
    const { id } = deleteTarget;
    deleting = true;
    deleteError = '';
    try {
      await apiRequest(`${baseUrl}/${encodeURIComponent(id)}`, { method: 'DELETE', suppressToast: true });
      deleteDialog.close();
      deleteTarget = null;
      clearSelection();
      setRecordings(recordings.filter((recording) => recording.id !== id));
      deleteStatus = 'Recording deleted from the host.';
      await tick();
      closeButton?.focus();
    } catch (err) {
      deleteError = err instanceof Error ? err.message : 'Could not delete the recording.';
    } finally {
      deleting = false;
    }
  }

  function select(recording: Recording) {
    if (selected?.id === recording.id) return;
    contentRequest?.abort();
    selected = recording;
    query = '';
    currentMatch = 0;
    view = recording.in_progress && recording.log_size ? 'transcript' : 'playback';
    if (view === 'transcript') void loadTranscript(recording);
  }

  function showTranscript() {
    if (!selected || view === 'transcript') return;
    view = 'transcript';
    void loadTranscript(selected);
  }

  async function loadTranscript(recording: Recording) {
    contentRequest?.abort();
    const request = new AbortController();
    contentRequest = request;
    contentLoading = true;
    content = '';
    contentError = '';
    truncated = false;
    try {
      const data = await apiGetData<{ text: string; truncated: boolean }>(
        `${baseUrl}/${encodeURIComponent(recording.id)}/content`,
        { signal: request.signal, retry: 0, suppressToast: true }
      );
      if (request.signal.aborted) return;
      content = data.text;
      truncated = data.truncated;
    } catch (err) {
      if (!request.signal.aborted) {
        contentError = err instanceof Error ? err.message : 'Failed to load the transcript.';
      }
    } finally {
      if (!request.signal.aborted) contentLoading = false;
    }
  }

  $: result = findMatches(content, query, regexMode);
  $: matches = result.matches;
  $: segments = buildSegments(content, matches);
  $: if (currentMatch > 0 && currentMatch >= matches.length) currentMatch = 0;

  async function scrollToMatch() {
    await tick();
    transcript?.querySelector('[data-current-match]')?.scrollIntoView({ block: 'center' });
  }

  function resetMatch() {
    currentMatch = 0;
    if (query) void scrollToMatch();
  }

  function jump(delta: number) {
    if (!matches.length) return;
    currentMatch = (currentMatch + delta + matches.length) % matches.length;
    void scrollToMatch();
  }

  function handleFindKey(event: KeyboardEvent) {
    if (event.key === 'Enter') {
      event.preventDefault();
      jump(event.shiftKey ? -1 : 1);
    }
  }
</script>

<dialog
  bind:this={dialog}
  class="m-auto h-[85dvh] max-h-none w-[calc(100%-1rem)] max-w-6xl overflow-visible rounded-2xl border border-gray-700 bg-gray-900 text-gray-200 shadow-2xl backdrop:bg-black/70 sm:w-[calc(100%-2rem)]"
  aria-labelledby="recordings-heading"
  on:cancel|preventDefault={close}
  on:pointerdown={(event) => { if (event.target === dialog && event.button === 0) close(); }}
  data-testid="recordings-panel"
>
  <div class="flex h-full min-h-0 flex-col overflow-hidden rounded-2xl">
    <div class="flex shrink-0 items-start justify-between gap-2 border-b border-gray-800 px-4 py-3">
      <div class="min-w-0">
        <h2 id="recordings-heading" class="text-sm font-semibold text-gray-100">Session recordings</h2>
        <p class="mt-0.5 truncate text-xs text-gray-300">{labName}{nodeName ? ` · ${nodeName}` : ''}</p>
        <p class="mt-1 text-[11px] text-gray-400">Recordings may include passwords and other sensitive console input.</p>
      </div>
      <div class="flex items-center gap-2">
        <button type="button" class={buttonClass} title="Refresh recordings" aria-label="Refresh recordings" disabled={loading} on:click={load}>
          <RefreshCw class="h-4 w-4" aria-hidden="true" />
        </button>
        <button type="button" class={buttonClass} aria-label="Close recordings" bind:this={closeButton} on:click={close}>
          <X class="h-4 w-4" aria-hidden="true" />
        </button>
      </div>
    </div>

    {#if deleteStatus}
      <p role="status" class="border-b border-gray-800 px-4 py-2 text-xs text-emerald-300">{deleteStatus}</p>
    {/if}

    <div class="flex min-h-0 flex-1 flex-col md:flex-row">
      <div class="flex max-h-[33%] shrink-0 flex-col border-b border-gray-800 md:w-72 md:max-h-none md:border-b-0 md:border-r">
        {#if nodeId === null && nodeOptions.length > 1}
          <div class="border-b border-gray-800 px-3 py-2">
            <label for="recording-node" class="sr-only">Filter by node</label>
            <select id="recording-node" bind:value={nodeFilter} on:change={clearSelection}
              class="w-full rounded-md border border-gray-700 bg-gray-950 px-2 py-1 text-xs text-gray-200">
              <option value="">All nodes</option>
              {#each nodeOptions as [id, name] (id)}
                <option value={String(id)}>{name} (#{id})</option>
              {/each}
            </select>
          </div>
        {/if}
        <div class="min-h-0 overflow-y-auto">
          {#if loading && !recordings.length}
            <p role="status" class="p-4 text-xs text-gray-400">Loading recordings…</p>
          {:else if error}
            <p role="alert" class="p-4 text-xs text-red-300">{error}</p>
          {:else if visibleRecordings.length === 0}
            <p class="p-4 text-xs leading-relaxed text-gray-400">
              No recordings yet. Use the record button in a built-in telnet console to start and stop a recording.
            </p>
          {:else}
            {#each visibleRecordings as recording (recording.id)}
              <button type="button"
                class={`block w-full border-b border-gray-800 px-3 py-2.5 text-left transition ${selected?.id === recording.id ? 'bg-blue-500/15' : 'hover:bg-gray-800'}`}
                aria-pressed={selected?.id === recording.id}
                on:click={() => select(recording)}
                data-testid="recording-entry"
              >
                <div class="flex items-center justify-between gap-2">
                  <span class="truncate text-xs font-medium text-gray-100">
                    {getNodeName(recording)}
                    {#if recording.console === 'second'}
                      <span class="text-[11px] font-normal text-gray-400"> · 2nd console</span>
                    {/if}
                  </span>
                  {#if recording.in_progress}
                    <span class="shrink-0 text-[10px] font-medium text-red-300">Recording</span>
                  {/if}
                </div>
                <div class="mt-0.5 text-[11px] text-gray-300">{new Date(recording.started_at).toLocaleString()}</div>
                <div class="mt-0.5 text-[11px] text-gray-400">
                  {formatDuration(recording.duration)} · {formatSize(recording.cast_size + recording.log_size)} total
                </div>
              </button>
            {/each}
          {/if}
        </div>
      </div>

      <div class="flex min-h-0 min-w-0 flex-1 flex-col">
        {#if !selected}
          {#if visibleRecordings.length && !error}
            <p class="m-auto p-4 text-center text-xs text-gray-400">Select a recording to play it or search its transcript.</p>
          {/if}
        {:else}
          <div class="flex shrink-0 flex-wrap items-center gap-2 border-b border-gray-800 px-3 py-2">
            <div class="min-w-0 flex-1">
              <div class="truncate text-xs font-medium text-gray-100">{getNodeName(selected)}</div>
              <div class="text-[11px] text-gray-400">
                {#if selected.console === 'second'}2nd console · {/if}
                Started {new Date(selected.started_at).toLocaleString()}
                {#if selected.stopped_at} · Stopped {new Date(selected.stopped_at).toLocaleString()}{/if}
              </div>
              <div class="text-[11px] text-gray-400" data-testid="recording-sizes">
                Cast {formatSize(selected.cast_size)} · Log {formatSize(selected.log_size)}
              </div>
            </div>
            <a href={`${selectedUrl}/cast`} class={buttonClass} title="Download asciinema cast" data-testid="download-cast">
              <Download class="h-3 w-3" aria-hidden="true" /> .cast
            </a>
            {#if selected.log_size}
              <a href={`${selectedUrl}/log`} class={buttonClass} title="Download raw console log" data-testid="download-log">
                <Download class="h-3 w-3" aria-hidden="true" /> Raw log
              </a>
            {/if}
            <button type="button"
              class="inline-flex items-center gap-1.5 rounded-md border border-red-500/40 px-2 py-1 text-xs text-red-200 hover:bg-red-500/15 disabled:opacity-40"
              disabled={selected.in_progress || loading}
              title={selected.in_progress ? 'Stop recording before deleting' : undefined}
              on:click={() => selected && askToDelete(selected)}
            >
              <Trash2 class="h-3 w-3" aria-hidden="true" /> Delete recording
            </button>
          </div>

          <div role="group" aria-label="Recording view" class="flex shrink-0 items-center gap-2 border-b border-gray-800 p-2">
            <button type="button" class={buttonClass} aria-pressed={view === 'playback'}
              on:click={() => (view = 'playback')}>
              Playback
            </button>
            <button type="button" class={buttonClass} aria-pressed={view === 'transcript'} disabled={!selected.log_size}
              on:click={showTranscript}>
              Transcript
            </button>
          </div>

          {#if view === 'playback'}
            <div class="min-h-0 flex-1 overflow-auto bg-gray-950 p-3">
              {#if selected.in_progress}
                <p class="text-xs text-gray-400">Playback is available after recording stops. Refresh to check for updates.</p>
              {:else}
                {#key selectedUrl}
                  <RecordingPlayback url={`${selectedUrl}/cast`} />
                {/key}
              {/if}
            </div>
          {:else}
            <div class="flex shrink-0 flex-wrap items-center gap-2 border-b border-gray-800 px-3 py-2">
              <input
                type="text"
                aria-label="Find in transcript"
                bind:value={query}
                on:input={resetMatch}
                on:keydown={handleFindKey}
                placeholder="Find in transcript…"
                class="min-w-28 flex-1 rounded-md border border-gray-700 bg-gray-950 px-2 py-1 text-xs text-gray-200 outline-none focus:border-blue-500"
                data-testid="recording-find-input"
              />
              <label class="flex cursor-pointer items-center gap-1 text-[11px] text-gray-300">
                <input type="checkbox" bind:checked={regexMode} on:change={resetMatch} class="accent-blue-500" data-testid="regex-toggle" />
                Regex
              </label>
              <span class="text-[11px] text-gray-400" aria-live="polite" data-testid="match-count">
                {#if result.error}
                  <span class="text-red-300">Invalid regex</span>
                {:else if query && !contentLoading}
                  {matches.length ? `${currentMatch + 1}/${matches.length}` : 'No matches'}
                {/if}
              </span>
              <button class="rounded p-1 text-gray-300 hover:text-white disabled:opacity-30" disabled={!matches.length} aria-label="Previous match" on:click={() => jump(-1)}>
                <ChevronUp class="h-4 w-4" aria-hidden="true" />
              </button>
              <button class="rounded p-1 text-gray-300 hover:text-white disabled:opacity-30" disabled={!matches.length} aria-label="Next match" on:click={() => jump(1)}>
                <ChevronDown class="h-4 w-4" aria-hidden="true" />
              </button>
            </div>
            {#if truncated}
              <p class="border-b border-gray-800 px-3 py-1.5 text-xs text-amber-300">Showing the last 512 KiB. Download the raw log for the full transcript.</p>
            {/if}
            <div class="min-h-0 flex-1 overflow-auto bg-gray-950 p-3" bind:this={transcript}>
              {#if contentLoading}
                <p role="status" class="text-xs text-gray-400">Loading transcript…</p>
              {:else if contentError}
                <p role="alert" class="text-xs text-red-300">{contentError}</p>
              {:else}
                <pre class="whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed text-gray-300">{#each segments as segment}{#if segment.matchIndex >= 0}<mark
                          data-current-match={segment.matchIndex === currentMatch ? '' : undefined}
                          class={segment.matchIndex === currentMatch ? 'bg-blue-500/60 text-white' : 'bg-yellow-500/30 text-yellow-100'}
                        >{segment.text}</mark>{:else}{segment.text}{/if}{/each}</pre>
              {/if}
            </div>
          {/if}
        {/if}
      </div>
    </div>
  </div>
</dialog>

<dialog
  bind:this={deleteDialog}
  class="m-auto w-[calc(100%-2rem)] max-w-sm rounded-xl border border-gray-700 bg-gray-900 p-5 text-gray-200 shadow-2xl backdrop:bg-black/65"
  aria-labelledby="delete-recording-heading"
  aria-describedby="delete-recording-warning"
  on:cancel|preventDefault={cancelDelete}
>
  <h2 id="delete-recording-heading" class="text-sm font-semibold text-gray-100">Delete recording?</h2>
  {#if deleteTarget}
    <p class="mt-1 text-xs text-gray-300">
      {getNodeName(deleteTarget)} · {new Date(deleteTarget.started_at).toLocaleString()}
    </p>
  {/if}
  <p id="delete-recording-warning" class="mt-3 text-xs leading-relaxed text-gray-300">
    Are you sure? This permanently deletes the cast and raw log from the host.
  </p>
  {#if deleteError}
    <p role="alert" class="mt-2 text-xs text-red-300">{deleteError}</p>
  {/if}
  <div class="mt-4 flex justify-end gap-2">
    <button type="button" class={buttonClass} bind:this={cancelDeleteButton} disabled={deleting} on:click={cancelDelete}>
      Cancel
    </button>
    <button type="button" class="rounded-md bg-red-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-red-500 disabled:opacity-60"
      disabled={deleting} on:click={deleteRecording}
    >
      {deleting ? 'Deleting…' : 'Delete recording'}
    </button>
  </div>
</dialog>
