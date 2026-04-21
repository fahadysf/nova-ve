<script lang="ts">
  import { onMount } from 'svelte';
  import { page } from '$app/stores';
  import { goto } from '$app/navigation';
  import { authStore } from '$lib/stores/auth';
  import TopologyCanvas from '$lib/components/canvas/TopologyCanvas.svelte';
  import type { NodeData, NetworkData, TopologyLink } from '$lib/types';

  let labId: string = '';
  let labMeta: any = null;
  let nodes: Record<string, NodeData> = {};
  let networks: Record<string, NetworkData> = {};
  let topology: TopologyLink[] = [];
  let loading = true;

  $: labId = $page.params.id ?? '';

  onMount(async () => {
    if (!$authStore.authenticated) {
      goto('/login');
      return;
    }
    await loadLab();
  });

  async function loadLab() {
    try {
      const [metaRes, nodesRes, netsRes, topoRes] = await Promise.all([
        fetch(`/api/labs/${labId}`, { credentials: 'include' }),
        fetch(`/api/labs/${labId}/nodes`, { credentials: 'include' }),
        fetch(`/api/labs/${labId}/networks`, { credentials: 'include' }),
        fetch(`/api/labs/${labId}/topology`, { credentials: 'include' }),
      ]);

      const meta = await metaRes.json();
      const nodeData = await nodesRes.json();
      const netData = await netsRes.json();
      const topoData = await topoRes.json();

      if (meta.code === 200) labMeta = meta.data;
      if (nodeData.code === 200) nodes = nodeData.data;
      if (netData.code === 200) networks = netData.data;
      if (topoData.code === 200 || topoData.status === 'success') topology = topoData.data || [];
    } catch (e) {
      console.error(e);
    } finally {
      loading = false;
    }
  }
</script>

<div class="flex-1 flex flex-col h-screen">
  <header class="h-14 bg-gray-800 border-b border-gray-700 flex items-center px-4 justify-between shrink-0">
    <div class="flex items-center gap-4">
      <button on:click={() => goto('/labs')} class="text-gray-400 hover:text-white">← Back</button>
      <span class="font-bold">{labMeta?.name || labId}</span>
    </div>
    <div class="text-sm text-gray-400">{Object.keys(nodes).length} nodes · {Object.keys(networks).length} networks</div>
  </header>

  {#if loading}
    <div class="flex-1 flex items-center justify-center">Loading lab...</div>
  {:else}
    <TopologyCanvas {nodes} {networks} {topology} />
  {/if}
</div>
