<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { apiGetData, apiRequest, ApiError } from '$lib/api';
  import { authStore, logout } from '$lib/stores/auth';
  import { toastStore } from '$lib/stores/toasts';

  interface DockerImageRecord {
    id: string;
    repo_tags: string[];
    marker_tags: string[];
    marked: boolean;
    size: number;
    created: string;
    primary_repo_tag: string | null;
  }

  interface DockerImagesPayload {
    images: DockerImageRecord[];
    marker_namespace: string;
  }

  let images: DockerImageRecord[] = [];
  let loading = true;
  let error = '';
  let busyRef: string | null = null;
  let pullReference = '';
  let pullAndMark = true;
  let pulling = false;

  $: currentUser = $authStore.user;
  $: isAdmin = currentUser?.role === 'admin';
  $: visibleImages = images
    .slice()
    .sort((a, b) => {
      // Marked first, then by primary repo tag (case-insensitive).
      if (a.marked !== b.marked) return a.marked ? -1 : 1;
      const an = (a.primary_repo_tag || a.marker_tags[0] || a.id).toLowerCase();
      const bn = (b.primary_repo_tag || b.marker_tags[0] || b.id).toLowerCase();
      return an.localeCompare(bn);
    });

  onMount(async () => {
    if (!$authStore.authenticated) {
      goto('/login');
      return;
    }
    if (!isAdmin) {
      goto('/labs');
      return;
    }
    await refresh();
  });

  async function refresh() {
    loading = true;
    error = '';
    try {
      const data = await apiGetData<DockerImagesPayload>('/docker/images');
      images = data.images ?? [];
    } catch (e) {
      error = e instanceof ApiError ? e.message : 'Unable to load Docker images.';
    } finally {
      loading = false;
    }
  }

  async function markImage(record: DockerImageRecord) {
    const reference = record.primary_repo_tag;
    if (!reference) {
      toastStore.push('No upstream tag to mark — re-tag the image manually first.', 'error');
      return;
    }
    busyRef = reference;
    try {
      await apiRequest('/docker/images/mark', { method: 'POST', body: { image: reference } });
      toastStore.push(`Marked ${reference} for lab use.`, 'success');
      await refresh();
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : 'Mark failed.';
      toastStore.push(msg, 'error');
    } finally {
      busyRef = null;
    }
  }

  async function unmarkImage(record: DockerImageRecord) {
    // Prefer the original tag (so the backend can recompute the marker
    // deterministically); fall back to the marker itself for orphans.
    const reference = record.primary_repo_tag ?? record.marker_tags[0];
    if (!reference) return;
    busyRef = reference;
    try {
      await apiRequest('/docker/images/unmark', {
        method: 'POST',
        body: { image: reference }
      });
      toastStore.push(`Unmarked ${reference}.`, 'success');
      await refresh();
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : 'Unmark failed.';
      toastStore.push(msg, 'error');
    } finally {
      busyRef = null;
    }
  }

  async function pullImage() {
    const reference = pullReference.trim();
    if (!reference) return;
    pulling = true;
    try {
      await apiRequest('/docker/images/pull', {
        method: 'POST',
        body: { reference, mark: pullAndMark }
      });
      toastStore.push(`Pulled ${reference}${pullAndMark ? ' and marked for lab use' : ''}.`, 'success');
      pullReference = '';
      await refresh();
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : 'Pull failed.';
      toastStore.push(msg, 'error');
    } finally {
      pulling = false;
    }
  }

  async function handleLogout() {
    await logout();
    goto('/login');
  }

  function formatSize(bytes: number): string {
    if (!bytes) return '—';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let value = bytes;
    let unitIdx = 0;
    while (value >= 1024 && unitIdx < units.length - 1) {
      value /= 1024;
      unitIdx += 1;
    }
    return `${value.toFixed(value >= 100 || unitIdx === 0 ? 0 : 1)} ${units[unitIdx]}`;
  }
</script>

<div class="flex flex-1 flex-col bg-gray-900 text-gray-200">
  <header class="flex h-14 items-center justify-between border-b border-gray-700 bg-gray-800 px-4">
    <div class="flex items-center gap-4">
      <button
        class="text-lg font-bold tracking-tight text-gray-100 transition hover:text-white"
        on:click={() => goto('/labs')}
      >nova-ve</button>
      <span class="text-xs uppercase tracking-[0.1em] text-gray-500">Admin · Docker Images</span>
    </div>
    <div class="flex items-center gap-4">
      <button class="text-sm text-gray-300 transition hover:text-white" on:click={() => goto('/labs')}>
        Back to Labs
      </button>
      <span class="text-sm text-gray-400">{currentUser?.name || currentUser?.username || ''}</span>
      <button on:click={handleLogout} class="text-sm text-red-400 transition hover:text-red-300">Logout</button>
    </div>
  </header>

  <main class="flex-1 overflow-auto p-6">
    <div class="mx-auto max-w-5xl space-y-6">
      <section class="rounded-2xl border border-gray-800 bg-gray-900/95 px-5 py-4">
        <div class="text-[10px] uppercase tracking-[0.05em] text-gray-500">Lab image curation</div>
        <h1 class="mt-2 text-xl font-semibold tracking-tight text-gray-100">Docker Images</h1>
        <p class="mt-2 text-sm text-gray-400">
          Only images marked here appear in the lab "Add Node" picker. Marking adds a
          local <code class="rounded bg-gray-800 px-1 text-gray-300">nova-ve-lab/&lt;name&gt;</code> reverse tag;
          unmarking removes only that tag, the underlying image is preserved.
        </p>
      </section>

      <section class="rounded-2xl border border-gray-800 bg-gray-900/95 px-5 py-4">
        <h2 class="text-sm font-semibold uppercase tracking-[0.05em] text-gray-400">Pull a new image</h2>
        <form class="mt-3 flex flex-wrap items-center gap-3" on:submit|preventDefault={pullImage}>
          <input
            type="text"
            placeholder="alpine:3.22"
            bind:value={pullReference}
            class="flex-1 min-w-[18rem] rounded-md border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-100 placeholder-gray-500 focus:border-blue-500 focus:outline-none"
            disabled={pulling}
          />
          <label class="flex items-center gap-2 text-sm text-gray-300">
            <input type="checkbox" bind:checked={pullAndMark} disabled={pulling} />
            Mark for lab use after pull
          </label>
          <button
            type="submit"
            class="rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-blue-500 disabled:cursor-not-allowed disabled:opacity-50"
            disabled={pulling || !pullReference.trim()}
          >
            {pulling ? 'Pulling…' : 'Pull image'}
          </button>
        </form>
      </section>

      <section class="rounded-2xl border border-gray-800 bg-gray-900/95">
        <div class="flex items-center justify-between border-b border-gray-800 px-5 py-3">
          <h2 class="text-sm font-semibold uppercase tracking-[0.05em] text-gray-400">Local images</h2>
          <button
            class="text-xs text-blue-400 transition hover:text-blue-300"
            on:click={refresh}
            disabled={loading}
          >{loading ? 'Refreshing…' : 'Refresh'}</button>
        </div>

        {#if loading && images.length === 0}
          <div class="px-5 py-10 text-center text-sm text-gray-500">Loading images…</div>
        {:else if error}
          <div class="px-5 py-10 text-center text-sm text-red-400">{error}</div>
        {:else if visibleImages.length === 0}
          <div class="px-5 py-10 text-center text-sm text-gray-500">
            No Docker images detected locally. Pull one above to get started.
          </div>
        {:else}
          <table class="w-full text-left text-sm">
            <thead>
              <tr class="border-b border-gray-800 text-[10px] uppercase tracking-[0.05em] text-gray-500">
                <th class="px-5 py-2">Image</th>
                <th class="px-3 py-2">Size</th>
                <th class="px-3 py-2">Lab use</th>
                <th class="px-5 py-2 text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {#each visibleImages as record (record.id)}
                {@const reference = record.primary_repo_tag ?? record.marker_tags[0] ?? record.id}
                <tr class="border-b border-gray-800/60 last:border-0 hover:bg-gray-800/40">
                  <td class="px-5 py-3 align-top">
                    <div class="font-mono text-sm text-gray-100">
                      {record.primary_repo_tag ?? '(orphan marker)'}
                    </div>
                    {#if record.repo_tags.length > 1}
                      <div class="mt-1 text-[11px] text-gray-500">
                        also: {record.repo_tags.filter((t) => t !== record.primary_repo_tag).join(', ')}
                      </div>
                    {/if}
                    {#if record.marker_tags.length > 0}
                      <div class="mt-1 text-[11px] text-emerald-400">
                        marker: {record.marker_tags.join(', ')}
                      </div>
                    {/if}
                    <div class="mt-1 text-[10px] text-gray-600">
                      id: {record.id.replace(/^sha256:/, '').slice(0, 12)}
                    </div>
                  </td>
                  <td class="px-3 py-3 align-top text-gray-400">{formatSize(record.size)}</td>
                  <td class="px-3 py-3 align-top">
                    {#if record.marked}
                      <span class="inline-flex items-center rounded-full bg-emerald-900/40 px-2 py-0.5 text-[11px] font-medium text-emerald-300">
                        Available in labs
                      </span>
                    {:else}
                      <span class="inline-flex items-center rounded-full bg-gray-800 px-2 py-0.5 text-[11px] font-medium text-gray-400">
                        Hidden
                      </span>
                    {/if}
                  </td>
                  <td class="px-5 py-3 align-top text-right">
                    {#if record.marked}
                      <button
                        class="rounded-md border border-gray-700 px-3 py-1 text-xs text-gray-200 transition hover:border-red-500 hover:text-red-300 disabled:cursor-not-allowed disabled:opacity-50"
                        on:click={() => unmarkImage(record)}
                        disabled={busyRef === reference}
                      >
                        {busyRef === reference ? '…' : 'Unmark'}
                      </button>
                    {:else}
                      <button
                        class="rounded-md bg-emerald-600 px-3 py-1 text-xs font-medium text-white transition hover:bg-emerald-500 disabled:cursor-not-allowed disabled:opacity-50"
                        on:click={() => markImage(record)}
                        disabled={!record.primary_repo_tag || busyRef === reference}
                        title={!record.primary_repo_tag ? 'No upstream tag — retag the image manually first' : ''}
                      >
                        {busyRef === reference ? '…' : 'Mark for lab use'}
                      </button>
                    {/if}
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        {/if}
      </section>
    </div>
  </main>
</div>
