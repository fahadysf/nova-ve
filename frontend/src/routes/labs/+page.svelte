<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0 -->

<script lang="ts">
  import { onMount } from 'svelte';
  import { authStore, logout } from '$lib/stores/auth';
  import { goto } from '$app/navigation';
  import { apiGetData, ApiError } from '$lib/api';
  import type { FolderListing, LabListItem } from '$lib/types';

  const preferredLabPath = '/alpine-docker-demo.json';

  let labs: LabListItem[] = [];
  let loading = true;
  let error = '';

  onMount(async () => {
    if (!$authStore.authenticated) {
      goto('/login');
      return;
    }
    await fetchLabs();
  });

  async function fetchLabs() {
    loading = true;
    error = '';
    try {
      const data = await apiGetData<FolderListing>('/folders/');
      labs = [...(data.labs || [])].sort((left, right) => {
        const leftPreferred = left.path === preferredLabPath;
        const rightPreferred = right.path === preferredLabPath;
        if (leftPreferred !== rightPreferred) {
          return leftPreferred ? -1 : 1;
        }
        return left.file.localeCompare(right.file);
      });
    } catch (e) {
      error = e instanceof ApiError ? e.message : 'Unable to load labs.';
    } finally {
      loading = false;
    }
  }

  function openLab(lab: LabListItem) {
    goto(`/labs${lab.path}`);
  }

  async function handleLogout() {
    await logout();
    goto('/login');
  }
</script>

<div class="flex-1 flex flex-col">
  <header class="h-14 bg-gray-800 border-b border-gray-700 flex items-center px-4 justify-between">
    <div class="font-bold text-lg">nova-ve</div>
    <div class="flex items-center gap-4">
      <span class="text-sm text-gray-400">{$authStore.user?.username || ''}</span>
      <button on:click={handleLogout} class="text-sm text-red-400 hover:text-red-300">Logout</button>
    </div>
  </header>

  <main class="flex-1 p-6 overflow-auto">
    <h2 class="text-xl font-semibold mb-4">Labs</h2>
    {#if loading}
      <div class="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
        {#each Array.from({ length: 6 }) as _, index}
          <div class="animate-pulse rounded-lg border border-gray-800 bg-gray-900 p-4" aria-hidden="true">
            <div class="h-4 w-2/3 rounded bg-gray-700"></div>
            <div class="mt-3 h-3 w-1/2 rounded bg-gray-800"></div>
          </div>
        {/each}
      </div>
    {:else if error}
      <div class="max-w-xl rounded-xl border border-red-500/30 bg-red-500/10 p-4 text-sm text-red-100">
        <div class="font-medium">Unable to load labs</div>
        <p class="mt-2 text-red-100/80">{error}</p>
        <button class="mt-4 rounded-md border border-red-400/40 px-3 py-2 text-xs uppercase tracking-[0.2em] text-red-100 hover:bg-red-500/10" on:click={fetchLabs}>
          Retry
        </button>
      </div>
    {:else}
      {#if labs.length === 0}
        <div class="rounded-xl border border-gray-800 bg-gray-900 p-6 text-sm text-gray-400">
          No labs were returned by the backend yet.
        </div>
      {:else}
        <div class="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
          {#each labs as lab}
            <button
              on:click={() => openLab(lab)}
              class="rounded-lg border border-gray-700 bg-gray-800 p-4 text-left transition hover:border-blue-500 hover:bg-gray-700"
            >
              <div class="font-medium">{lab.file.replace('.json', '').replace('.unl', '')}</div>
              <div class="mt-2 text-xs uppercase tracking-[0.2em] text-gray-500">{lab.path}</div>
              <div class="mt-3 text-sm text-gray-500">{lab.mtime}</div>
            </button>
          {/each}
        </div>
      {/if}
    {/if}
  </main>
</div>
