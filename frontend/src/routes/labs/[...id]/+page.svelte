<script lang="ts">
  import { onMount } from 'svelte';
  import { page } from '$app/stores';
  import { goto } from '$app/navigation';
  import { authStore } from '$lib/stores/auth';
  import { apiGetData, ApiError } from '$lib/api';
  import TopologyCanvas from '$lib/components/canvas/TopologyCanvas.svelte';
  import type { LabMeta, NetworkData, NodeData, TopologyLink } from '$lib/types';

  let labId = '';
  let labMeta: LabMeta | null = null;
  let nodes: Record<string, NodeData> = {};
  let networks: Record<string, NetworkData> = {};
  let topology: TopologyLink[] = [];
  let loading = true;
  let error = '';

  $: labId = $page.params.id ?? '';

  onMount(async () => {
    if (!$authStore.authenticated) {
      goto('/login');
      return;
    }
    await loadLab();
  });

  async function loadLab() {
    loading = true;
    error = '';

    try {
      const [meta, nodeData, networkData, topologyData] = await Promise.all([
        apiGetData<LabMeta>(`/labs/${labId}`),
        apiGetData<Record<string, NodeData>>(`/labs/${labId}/nodes`),
        apiGetData<Record<string, NetworkData>>(`/labs/${labId}/networks`),
        apiGetData<TopologyLink[]>(`/labs/${labId}/topology`)
      ]);

      labMeta = meta;
      nodes = nodeData;
      networks = networkData;
      topology = topologyData ?? [];
    } catch (e) {
      error = e instanceof ApiError ? e.message : 'Unable to load the lab.';
    } finally {
      loading = false;
    }
  }
</script>

<div class="flex h-screen flex-1 flex-col">
  <header class="flex h-14 shrink-0 items-center justify-between border-b border-gray-700 bg-gray-800 px-4">
    <div class="flex items-center gap-4">
      <button on:click={() => goto('/labs')} class="text-gray-400 hover:text-white">← Back</button>
      <span class="font-bold">{labMeta?.name || labId}</span>
    </div>
    <div class="text-sm text-gray-400">{Object.keys(nodes).length} nodes · {Object.keys(networks).length} networks</div>
  </header>

  {#if loading}
    <div class="grid flex-1 gap-4 p-6 lg:grid-cols-[18rem_1fr]">
      <div class="animate-pulse rounded-2xl border border-gray-800 bg-gray-900 p-5">
        <div class="h-5 w-2/3 rounded bg-gray-700"></div>
        <div class="mt-4 h-3 w-full rounded bg-gray-800"></div>
        <div class="mt-2 h-3 w-5/6 rounded bg-gray-800"></div>
        <div class="mt-2 h-3 w-3/4 rounded bg-gray-800"></div>
      </div>
      <div class="animate-pulse rounded-2xl border border-gray-800 bg-gray-900/70 p-4">
        <div class="h-full min-h-[26rem] rounded-xl border border-dashed border-gray-800 bg-gray-950/60"></div>
      </div>
    </div>
  {:else if error}
    <div class="flex flex-1 items-center justify-center p-6">
      <div class="max-w-xl rounded-xl border border-red-500/30 bg-red-500/10 p-5 text-red-100">
        <div class="font-medium">Unable to load lab</div>
        <p class="mt-2 text-sm text-red-100/80">{error}</p>
        <button class="mt-4 rounded-md border border-red-400/40 px-3 py-2 text-xs uppercase tracking-[0.2em] text-red-100 hover:bg-red-500/10" on:click={loadLab}>
          Retry
        </button>
      </div>
    </div>
  {:else}
    <div class="grid flex-1 gap-4 p-6 lg:grid-cols-[18rem_1fr]">
      <aside class="rounded-2xl border border-gray-800 bg-gray-900 p-5">
        <h2 class="text-sm uppercase tracking-[0.24em] text-gray-500">Lab Summary</h2>
        <div class="mt-4 space-y-4 text-sm">
          <div>
            <div class="text-gray-500">Owner</div>
            <div class="mt-1 text-gray-100">{labMeta?.owner || 'n/a'}</div>
          </div>
          <div>
            <div class="text-gray-500">Author</div>
            <div class="mt-1 text-gray-100">{labMeta?.author || 'n/a'}</div>
          </div>
          <div>
            <div class="text-gray-500">Path</div>
            <div class="mt-1 break-all text-gray-300">{labMeta?.path || labId}</div>
          </div>
          <div>
            <div class="text-gray-500">Description</div>
            <div class="mt-1 text-gray-300">{labMeta?.description || 'No description set.'}</div>
          </div>
        </div>
      </aside>

      <div class="overflow-hidden rounded-2xl border border-gray-800 bg-gray-900/70">
        <TopologyCanvas labId={labId} {nodes} {networks} {topology} />
      </div>
    </div>
  {/if}
</div>
