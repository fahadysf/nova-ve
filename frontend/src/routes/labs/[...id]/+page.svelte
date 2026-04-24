<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { page } from '$app/stores';
  import { goto } from '$app/navigation';
  import { authStore } from '$lib/stores/auth';
  import { apiGetData, ApiError } from '$lib/api';
  import { toastStore } from '$lib/stores/toasts';
  import { SvelteFlowProvider } from '@xyflow/svelte';
  import TopologyCanvas from '$lib/components/canvas/TopologyCanvas.svelte';
  import type { FolderListing, LabListItem, LabMeta, NetworkData, NodeData, TopologyLink } from '$lib/types';

  let labId = '';
  let labMeta: LabMeta | null = null;
  let rootLabs: LabListItem[] = [];
  let nodes: Record<string, NodeData> = {};
  let networks: Record<string, NetworkData> = {};
  let topology: TopologyLink[] = [];
  let loading = true;
  let error = '';
  type ConsoleBounds = { x: number; y: number; width: number; height: number };
  type ConsoleTab = {
    id: number;
    nodeId: number;
    nodeName: string;
    src: string;
    requestId: number;
  };
  type ConsoleWorkspaceState = ConsoleBounds & {
    tabs: ConsoleTab[];
    activeTabId: number | null;
    maximized: boolean;
    minimized: boolean;
    restoreBounds: ConsoleBounds | null;
  };

  const CONSOLE_HEADER_HEIGHT = 56;
  const preferredLabPath = '/alpine-docker-demo.json';

  let consoleWorkspace: ConsoleWorkspaceState | null = null;
  let activeConsoleTabState: ConsoleTab | null = null;
  let consoleTabCounter = 0;
  let summaryCollapsed = false;
  let inventoryCollapsed = false;
  let lastLoadedRoute = '';
  let dragState: { startX: number; startY: number; originX: number; originY: number } | null = null;
  let resizeState: { startX: number; startY: number; width: number; height: number } | null = null;

  $: labId = $page.params.id ?? '';
  $: isLabIndexRoute = labId === '';
  $: nodeList = Object.values(nodes).sort((a, b) => a.id - b.id);
  $: networkList = Object.values(networks).sort((a, b) => a.id - b.id);
  $: runningConsoleNodes = nodeList.filter((node) => node.status === 2);
  $: {
    const workspace = consoleWorkspace;
    if (!workspace || workspace.activeTabId == null) {
      activeConsoleTabState = null;
    } else {
      activeConsoleTabState = workspace.tabs.find((tab) => tab.id === workspace.activeTabId) ?? null;
    }
  }

  onMount(async () => {
    if (!$authStore.authenticated) {
      goto('/login');
    }
  });

  $: {
    const routeKey = isLabIndexRoute ? 'labs-index' : `lab:${labId}`;
    if ($authStore.authenticated && routeKey !== lastLoadedRoute) {
      lastLoadedRoute = routeKey;
      void (isLabIndexRoute ? loadLabIndex() : loadLab());
    }
  }

  async function loadLabIndex() {
    loading = true;
    error = '';

    try {
      const data = await apiGetData<FolderListing>('/folders/');
      rootLabs = [...(data.labs || [])].sort((left, right) => {
        const leftPreferred = left.path === preferredLabPath;
        const rightPreferred = right.path === preferredLabPath;
        if (leftPreferred !== rightPreferred) {
          return leftPreferred ? -1 : 1;
        }
        return left.file.localeCompare(right.file);
      });
      labMeta = null;
      nodes = {};
      networks = {};
      topology = [];
      consoleWorkspace = null;
    } catch (e) {
      error = e instanceof ApiError ? e.message : 'Unable to load labs.';
    } finally {
      loading = false;
    }
  }

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
      if (consoleWorkspace) {
        const priorActiveTabId = consoleWorkspace.activeTabId;
        const tabs = consoleWorkspace.tabs
          .filter((tab) => nodeData[String(tab.nodeId)]?.status === 2)
          .map((tab) => ({
            ...tab,
            nodeName: nodeData[String(tab.nodeId)]?.name || tab.nodeName
          }));
        consoleWorkspace = tabs.length
          ? {
              ...consoleWorkspace,
              tabs,
              activeTabId:
                tabs.find((tab) => tab.id === priorActiveTabId)?.id ?? tabs[0].id
            }
          : null;
      }
    } catch (e) {
      error = e instanceof ApiError ? e.message : 'Unable to load the lab.';
    } finally {
      loading = false;
    }
  }

  function openLabFromIndex(lab: LabListItem) {
    goto(`/labs${lab.path}`);
  }

  function nextConsoleTabId(): number {
    consoleTabCounter += 1;
    return consoleTabCounter;
  }

  function defaultConsoleBounds(windowOffset = consoleWorkspace?.tabs.length ?? 0): ConsoleBounds {
    const viewportWidth = typeof window !== 'undefined' ? window.innerWidth : 1440;
    const viewportHeight = typeof window !== 'undefined' ? window.innerHeight : 900;
    const width = Math.max(480, Math.min(700, Math.round(viewportWidth * 0.38)));
    const height = Math.max(320, Math.min(520, Math.round(viewportHeight * 0.42)));
    const offset = Math.min(windowOffset, 6) * 28;

    return {
      width,
      height,
      x: Math.max(24, viewportWidth - width - 32 - offset),
      y: Math.max(72, 88 + offset)
    };
  }

  function maximizedConsoleBounds(): ConsoleBounds {
    const viewportWidth = typeof window !== 'undefined' ? window.innerWidth : 1440;
    const viewportHeight = typeof window !== 'undefined' ? window.innerHeight : 900;
    return {
      x: 24,
      y: 72,
      width: Math.max(720, viewportWidth - 48),
      height: Math.max(480, viewportHeight - 96)
    };
  }

  function clampConsoleBounds(bounds: ConsoleBounds): ConsoleBounds {
    const viewportWidth = typeof window !== 'undefined' ? window.innerWidth : 1440;
    const viewportHeight = typeof window !== 'undefined' ? window.innerHeight : 900;
    const width = Math.min(Math.max(520, bounds.width), viewportWidth - 32);
    const height = Math.min(Math.max(360, bounds.height), viewportHeight - 96);

    return {
      width,
      height,
      x: Math.min(Math.max(16, bounds.x), Math.max(16, viewportWidth - width - 16)),
      y: Math.min(Math.max(56, bounds.y), Math.max(56, viewportHeight - height - 16))
    };
  }

  function replaceConsoleWindow(
    updater: (workspace: ConsoleWorkspaceState) => ConsoleWorkspaceState
  ) {
    if (!consoleWorkspace) return;
    consoleWorkspace = updater(consoleWorkspace);
  }

  function activeConsoleTab(): ConsoleTab | null {
    if (!consoleWorkspace || consoleWorkspace.activeTabId == null) return null;
    const activeTabId = consoleWorkspace.activeTabId;
    return consoleWorkspace.tabs.find((tab) => tab.id === activeTabId) ?? null;
  }

  function consoleIframe(tabId?: number): HTMLIFrameElement | null {
    const targetTabId = tabId ?? activeConsoleTabState?.id;
    if (targetTabId == null) return null;
    return document.querySelector(`iframe[data-console-tab-id="${targetTabId}"]`);
  }

  function focusGuacamoleWindow(tabId?: number, attempts = 4) {
    const iframe = consoleIframe(tabId);
    if (!iframe) {
      if (attempts > 0) {
        requestAnimationFrame(() => focusGuacamoleWindow(tabId, attempts - 1));
      }
      return;
    }

    iframe.focus();
    try {
      iframe.contentWindow?.focus();
    } catch (_error) {
      if (attempts > 0) {
        requestAnimationFrame(() => focusGuacamoleWindow(tabId, attempts - 1));
      }
    }
  }

  function focusConsoleWorkspace() {
    setTimeout(() => focusGuacamoleWindow(activeConsoleTabState?.id), 0);
  }

  function buildConsoleWorkspace(existingWorkspace?: ConsoleWorkspaceState): ConsoleWorkspaceState {
    const priorBounds = existingWorkspace?.maximized
      ? maximizedConsoleBounds()
      : existingWorkspace ?? defaultConsoleBounds();
    const bounds = clampConsoleBounds({
      x: priorBounds.x,
      y: priorBounds.y,
      width: priorBounds.width,
      height: priorBounds.height
    });

    return {
      ...bounds,
      tabs: existingWorkspace?.tabs ?? [],
      activeTabId: existingWorkspace?.activeTabId ?? null,
      maximized: existingWorkspace?.maximized ?? false,
      minimized: false,
      restoreBounds: existingWorkspace?.restoreBounds ?? bounds
    };
  }

  async function openConsole(node: NodeData) {
    if (node.status !== 2) {
      toastStore.push('Start the node before opening the HTML5 console.');
      return;
    }

    const existingTab = consoleWorkspace?.tabs.find((tab) => tab.nodeId === node.id);
    if (existingTab) {
      replaceConsoleWindow((workspace) => ({ ...workspace, minimized: false, activeTabId: existingTab.id }));
      await tick();
      focusGuacamoleWindow(existingTab.id);
      return;
    }

    const nextTab: ConsoleTab = {
      id: nextConsoleTabId(),
      nodeId: node.id,
      nodeName: node.name,
      src: `/api/labs/${labId}/nodes/${node.id}/html5?ts=${Date.now()}`,
      requestId: Date.now()
    };
    await tick();
    const nextWorkspace = buildConsoleWorkspace(consoleWorkspace ?? undefined);
    nextWorkspace.tabs = [...nextWorkspace.tabs, nextTab];
    nextWorkspace.activeTabId = nextTab.id;
    consoleWorkspace = nextWorkspace;
    await tick();
    focusGuacamoleWindow(nextTab.id);
  }

  async function handleConsoleNodeChange(event: Event) {
    const selectedId = Number((event.currentTarget as HTMLSelectElement).value);
    const node = nodeList.find((candidate) => candidate.id === selectedId);
    if (node) {
      const existingTab = consoleWorkspace?.tabs.find((tab) => tab.nodeId === node.id);
      if (existingTab) {
        replaceConsoleWindow((workspace) => ({ ...workspace, minimized: false, activeTabId: existingTab.id }));
        await tick();
        focusGuacamoleWindow(existingTab.id);
        return;
      }

      await openConsole(node);
    }
  }

  async function selectConsoleTab(tabId: number) {
    if (!consoleWorkspace) return;
    consoleWorkspace = { ...consoleWorkspace, activeTabId: tabId };
    await tick();
    focusGuacamoleWindow(tabId);
  }

  function closeConsole(tabId: number) {
    if (!consoleWorkspace) return;
    const tabs = consoleWorkspace.tabs.filter((tab) => tab.id !== tabId);
    if (!tabs.length) {
      consoleWorkspace = null;
      dragState = null;
      resizeState = null;
      return;
    }
    const activeTabId =
      tabId === consoleWorkspace.activeTabId ? tabs[tabs.length - 1].id : consoleWorkspace.activeTabId;
    consoleWorkspace = { ...consoleWorkspace, tabs, activeTabId };
  }

  async function reloadConsole() {
    const activeTab = activeConsoleTab();
    if (!activeTab || !consoleWorkspace) return;
    consoleWorkspace = {
      ...consoleWorkspace,
      minimized: false,
      tabs: consoleWorkspace.tabs.map((tab) =>
        tab.id === activeTab.id
          ? { ...tab, src: `/api/labs/${labId}/nodes/${activeTab.nodeId}/html5?ts=${Date.now()}`, requestId: Date.now() }
          : tab
      )
    };
    setTimeout(() => focusGuacamoleWindow(activeTab.id), 0);
  }

  function toggleConsoleMaximize() {
    if (!consoleWorkspace) return;

    if (consoleWorkspace.maximized) {
      const restored = clampConsoleBounds(consoleWorkspace.restoreBounds ?? defaultConsoleBounds());
      consoleWorkspace = {
        ...consoleWorkspace,
        ...restored,
        maximized: false,
        minimized: false,
        restoreBounds: restored
      };
      return;
    }

    const currentBounds = {
      x: consoleWorkspace.x,
      y: consoleWorkspace.y,
      width: consoleWorkspace.width,
      height: consoleWorkspace.height
    };
    consoleWorkspace = {
      ...consoleWorkspace,
      ...maximizedConsoleBounds(),
      maximized: true,
      minimized: false,
      restoreBounds: currentBounds
    };
  }

  function toggleConsoleMinimize() {
    if (!consoleWorkspace) return;
    consoleWorkspace = {
      ...consoleWorkspace,
      minimized: !consoleWorkspace.minimized,
      maximized: consoleWorkspace.minimized ? consoleWorkspace.maximized : false
    };
  }

  function beginConsoleDrag(event: MouseEvent) {
    if (!consoleWorkspace || consoleWorkspace.maximized) return;
    focusConsoleWorkspace();
    dragState = {
      startX: event.clientX,
      startY: event.clientY,
      originX: consoleWorkspace.x,
      originY: consoleWorkspace.y
    };
  }

  function beginConsoleResize(event: MouseEvent) {
    if (!consoleWorkspace || consoleWorkspace.maximized || consoleWorkspace.minimized) return;
    event.stopPropagation();
    focusConsoleWorkspace();
    resizeState = {
      startX: event.clientX,
      startY: event.clientY,
      width: consoleWorkspace.width,
      height: consoleWorkspace.height
    };
  }

  function handleWindowPointerMove(event: MouseEvent) {
    if (dragState && consoleWorkspace) {
      const drag = dragState;
      consoleWorkspace = {
        ...consoleWorkspace,
        ...clampConsoleBounds({
          x: drag.originX + (event.clientX - drag.startX),
          y: drag.originY + (event.clientY - drag.startY),
          width: consoleWorkspace.width,
          height: consoleWorkspace.height
        })
      };
    }

    if (resizeState && consoleWorkspace) {
      const resize = resizeState;
      consoleWorkspace = {
        ...consoleWorkspace,
        ...clampConsoleBounds({
          x: consoleWorkspace.x,
          y: consoleWorkspace.y,
          width: resize.width + (event.clientX - resize.startX),
          height: resize.height + (event.clientY - resize.startY)
        })
      };
    }
  }

  function stopWindowPointerTracking() {
    dragState = null;
    resizeState = null;
  }

  function toggleSummaryCollapsed() {
    summaryCollapsed = !summaryCollapsed;
  }

  function toggleInventoryCollapsed() {
    inventoryCollapsed = !inventoryCollapsed;
  }
</script>

<svelte:window on:mousemove={handleWindowPointerMove} on:mouseup={stopWindowPointerTracking} />

<div class="flex h-screen flex-1 flex-col overflow-hidden">
  <header class="flex h-14 shrink-0 items-center justify-between border-b border-gray-700 bg-gray-800 px-4">
    <div class="flex items-center gap-4">
      <button on:click={() => goto('/labs')} class="text-gray-400 hover:text-white">← Back</button>
      <span class="font-bold">{isLabIndexRoute ? 'Labs' : labMeta?.name || labId}</span>
    </div>
    <div class="text-sm text-gray-400">
      {#if isLabIndexRoute}
        {rootLabs.length} labs
      {:else}
        {Object.keys(nodes).length} nodes · {Object.keys(networks).length} networks
      {/if}
    </div>
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
        <div class="font-medium">{isLabIndexRoute ? 'Unable to load labs' : 'Unable to load lab'}</div>
        <p class="mt-2 text-sm text-red-100/80">{error}</p>
        <button
          class="mt-4 rounded-md border border-red-400/40 px-3 py-2 text-xs uppercase tracking-[0.2em] text-red-100 hover:bg-red-500/10"
          on:click={isLabIndexRoute ? loadLabIndex : loadLab}
        >
          Retry
        </button>
      </div>
    </div>
  {:else if isLabIndexRoute}
    <div class="flex-1 overflow-auto p-6">
      {#if rootLabs.length === 0}
        <div class="rounded-xl border border-gray-800 bg-gray-900 p-6 text-sm text-gray-400">
          No labs were returned by the backend yet.
        </div>
      {:else}
        <div class="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
          {#each rootLabs as lab}
            <button
              on:click={() => openLabFromIndex(lab)}
              class="rounded-lg border border-gray-700 bg-gray-800 p-4 text-left transition hover:border-blue-500 hover:bg-gray-700"
            >
              <div class="font-medium">{lab.file.replace('.json', '').replace('.unl', '')}</div>
              <div class="mt-2 text-xs uppercase tracking-[0.2em] text-gray-500">{lab.path}</div>
              <div class="mt-3 text-sm text-gray-500">{lab.mtime}</div>
            </button>
          {/each}
        </div>
      {/if}
    </div>
  {:else}
    <div class="relative flex flex-1 gap-3 overflow-hidden p-4">
      <aside class="shrink-0 overflow-hidden rounded-2xl border border-gray-800 bg-gray-900 transition-[width] duration-200" style={`width: ${summaryCollapsed ? '3.5rem' : '15rem'}`}>
        {#if summaryCollapsed}
          <button class="flex h-full w-full flex-col items-center justify-between px-2 py-4 text-[10px] uppercase tracking-[0.3em] text-gray-500 hover:text-white" on:click={toggleSummaryCollapsed}>
            <span>»</span>
            <span class="console-rail-label">Lab Summary</span>
            <span></span>
          </button>
        {:else}
          <div class="p-4">
            <div class="flex items-center justify-between">
              <h2 class="text-sm uppercase tracking-[0.24em] text-gray-500">Lab Summary</h2>
              <button class="rounded-md border border-gray-700 px-2 py-1 text-[10px] uppercase tracking-[0.2em] text-gray-300 hover:border-blue-500 hover:text-white" on:click={toggleSummaryCollapsed}>
                Collapse
              </button>
            </div>
            <div class="mt-3 space-y-3 text-xs">
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
          </div>
        {/if}
      </aside>

      <aside class="shrink-0 overflow-hidden rounded-2xl border border-gray-800 bg-gray-900 transition-[width] duration-200" style={`width: ${inventoryCollapsed ? '3.5rem' : '19rem'}`}>
        {#if inventoryCollapsed}
          <button class="flex h-full w-full flex-col items-center justify-between px-2 py-4 text-[10px] uppercase tracking-[0.3em] text-gray-500 hover:text-white" on:click={toggleInventoryCollapsed}>
            <span>»</span>
            <span class="console-rail-label">Inventory</span>
            <span></span>
          </button>
        {:else}
          <div class="p-4">
            <div class="flex items-center justify-between">
              <h2 class="text-sm uppercase tracking-[0.24em] text-gray-500">Inventory</h2>
              <div class="flex items-center gap-2">
                <button
                  class="rounded-md border border-gray-700 px-2 py-1 text-[10px] uppercase tracking-[0.2em] text-gray-300 hover:border-blue-500 hover:text-white"
                  on:click={loadLab}
                >
                  Refresh
                </button>
                <button class="rounded-md border border-gray-700 px-2 py-1 text-[10px] uppercase tracking-[0.2em] text-gray-300 hover:border-blue-500 hover:text-white" on:click={toggleInventoryCollapsed}>
                  Collapse
                </button>
              </div>
            </div>

            <div class="mt-4">
              <div class="text-[10px] uppercase tracking-[0.24em] text-gray-500">Nodes</div>
              {#if nodeList.length === 0}
                <div class="mt-2 rounded-xl border border-gray-800 bg-gray-950/60 p-3 text-xs text-gray-500">
                  No nodes in this lab.
                </div>
              {:else}
                <div class="mt-2 space-y-2">
                  {#each nodeList as node}
                    <div class="rounded-xl border border-gray-800 bg-gray-950/60 p-3 text-xs">
                      <div class="flex items-center justify-between gap-3">
                        <div class="font-medium text-gray-100">{node.name}</div>
                        <span class={`rounded-full px-2 py-1 text-[10px] uppercase tracking-[0.2em] ${node.status === 2 ? 'bg-emerald-500/20 text-emerald-200' : 'bg-gray-800 text-gray-300'}`}>
                          {node.status === 2 ? 'running' : 'stopped'}
                        </span>
                      </div>
                      <div class="mt-2 grid grid-cols-2 gap-1.5 text-[11px] text-gray-400">
                        <div>Type: {node.type}</div>
                        <div>Console: {node.console}</div>
                        <div>CPU: {node.cpu}</div>
                        <div>RAM: {node.ram} MB</div>
                        <div>NICs: {node.interfaces?.length ?? 0}</div>
                        <div>ID: {node.id}</div>
                      </div>
                      <div class="mt-3 flex items-center gap-2">
                        <button
                          class="rounded-md border border-gray-700 px-3 py-2 text-[10px] uppercase tracking-[0.2em] text-gray-300 hover:border-blue-500 hover:text-white disabled:cursor-not-allowed disabled:opacity-40"
                          on:click={() => openConsole(node)}
                          disabled={node.status !== 2}
                        >
                          Open Console
                        </button>
                      </div>
                    </div>
                  {/each}
                </div>
              {/if}
            </div>

            <div class="mt-6">
              <div class="text-[10px] uppercase tracking-[0.24em] text-gray-500">Networks</div>
              {#if networkList.length === 0}
                <div class="mt-2 rounded-xl border border-gray-800 bg-gray-950/60 p-3 text-xs text-gray-500">
                  No networks in this lab.
                </div>
              {:else}
                <div class="mt-2 space-y-2">
                  {#each networkList as network}
                    <div class="rounded-xl border border-gray-800 bg-gray-950/60 p-3 text-xs">
                      <div class="flex items-center justify-between gap-3">
                        <div class="font-medium text-gray-100">{network.name}</div>
                        <span class="rounded-full bg-blue-500/20 px-2 py-1 text-[10px] uppercase tracking-[0.2em] text-blue-200">
                          {network.type}
                        </span>
                      </div>
                      <div class="mt-2 grid grid-cols-2 gap-1.5 text-[11px] text-gray-400">
                        <div>ID: {network.id}</div>
                        <div>Links: {network.count ?? 0}</div>
                        <div>Visible: {network.visibility ? 'yes' : 'no'}</div>
                        <div>Left/Top: {network.left}, {network.top}</div>
                      </div>
                    </div>
                  {/each}
                </div>
              {/if}
            </div>
          </div>
        {/if}
      </aside>

      <div class="min-w-0 flex-1 overflow-hidden rounded-2xl border border-gray-800 bg-gray-900/70">
        <SvelteFlowProvider>
          <TopologyCanvas labId={labId} {nodes} {networks} {topology} on:console={(event) => openConsole(event.detail.node)} />
        </SvelteFlowProvider>
      </div>

      {#if consoleWorkspace}
        <div
          role="dialog"
          tabindex="-1"
          aria-label="Console workspace"
          class="pointer-events-auto fixed flex overflow-hidden rounded-2xl border border-gray-700 bg-gray-900/95 shadow-2xl"
          style={`left: ${consoleWorkspace.x}px; top: ${consoleWorkspace.y}px; width: ${consoleWorkspace.width}px; height: ${consoleWorkspace.minimized ? CONSOLE_HEADER_HEIGHT : consoleWorkspace.height}px; z-index: 50;`}
          on:mousedown={focusConsoleWorkspace}
        >
          <div class="flex h-full w-full flex-col">
            <div
              role="toolbar"
              aria-label="Console workspace controls"
              tabindex="-1"
              class={`flex items-center justify-between border-b border-gray-800 px-3 py-2 ${consoleWorkspace.maximized ? 'cursor-default' : 'cursor-move'}`}
              on:mousedown={beginConsoleDrag}
            >
              <div class="flex items-center gap-3">
                <div class="flex items-center gap-2">
                  <button
                    class="h-3 w-3 rounded-full bg-red-400 transition hover:bg-red-300"
                    aria-label="Close console workspace"
                    on:click={() => (consoleWorkspace = null)}
                  ></button>
                  <button
                    class="h-3 w-3 rounded-full bg-amber-300 transition hover:bg-amber-200"
                    aria-label={consoleWorkspace.minimized ? 'Restore console workspace' : 'Minimize console workspace'}
                    on:click={toggleConsoleMinimize}
                  ></button>
                  <button
                    class="h-3 w-3 rounded-full bg-emerald-400 transition hover:bg-emerald-300"
                    aria-label={consoleWorkspace.maximized ? 'Restore console workspace' : 'Maximize console workspace'}
                    on:click={toggleConsoleMaximize}
                  ></button>
                </div>
                <div>
                  <div class="text-[9px] uppercase tracking-[0.24em] text-gray-500">HTML5 Console Workspace</div>
                  <div class="mt-0.5 text-xs font-medium text-gray-100">{activeConsoleTabState?.nodeName || 'No console selected'}</div>
                </div>
              </div>
              <div class="flex items-center gap-2">
                <label class="sr-only" for="console-node-select">Console target</label>
                <select
                  id="console-node-select"
                  class="rounded-md border border-gray-700 bg-gray-900 px-2 py-1.5 text-[10px] uppercase tracking-[0.18em] text-gray-300 outline-none hover:border-blue-500"
                  value={activeConsoleTabState?.nodeId ?? ''}
                  on:change={handleConsoleNodeChange}
                >
                  <option value="" disabled selected={activeConsoleTabState == null}>Select</option>
                  {#each runningConsoleNodes as node}
                    <option value={node.id}>{node.name}</option>
                  {/each}
                </select>
                <button
                  class="rounded-md border border-gray-700 px-2.5 py-1.5 text-[10px] uppercase tracking-[0.18em] text-gray-300 hover:border-blue-500 hover:text-white"
                  on:click={reloadConsole}
                  disabled={activeConsoleTabState == null}
                >
                  Reload
                </button>
              </div>
            </div>
            {#if !consoleWorkspace.minimized}
              <div class="flex min-h-0 flex-1 flex-col bg-gray-950">
                <div class="flex items-center gap-1 border-b border-gray-800 bg-gray-900/80 px-3 py-2">
                  {#each consoleWorkspace.tabs as tab}
                    <div class={`flex items-center gap-1 rounded-md border ${tab.id === consoleWorkspace.activeTabId ? 'border-blue-500 bg-blue-500/10 text-white' : 'border-gray-700 bg-gray-900 text-gray-300 hover:border-blue-500 hover:text-white'}`}>
                      <button
                        type="button"
                        class="px-3 py-1.5 text-[10px] uppercase tracking-[0.18em]"
                        on:click={() => selectConsoleTab(tab.id)}
                      >
                        {tab.nodeName}
                      </button>
                      <button
                        type="button"
                        class="pr-2 text-gray-500 hover:text-red-400"
                        aria-label={`Close ${tab.nodeName}`}
                        on:click={() => closeConsole(tab.id)}
                      >
                        ×
                      </button>
                    </div>
                  {/each}
                </div>
                <div class="relative min-h-0 flex-1 bg-gray-950">
                  {#each consoleWorkspace.tabs as tab (tab.id)}
                    <div
                      role="presentation"
                      class={`absolute inset-0 h-full w-full ${tab.id === consoleWorkspace.activeTabId ? 'block' : 'hidden'}`}
                      on:mouseenter={() => {
                        if (consoleWorkspace?.activeTabId !== tab.id) {
                          selectConsoleTab(tab.id);
                        } else {
                          focusGuacamoleWindow(tab.id);
                        }
                      }}
                    >
                      <iframe
                        title={`Guacamole console for ${tab.nodeName}`}
                        src={tab.src}
                        data-console-tab-id={tab.id}
                        tabindex="-1"
                        class="h-full w-full border-0"
                        on:load={() => {
                          if (consoleWorkspace?.activeTabId === tab.id) {
                            focusGuacamoleWindow(tab.id);
                          }
                        }}
                      ></iframe>
                    </div>
                  {/each}
                </div>
              </div>
            {/if}
          </div>
          {#if !consoleWorkspace.maximized && !consoleWorkspace.minimized}
            <button
              class="absolute bottom-0 right-0 h-5 w-5 cursor-nwse-resize bg-transparent"
              aria-label="Resize console workspace"
              on:mousedown={beginConsoleResize}
            ></button>
          {/if}
        </div>
      {/if}
    </div>
  {/if}
</div>

<style>
  .console-rail-label {
    writing-mode: vertical-rl;
    transform: rotate(180deg);
  }
</style>
