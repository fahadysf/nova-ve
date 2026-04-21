<script lang="ts">
  import { onMount } from 'svelte';
  import { authStore, logout } from '$lib/stores/auth';
  import { goto } from '$app/navigation';

  interface LabItem {
    file: string;
    path: string;
    mtime: string;
  }

  let labs: LabItem[] = [];
  let loading = true;

  onMount(async () => {
    if (!$authStore.authenticated) {
      goto('/login');
      return;
    }
    await fetchLabs();
  });

  async function fetchLabs() {
    try {
      const res = await fetch('/api/folders/', { credentials: 'include' });
      const data = await res.json();
      if (data.code === 200) {
        labs = data.data?.labs || [];
      }
    } catch (e) {
      console.error(e);
    } finally {
      loading = false;
    }
  }

  function openLab(lab: LabItem) {
    goto(`/labs/${encodeURIComponent(lab.file)}`);
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
      <div>Loading labs...</div>
    {:else}
      <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {#each labs as lab}
          <button
            on:click={() => openLab(lab)}
            class="bg-gray-800 border border-gray-700 rounded-lg p-4 text-left hover:border-blue-500 hover:bg-gray-750 transition"
          >
            <div class="font-medium">{lab.file.replace('.json', '').replace('.unl', '')}</div>
            <div class="text-sm text-gray-500 mt-1">{lab.mtime}</div>
          </button>
        {/each}
      </div>
    {/if}
  </main>
</div>
