<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<!--
  Activity log (#297): the searchable user-activity trail behind System →
  Logs. Free-text search plus area/action/user/time filters, paged
  newest-first, with a CSV export of everything matching the filters.
-->
<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { Download, RefreshCw } from 'lucide-svelte';
  import { apiRequest } from '$lib/api';
  import { toastStore } from '$lib/stores/toasts';
  import {
    ACTIVITY_ACTIONS,
    ACTIVITY_CATEGORIES,
    ACTIVITY_EXPORT_CAP,
    ACTIVITY_MAX_PAGE_SIZE,
    ACTIVITY_PAGE_SIZE,
    activityActor,
    activityPredicate,
    activityToCsv,
    formatActivityTime,
    rangeToEpoch,
    type ActivityEntry,
    type ActivityFilters,
    type ActivityPage
  } from '$lib/services/activityLog';

  const SEARCH_DEBOUNCE_MS = 300;
  const AUTO_REFRESH_MS = 10000;

  // What the controls hold right now…
  let search = '';
  let category = '';
  let action = '';
  let actor = '';
  let from = '';
  let to = '';

  // …versus what the rows on screen were fetched with. Paging and
  // auto-refresh reuse the latter, so a half-typed search never leaks
  // into a request for a different page.
  let applied: ActivityFilters = {};
  let offset = 0;

  let entries: ActivityEntry[] = [];
  let total = 0;
  let actors: string[] = [];
  let loading = false;
  let loaded = false;
  let exporting = false;
  let autoRefresh: ReturnType<typeof setInterval> | undefined;
  let searchTimer: ReturnType<typeof setTimeout> | undefined;
  // Responses can arrive out of order; only the newest request may paint.
  let requestSeq = 0;

  const controlClass =
    'rounded-lg border border-gray-700 bg-gray-950 px-2.5 py-1.5 text-[12px] text-gray-300 focus:border-blue-500 focus:outline-none';

  function currentFilters(): ActivityFilters {
    return {
      q: search.trim() || undefined,
      category: category || undefined,
      action: action || undefined,
      actor: actor || undefined,
      ...rangeToEpoch(from, to)
    };
  }

  async function fetchPage(filters: ActivityFilters, limit: number, pageOffset: number): Promise<ActivityPage> {
    const params = new URLSearchParams();
    Object.entries(filters).forEach(([key, value]) => {
      if (value !== undefined && value !== null && value !== '') params.set(key, String(value));
    });
    params.set('limit', String(limit));
    params.set('offset', String(pageOffset));
    const response = await apiRequest<ActivityPage>(`/system/logs/activity?${params.toString()}`, { retry: 0 });
    return response.data;
  }

  /** Re-read the trail. ``applyControls`` starts over from the newest row
   *  with whatever the controls hold; otherwise the current page reloads. */
  async function load(applyControls: boolean) {
    if (applyControls) {
      applied = currentFilters();
      offset = 0;
    }
    const seq = ++requestSeq;
    const pageOffset = offset;
    loading = true;
    try {
      const page = await fetchPage(applied, ACTIVITY_PAGE_SIZE, pageOffset);
      if (seq !== requestSeq) return;
      entries = page.entries;
      total = page.total;
      actors = page.actors;
      loaded = true;
      // The page may have emptied out (rows aged past the end).
      if (pageOffset > 0 && page.entries.length === 0 && page.total > 0) {
        offset = Math.floor((page.total - 1) / ACTIVITY_PAGE_SIZE) * ACTIVITY_PAGE_SIZE;
        void load(false);
      }
    } catch {
      /* apiRequest already toasted the failure */
    } finally {
      if (seq === requestSeq) loading = false;
    }
  }

  /**
   * Filter controls commit their own value before reloading: an event
   * handler may run before `bind:` has updated the variable, and a request
   * must never carry the previous value of the control.
   */
  function setCategory(event: Event) {
    category = (event.currentTarget as HTMLSelectElement).value;
    void load(true);
  }

  function setAction(event: Event) {
    action = (event.currentTarget as HTMLSelectElement).value;
    void load(true);
  }

  function setActor(event: Event) {
    actor = (event.currentTarget as HTMLSelectElement).value;
    void load(true);
  }

  function setFrom(event: Event) {
    from = (event.currentTarget as HTMLInputElement).value;
    void load(true);
  }

  function setTo(event: Event) {
    to = (event.currentTarget as HTMLInputElement).value;
    void load(true);
  }

  function applySearch() {
    if (searchTimer) clearTimeout(searchTimer);
    searchTimer = undefined;
    void load(true);
  }

  function queueSearch() {
    if (searchTimer) clearTimeout(searchTimer);
    searchTimer = setTimeout(applySearch, SEARCH_DEBOUNCE_MS);
  }

  function reset() {
    if (searchTimer) clearTimeout(searchTimer);
    searchTimer = undefined;
    search = '';
    category = '';
    action = '';
    actor = '';
    from = '';
    to = '';
    void load(true);
  }

  function newer() {
    offset = Math.max(0, offset - ACTIVITY_PAGE_SIZE);
    void load(false);
  }

  function older() {
    if (offset + ACTIVITY_PAGE_SIZE >= total) return;
    offset += ACTIVITY_PAGE_SIZE;
    void load(false);
  }

  function togglePolling(event: Event) {
    if (autoRefresh) {
      clearInterval(autoRefresh);
      autoRefresh = undefined;
    }
    if ((event.target as HTMLInputElement).checked) {
      autoRefresh = setInterval(() => void load(false), AUTO_REFRESH_MS);
    }
  }

  /** CSV of everything matching the filters — the server pages at 1000. */
  async function exportCsv() {
    exporting = true;
    try {
      const collected: ActivityEntry[] = [];
      for (let pageOffset = 0; pageOffset < ACTIVITY_EXPORT_CAP; pageOffset += ACTIVITY_MAX_PAGE_SIZE) {
        const page = await fetchPage(applied, ACTIVITY_MAX_PAGE_SIZE, pageOffset);
        collected.push(...page.entries.slice(0, ACTIVITY_EXPORT_CAP - collected.length));
        if (collected.length >= page.total || page.entries.length === 0) break;
      }
      const blob = new Blob([`\uFEFF${activityToCsv(collected)}`], { type: 'text/csv;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `nova-ve-activity-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')}.csv`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      if (collected.length >= ACTIVITY_EXPORT_CAP && total > ACTIVITY_EXPORT_CAP) {
        toastStore.push(`Export covers the newest ${ACTIVITY_EXPORT_CAP.toLocaleString()} matching entries.`, 'info');
      } else {
        toastStore.push(`Exported ${collected.length} entr${collected.length === 1 ? 'y' : 'ies'}.`, 'success');
      }
    } catch {
      /* apiRequest already toasted the failure */
    } finally {
      exporting = false;
    }
  }

  $: firstShown = total === 0 ? 0 : offset + 1;
  $: lastShown = Math.min(offset + entries.length, total);
  $: atNewest = offset === 0;
  $: atOldest = offset + ACTIVITY_PAGE_SIZE >= total;

  onMount(() => {
    void load(true);
  });

  onDestroy(() => {
    if (autoRefresh) clearInterval(autoRefresh);
    if (searchTimer) clearTimeout(searchTimer);
  });
</script>

<div class="space-y-4">
  <div class="flex flex-wrap items-center gap-2">
    <input
      class="w-64 rounded-lg border border-gray-700 bg-gray-950 px-3 py-1.5 text-[12px] text-gray-200 placeholder-gray-600 focus:border-blue-500 focus:outline-none"
      aria-label="Search activity log"
      placeholder="Search user, node, lab, address…"
      bind:value={search}
      on:input={queueSearch}
      on:keydown={(event) => {
        if (event.key === 'Enter') applySearch();
      }}
      data-testid="activity-search"
    />
    <!-- Each control names itself: the visible layout gives these three no
         text label, so without one a screen reader hears "combobox" x3. -->
    <select
      class={controlClass}
      aria-label="Area"
      bind:value={category}
      on:change={setCategory}
      data-testid="activity-category"
    >
      <option value="">All areas</option>
      {#each ACTIVITY_CATEGORIES as option (option.value)}
        <option value={option.value}>{option.label}</option>
      {/each}
    </select>
    <select
      class={controlClass}
      aria-label="Action"
      bind:value={action}
      on:change={setAction}
      data-testid="activity-action"
    >
      <option value="">All actions</option>
      {#each ACTIVITY_ACTIONS as option (option.value)}
        <option value={option.value}>{option.label}</option>
      {/each}
    </select>
    <select
      class={controlClass}
      aria-label="User"
      bind:value={actor}
      on:change={setActor}
      data-testid="activity-actor"
    >
      <option value="">All users</option>
      {#each actors as name (name)}
        <option value={name}>{name}</option>
      {/each}
    </select>
    <input
      type="datetime-local"
      class={controlClass}
      title="From (local time)"
      aria-label="From"
      bind:value={from}
      on:change={setFrom}
      data-testid="activity-from"
    />
    <span class="text-[12px] text-gray-600">–</span>
    <input
      type="datetime-local"
      class={controlClass}
      title="To (local time)"
      aria-label="To"
      bind:value={to}
      on:change={setTo}
      data-testid="activity-to"
    />
    <button
      class="rounded-md border border-gray-700 px-2.5 py-1.5 text-[10px] uppercase tracking-[0.05em] text-gray-400 transition hover:border-blue-500 hover:text-white"
      on:click={reset}
      data-testid="activity-reset"
    >Clear</button>
    <label class="flex items-center gap-1.5 text-[11px] text-gray-500">
      <input
        type="checkbox"
        class="h-3.5 w-3.5 rounded border-gray-700 bg-gray-950"
        on:change={togglePolling}
        data-testid="activity-autorefresh"
      />
      Auto (10s)
    </label>
    <span class="ml-auto flex items-center gap-2">
      <button
        class="inline-flex items-center gap-1.5 rounded-md border border-gray-700 px-3 py-2 text-[10px] uppercase tracking-[0.05em] text-gray-300 transition hover:border-blue-500 hover:text-white disabled:cursor-not-allowed disabled:opacity-50"
        on:click={() => void exportCsv()}
        disabled={exporting || total === 0}
        data-testid="activity-export"
      >
        <Download class="h-3 w-3" />
        {exporting ? 'Exporting…' : 'Export CSV'}
      </button>
      <button
        class="inline-flex items-center gap-1.5 rounded-md border border-gray-700 px-3 py-2 text-[10px] uppercase tracking-[0.05em] text-gray-300 transition hover:border-blue-500 hover:text-white"
        on:click={() => void load(false)}
        data-testid="activity-refresh"
      >
        <RefreshCw class="h-3 w-3" />
        Refresh
      </button>
    </span>
  </div>

  <section class="overflow-hidden rounded-2xl border border-gray-800 bg-gray-950" data-testid="activity-table">
    {#if entries.length > 0}
      <table class="w-full text-left text-sm">
        <thead>
          <tr class="border-b border-gray-800 text-[10px] uppercase tracking-[0.05em] text-gray-500">
            <th class="px-5 py-2.5">Time</th>
            <th class="px-3 py-2.5">User</th>
            <th class="px-3 py-2.5">Activity</th>
            <th class="px-5 py-2.5">Source</th>
          </tr>
        </thead>
        <tbody>
          {#each entries as entry (entry.id)}
            <tr
              class="border-b border-gray-800/60 last:border-0 hover:bg-gray-800/40"
              data-testid={`activity-row-${entry.id}`}
              data-outcome={entry.outcome}
            >
              <td class="whitespace-nowrap px-5 py-2.5 font-mono text-[11px] text-gray-500" title={new Date(entry.time * 1000).toISOString()}>
                {formatActivityTime(entry.time)}
              </td>
              <td class="px-3 py-2.5">
                <span
                  class={`rounded-full border px-1.5 py-px text-[10px] ${
                    entry.outcome === 'failure'
                      ? 'border-amber-500/40 text-amber-300'
                      : entry.actor
                        ? 'border-blue-500/40 text-blue-300'
                        : 'border-gray-700 text-gray-500'
                  }`}
                  data-testid={`activity-actor-${entry.id}`}
                >{activityActor(entry)}</span>
              </td>
              <td class="px-3 py-2.5 text-[12px] text-gray-300" data-testid={`activity-text-${entry.id}`}>
                {activityPredicate(entry)}
              </td>
              <td class="px-5 py-2.5 font-mono text-[11px] text-gray-600">{entry.ip || '—'}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    {:else if loading}
      <div class="px-5 py-10 text-center text-sm text-gray-600">Loading…</div>
    {:else if loaded}
      <div class="px-5 py-10 text-center text-sm text-gray-600" data-testid="activity-empty">
        {#if search || category || action || actor || from || to}
          No activity matches these filters.
        {:else}
          No activity has been recorded yet.
        {/if}
      </div>
    {/if}
  </section>

  <div class="flex flex-wrap items-center justify-between gap-2 text-[11px] text-gray-600">
    <span data-testid="activity-range">
      {#if loaded}
        Showing {firstShown}–{lastShown} of {total}
      {:else}
        &nbsp;
      {/if}
    </span>
    <span class="flex items-center gap-2">
      <button
        class="rounded-md border border-gray-700 px-2.5 py-1.5 text-[10px] uppercase tracking-[0.05em] text-gray-300 transition hover:border-blue-500 hover:text-white disabled:cursor-not-allowed disabled:opacity-40"
        on:click={newer}
        disabled={atNewest || loading}
        data-testid="activity-newer"
      >← Newer</button>
      <button
        class="rounded-md border border-gray-700 px-2.5 py-1.5 text-[10px] uppercase tracking-[0.05em] text-gray-300 transition hover:border-blue-500 hover:text-white disabled:cursor-not-allowed disabled:opacity-40"
        on:click={older}
        disabled={atOldest || loading}
        data-testid="activity-older"
      >Older →</button>
    </span>
  </div>
</div>
