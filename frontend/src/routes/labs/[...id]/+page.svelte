<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { page } from '$app/stores';
  import { goto } from '$app/navigation';
  import { ChevronDown, ChevronRight, ChevronsLeft, ChevronsRight, Monitor, RefreshCw } from 'lucide-svelte';
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
  type RailSectionKey = 'summary' | 'inventory' | 'nodes' | 'networks';
  type CanvasChangeDetail = {
    reason: string;
    nodeId?: number;
    node?: NodeData;
    nodes?: Record<string, NodeData>;
    networks?: Record<string, NetworkData>;
    topology?: TopologyLink[];
  };
  type ConsoleWorkspaceState = ConsoleBounds & {
    tabs: ConsoleTab[];
    activeTabId: number | null;
    maximized: boolean;
    minimized: boolean;
    restoreBounds: ConsoleBounds | null;
  };

  const CONSOLE_HEADER_HEIGHT = 56;
  // Roboto Mono at 10pt in Guacamole renders cells of ~9px × 18px. Reserve
  // enough room for 80 cols × 24 rows plus the floating window header.
  const CONSOLE_MIN_WIDTH = 760;
  const CONSOLE_MIN_HEIGHT = 504;
  const CONSOLE_CANVAS_INSET = 16;
  const preferredLabPath = '/alpine-docker-demo.json';

  let consoleWorkspace: ConsoleWorkspaceState | null = null;
  let activeConsoleTabState: ConsoleTab | null = null;
  let consoleTabCounter = 0;
  let canvasContainerEl: HTMLDivElement | null = null;
  let railCollapsed = false;
  let railSections: Record<RailSectionKey, boolean> = {
    summary: true,
    inventory: true,
    nodes: true,
    networks: true
  };
  let lastLoadedRoute = '';
  let dragState: { startX: number; startY: number; originX: number; originY: number } | null = null;
  let resizeState: { startX: number; startY: number; width: number; height: number } | null = null;
  let labRefreshInFlight = false;

  const compactIconButtonClass =
    'inline-flex h-7 w-7 items-center justify-center rounded-md border border-gray-800 bg-gray-950/80 text-gray-300 transition hover:border-blue-500/50 hover:text-white disabled:cursor-not-allowed disabled:opacity-40';
  const compactActionButtonClass =
    'inline-flex items-center rounded-md border border-gray-800 bg-gray-950/80 px-2.5 py-1.5 text-[10px] uppercase tracking-[0.18em] text-gray-300 transition hover:border-blue-500/50 hover:text-white disabled:cursor-not-allowed disabled:opacity-40';
  const chromePillClass =
    'inline-flex items-center rounded-full border border-gray-700/80 bg-gray-950/80 px-2.5 py-1 text-[9px] uppercase tracking-[0.18em] text-gray-300';
  const inventoryCardClass =
    'rounded-xl border border-gray-800 bg-gray-900/80 p-2.5 transition hover:border-gray-700';

  $: labId = $page.params.id ?? '';
  $: isLabIndexRoute = labId === '';
  $: nodeList = Object.values(nodes).sort((a, b) => a.id - b.id);
  $: networkList = Object.values(networks).sort((a, b) => a.id - b.id);
  $: runningConsoleNodes = nodeList.filter((node) => node.status === 2 && !node.transientStatus);
  $: runningNodeCount = runningConsoleNodes.length;
  $: currentUserLabel = $authStore.user?.name || $authStore.user?.username || 'Operator';
  $: consoleTabCount = consoleWorkspace?.tabs.length ?? 0;
  $: summaryStats = [
    { label: 'Nodes', value: String(nodeList.length) },
    { label: 'Running', value: String(runningNodeCount) },
    { label: 'Networks', value: String(networkList.length) },
    { label: 'CPU', value: String(nodeList.reduce((total, node) => total + (node.cpu ?? 0), 0)) },
    { label: 'RAM', value: `${nodeList.reduce((total, node) => total + (node.ram ?? 0), 0)} MB` }
  ];
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

  async function loadLab(options: { blocking?: boolean } = {}) {
    const blocking = options.blocking ?? true;
    if (labRefreshInFlight) return;
    labRefreshInFlight = true;
    if (blocking) {
      loading = true;
      error = '';
    }

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
      if (blocking) {
        error = e instanceof ApiError ? e.message : 'Unable to load the lab.';
      } else {
        toastStore.push(e instanceof ApiError ? e.message : 'Unable to refresh the lab state.');
      }
    } finally {
      if (blocking) {
        loading = false;
      }
      labRefreshInFlight = false;
    }
  }

  function syncNodeFromCanvas(nodeId: number, node: NodeData) {
    nodes = {
      ...nodes,
      [String(nodeId)]: node,
    };
  }

  function removeConsoleTabForNode(nodeId: number) {
    const tabId = consoleWorkspace?.tabs.find((tab) => tab.nodeId === nodeId)?.id;
    if (tabId != null) {
      closeConsole(tabId);
    }
  }

  function nodeStatusLabel(node: NodeData): string {
    if (node.transientStatus === 'starting') return 'starting';
    if (node.transientStatus === 'stopping') return 'stopping';
    return node.status === 2 ? 'run' : 'stop';
  }

  function nodeStatusClass(node: NodeData): string {
    if (node.transientStatus) {
      return 'bg-amber-300/20 text-amber-200';
    }
    return node.status === 2 ? 'bg-emerald-500/20 text-emerald-200' : 'bg-gray-800 text-gray-300';
  }

  async function refreshLabFromCanvas(
    event?: CustomEvent<CanvasChangeDetail>
  ) {
    const detail = event?.detail;
    const reason = detail?.reason;
    const nodeId = detail?.nodeId;
    const node = detail?.node;

    if (detail?.nodes) {
      nodes = detail.nodes;
    }
    if (detail?.networks) {
      networks = detail.networks;
    }
    if (detail?.topology) {
      topology = detail.topology;
    }

    if (reason === 'node-start' && nodeId != null && node) {
      syncNodeFromCanvas(nodeId, node);
      return;
    }

    if (reason === 'node-start-pending' && nodeId != null && node) {
      syncNodeFromCanvas(nodeId, node);
      return;
    }

    if ((reason === 'node-stop' || reason === 'node-wipe') && nodeId != null && node) {
      syncNodeFromCanvas(nodeId, node);
      removeConsoleTabForNode(nodeId);
      return;
    }

    if ((reason === 'node-stop-pending' || reason === 'node-wipe-pending') && nodeId != null && node) {
      syncNodeFromCanvas(nodeId, node);
      return;
    }

    if (reason === 'node-edit' && nodeId != null && node) {
      syncNodeFromCanvas(nodeId, node);
      return;
    }

    if (reason === 'node-delete' && nodeId != null) {
      removeConsoleTabForNode(nodeId);
      return;
    }

    if (
      reason === 'topology' ||
      reason === 'node-create' ||
      reason === 'network-create' ||
      reason === 'network-delete'
    ) {
      return;
    }

    if (!isLabIndexRoute) {
      await loadLab({ blocking: false });
    }
  }

  async function retryCurrentView() {
    await (isLabIndexRoute ? loadLabIndex() : loadLab());
  }

  async function refreshLabManually() {
    await loadLab();
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
    const canvasRect = canvasContainerEl?.getBoundingClientRect();
    const anchorX = canvasRect ? canvasRect.left + CONSOLE_CANVAS_INSET : 24;
    const anchorY = canvasRect ? canvasRect.top + CONSOLE_CANVAS_INSET : 88;
    const width = Math.max(CONSOLE_MIN_WIDTH, Math.min(820, Math.round(viewportWidth * 0.5)));
    const height = Math.max(CONSOLE_MIN_HEIGHT, Math.min(560, Math.round(viewportHeight * 0.55)));
    const offset = Math.min(windowOffset, 6) * 28;

    return {
      width,
      height,
      x: Math.max(16, anchorX + offset),
      y: Math.max(56, anchorY + offset)
    };
  }

  function maximizedConsoleBounds(): ConsoleBounds {
    const viewportWidth = typeof window !== 'undefined' ? window.innerWidth : 1440;
    const viewportHeight = typeof window !== 'undefined' ? window.innerHeight : 900;
    return {
      x: 24,
      y: 72,
      width: Math.max(CONSOLE_MIN_WIDTH, viewportWidth - 48),
      height: Math.max(CONSOLE_MIN_HEIGHT, viewportHeight - 96)
    };
  }

  function clampConsoleBounds(bounds: ConsoleBounds): ConsoleBounds {
    const viewportWidth = typeof window !== 'undefined' ? window.innerWidth : 1440;
    const viewportHeight = typeof window !== 'undefined' ? window.innerHeight : 900;
    const width = Math.min(Math.max(CONSOLE_MIN_WIDTH, bounds.width), Math.max(CONSOLE_MIN_WIDTH, viewportWidth - 32));
    const height = Math.min(Math.max(CONSOLE_MIN_HEIGHT, bounds.height), Math.max(CONSOLE_MIN_HEIGHT, viewportHeight - 96));

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
    if (node.status !== 2 || node.transientStatus) {
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

  function beginConsoleDrag(event: PointerEvent) {
    if (!consoleWorkspace || consoleWorkspace.maximized) return;
    if (event.button !== 0) return;
    const target = event.currentTarget as HTMLElement | null;
    target?.setPointerCapture?.(event.pointerId);
    event.preventDefault();
    focusConsoleWorkspace();
    dragState = {
      startX: event.clientX,
      startY: event.clientY,
      originX: consoleWorkspace.x,
      originY: consoleWorkspace.y
    };
  }

  function beginConsoleResize(event: PointerEvent) {
    if (!consoleWorkspace || consoleWorkspace.maximized || consoleWorkspace.minimized) return;
    if (event.button !== 0) return;
    event.stopPropagation();
    const target = event.currentTarget as HTMLElement | null;
    target?.setPointerCapture?.(event.pointerId);
    event.preventDefault();
    focusConsoleWorkspace();
    resizeState = {
      startX: event.clientX,
      startY: event.clientY,
      width: consoleWorkspace.width,
      height: consoleWorkspace.height
    };
  }

  function handleWindowPointerMove(event: PointerEvent) {
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

  function toggleRailSection(section: RailSectionKey) {
    railSections = {
      ...railSections,
      [section]: !railSections[section]
    };
  }

  function toggleRailCollapsed() {
    railCollapsed = !railCollapsed;
  }
</script>

<svelte:window
  on:pointermove={handleWindowPointerMove}
  on:pointerup={stopWindowPointerTracking}
  on:pointercancel={stopWindowPointerTracking}
/>

<div class="flex h-screen flex-1 flex-col overflow-hidden bg-gray-900">
  {#if isLabIndexRoute}
    <header class="flex h-14 shrink-0 items-center justify-between border-b border-gray-700 bg-gray-800 px-4">
      <div class="flex items-center gap-4">
        <button on:click={() => goto('/labs')} class="text-gray-400 hover:text-white">← Back</button>
        <span class="font-bold">Labs</span>
      </div>
      <div class="text-sm text-gray-400">{rootLabs.length} labs</div>
    </header>
  {:else}
    <header class="shrink-0 px-3 pt-2">
      <div class="flex items-start justify-between gap-3 rounded-2xl border border-gray-800 bg-gray-900/95 px-4 py-1">
        <div class="min-w-0 flex items-start gap-3">
          <button
            on:click={() => goto('/labs')}
            class="inline-flex items-center rounded-md border border-gray-800 bg-gray-950/80 px-3 py-2 text-[10px] uppercase tracking-[0.05em] text-gray-300 transition hover:border-blue-500/50 hover:text-white"
          >
            ← Labs
          </button>
          <div class="min-w-0">
            <div class="text-[10px] uppercase tracking-[0.05em] text-gray-500">Lab Editor</div>
            <div class="flex flex-wrap items-center gap-[0.15rem]">
              <h1 class="truncate text-lg font-semibold tracking-tight text-gray-100">{labMeta?.name || labId}</h1>
              <span class={`${chromePillClass} ${runningNodeCount ? 'border-emerald-500/30 text-emerald-200' : ''}`}>
                {runningNodeCount} running
              </span>
              <span class={chromePillClass}>{networkList.length} networks</span>
              <span class={chromePillClass}>{consoleTabCount} consoles</span>
            </div>
            <div class="mt-1 truncate font-mono text-[11px] text-gray-400">{labMeta?.path || labId}</div>
          </div>
        </div>

        <div class="hidden shrink-0 items-center gap-2 md:flex">
          <div class="rounded-xl border border-gray-800 bg-gray-950/70 px-3 py-2">
            <div class="text-[10px] uppercase tracking-[0.05em] text-gray-500">
              {labRefreshInFlight ? 'Refreshing lab' : 'Editor shell'}
            </div>
            <div class="mt-1 text-[11px] text-gray-300">
              {consoleTabCount ? `${consoleTabCount} console tab${consoleTabCount === 1 ? '' : 's'} active` : 'Console workspace idle'}
            </div>
          </div>
          <div class="rounded-xl border border-gray-800 bg-gray-950/70 px-3 py-2 text-right">
            <div class="text-[10px] uppercase tracking-[0.05em] text-gray-500">Operator</div>
            <div class="mt-1 text-[11px] text-gray-200">{currentUserLabel}</div>
          </div>
        </div>
      </div>
    </header>
  {/if}

  {#if loading}
    <div class="grid flex-1 gap-3 p-4 lg:grid-cols-[15rem_1fr]">
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
          class="mt-4 rounded-md border border-red-400/40 px-3 py-2 text-xs uppercase tracking-[0.05em] text-red-100 hover:bg-red-500/10"
          on:click={retryCurrentView}
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
              <div class="mt-2 text-xs uppercase tracking-[0.05em] text-gray-500">{lab.path}</div>
              <div class="mt-3 text-sm text-gray-500">{lab.mtime}</div>
            </button>
          {/each}
        </div>
      {/if}
    </div>
  {:else}
    <div class="relative flex flex-1 gap-2.5 overflow-hidden p-3">
      <aside
        class="min-h-0 shrink-0 overflow-hidden rounded-2xl border border-gray-800 bg-gray-900/95 transition-[width] duration-200"
        style={`width: ${railCollapsed ? '3.5rem' : '15rem'};`}
      >
        <div class="flex h-full flex-col">
          <div class="flex items-center justify-between border-b border-gray-800 px-3.5 py-3">
            {#if railCollapsed}
              <div class="flex w-full flex-col items-center gap-2">
                <button
                  type="button"
                  class={compactIconButtonClass}
                  aria-label="Expand lab rail"
                  title="Expand lab rail"
                  on:click={toggleRailCollapsed}
                >
                  <ChevronsRight class="h-3.5 w-3.5" />
                </button>
                <div class="text-[10px] uppercase tracking-[0.05em] text-gray-500 [writing-mode:vertical-rl]">
                  Rail
                </div>
              </div>
            {:else}
              <div class="flex w-full items-center justify-between gap-2">
                <div class="text-[10px] uppercase tracking-[0.05em] text-gray-500">Lab Rail</div>
                <div class="flex items-center gap-1.5">
                  <button
                    type="button"
                    class={compactIconButtonClass}
                    aria-label="Refresh lab"
                    title="Refresh lab"
                    on:click={refreshLabManually}
                    disabled={labRefreshInFlight}
                  >
                    <RefreshCw class={`h-3.5 w-3.5 ${labRefreshInFlight ? 'animate-spin' : ''}`} />
                  </button>
                  <button
                    type="button"
                    class={compactIconButtonClass}
                    aria-label="Collapse lab rail"
                    title="Collapse lab rail"
                    on:click={toggleRailCollapsed}
                  >
                    <ChevronsLeft class="h-3.5 w-3.5" />
                  </button>
                </div>
              </div>
            {/if}
          </div>

          {#if !railCollapsed}
            <div class="min-h-0 space-y-3 overflow-y-auto p-3 text-xs">
              <section class="rounded-2xl border border-gray-800 bg-gray-950/70">
                <div class="flex items-center justify-between gap-2 border-b border-gray-800 px-3.5 py-2.5">
                  <button
                    type="button"
                    class="flex min-w-0 flex-1 items-center gap-2 text-left text-gray-200"
                    aria-label={railSections.summary ? 'Collapse summary section' : 'Expand summary section'}
                    on:click={() => toggleRailSection('summary')}
                  >
                    {#if railSections.summary}
                      <ChevronDown class="h-3.5 w-3.5 text-gray-500" />
                    {:else}
                      <ChevronRight class="h-3.5 w-3.5 text-gray-500" />
                    {/if}
                    <span class="text-[10px] uppercase tracking-[0.05em] text-gray-500">Summary</span>
                  </button>
                </div>

                {#if railSections.summary}
                  <div class="space-y-3 px-3.5 py-3">
                    <div class="grid grid-cols-2 gap-2">
                      {#each summaryStats as stat}
                        <div class="rounded-xl border border-gray-800 bg-gray-900/70 px-2.5 py-2">
                          <div class="text-[10px] uppercase tracking-[0.18em] text-gray-500">{stat.label}</div>
                          <div class="mt-1 text-[11px] font-medium text-gray-100">{stat.value}</div>
                        </div>
                      {/each}
                    </div>
                    <div>
                      <div class="text-[10px] uppercase tracking-[0.18em] text-gray-500">Owner</div>
                      <div class="mt-1 text-[11px] text-gray-100">{labMeta?.owner || 'n/a'}</div>
                    </div>
                    <div>
                      <div class="text-[10px] uppercase tracking-[0.18em] text-gray-500">Author</div>
                      <div class="mt-1 text-[11px] text-gray-100">{labMeta?.author || 'n/a'}</div>
                    </div>
                    <div>
                      <div class="text-[10px] uppercase tracking-[0.18em] text-gray-500">Path</div>
                      <div class="mt-1 break-all font-mono text-[11px] text-gray-300">{labMeta?.path || labId}</div>
                    </div>
                    <div>
                      <div class="text-[10px] uppercase tracking-[0.18em] text-gray-500">Description</div>
                      <div class="mt-1 text-[11px] leading-5 text-gray-300">{labMeta?.description || 'No description set.'}</div>
                    </div>
                  </div>
                {/if}
              </section>

              <section class="rounded-2xl border border-gray-800 bg-gray-950/70">
                <div class="flex items-center justify-between gap-2 border-b border-gray-800 px-3.5 py-2.5">
                  <button
                    type="button"
                    class="flex min-w-0 flex-1 items-center gap-2 text-left text-gray-200"
                    aria-label={railSections.inventory ? 'Collapse inventory section' : 'Expand inventory section'}
                    on:click={() => toggleRailSection('inventory')}
                  >
                    {#if railSections.inventory}
                      <ChevronDown class="h-3.5 w-3.5 text-gray-500" />
                    {:else}
                      <ChevronRight class="h-3.5 w-3.5 text-gray-500" />
                    {/if}
                    <span class="text-[10px] uppercase tracking-[0.05em] text-gray-500">Inventory</span>
                  </button>
                  <div class="text-[10px] uppercase tracking-[0.18em] text-gray-600">
                    {nodeList.length}n · {networkList.length}net
                  </div>
                </div>

                {#if railSections.inventory}
                  <div class="space-y-2.5 px-3.5 py-3">
                    <div class="rounded-xl border border-gray-800 bg-gray-950/70">
                      <div class="flex items-center justify-between gap-2 border-b border-gray-800 px-2.5 py-2">
                        <button
                          type="button"
                          class="flex min-w-0 flex-1 items-center gap-2 text-left"
                          aria-label={railSections.nodes ? 'Collapse nodes section' : 'Expand nodes section'}
                          on:click={() => toggleRailSection('nodes')}
                        >
                          {#if railSections.nodes}
                            <ChevronDown class="h-3.5 w-3.5 text-gray-500" />
                          {:else}
                            <ChevronRight class="h-3.5 w-3.5 text-gray-500" />
                          {/if}
                          <span class="text-[10px] uppercase tracking-[0.05em] text-gray-500">Nodes</span>
                        </button>
                        <span class="text-[10px] text-gray-600">{nodeList.length}</span>
                      </div>

                      {#if railSections.nodes}
                        {#if nodeList.length === 0}
                          <div class="px-2.5 py-2 text-[11px] text-gray-500">No nodes in this lab.</div>
                        {:else}
                          <div class="space-y-2 px-2.5 py-2.5">
                            {#each nodeList as node}
                              <div class={inventoryCardClass}>
                                <div class="flex items-start justify-between gap-2">
                                  <div class="min-w-0">
                                    <div class="truncate text-[11px] font-medium text-gray-100">{node.name}</div>
                                    <div class="mt-1 text-[10px] uppercase tracking-[0.18em] text-gray-500">
                                      {node.type} · {node.console}
                                    </div>
                                  </div>
                                  <span class={`rounded-full px-1.5 py-0.5 text-[9px] uppercase tracking-[0.05em] ${nodeStatusClass(node)}`}>
                                    {nodeStatusLabel(node)}
                                  </span>
                                </div>
                                <div class="mt-2 grid grid-cols-2 gap-x-2 gap-y-1 text-[10px] text-gray-400">
                                  <div>CPU {node.cpu}</div>
                                  <div>RAM {node.ram}</div>
                                  <div>NIC {node.interfaces?.length ?? 0}</div>
                                  <div>ID {node.id}</div>
                                </div>
                                <div class="mt-2 flex items-center justify-between gap-2 border-t border-gray-800/80 pt-2">
                                  <div class="font-mono text-[10px] text-gray-500">{node.template}</div>
                                  <button
                                    type="button"
                                    class={compactIconButtonClass}
                                    aria-label={`Open console for ${node.name}`}
                                    title="Open console"
                                    on:click={() => openConsole(node)}
                                    disabled={node.status !== 2 || !!node.transientStatus}
                                  >
                                    <Monitor class="h-3.5 w-3.5" />
                                  </button>
                                </div>
                              </div>
                            {/each}
                          </div>
                        {/if}
                      {/if}
                    </div>

                    <div class="rounded-xl border border-gray-800 bg-gray-950/70">
                      <div class="flex items-center justify-between gap-2 border-b border-gray-800 px-2.5 py-2">
                        <button
                          type="button"
                          class="flex min-w-0 flex-1 items-center gap-2 text-left"
                          aria-label={railSections.networks ? 'Collapse networks section' : 'Expand networks section'}
                          on:click={() => toggleRailSection('networks')}
                        >
                          {#if railSections.networks}
                            <ChevronDown class="h-3.5 w-3.5 text-gray-500" />
                          {:else}
                            <ChevronRight class="h-3.5 w-3.5 text-gray-500" />
                          {/if}
                          <span class="text-[10px] uppercase tracking-[0.05em] text-gray-500">Networks</span>
                        </button>
                        <span class="text-[10px] text-gray-600">{networkList.length}</span>
                      </div>

                      {#if railSections.networks}
                        {#if networkList.length === 0}
                          <div class="px-2.5 py-2 text-[11px] text-gray-500">No networks in this lab.</div>
                        {:else}
                          <div class="space-y-2 px-2.5 py-2.5">
                            {#each networkList as network}
                              <div class={inventoryCardClass}>
                                <div class="flex items-start justify-between gap-2">
                                  <div class="min-w-0">
                                    <div class="truncate text-[11px] font-medium text-gray-100">{network.name}</div>
                                    <div class="mt-1 text-[10px] uppercase tracking-[0.18em] text-gray-500">{network.type}</div>
                                  </div>
                                  <span class="rounded-full bg-blue-500/20 px-1.5 py-0.5 text-[9px] uppercase tracking-[0.16em] text-blue-200">
                                    {network.count ?? 0}
                                  </span>
                                </div>
                                <div class="mt-2 grid grid-cols-2 gap-x-2 gap-y-1 text-[10px] text-gray-400">
                                  <div>ID {network.id}</div>
                                  <div>Links {network.count ?? 0}</div>
                                  <div>Visible {network.visibility ? 'yes' : 'no'}</div>
                                  <div>{network.left}, {network.top}</div>
                                </div>
                              </div>
                            {/each}
                          </div>
                        {/if}
                      {/if}
                    </div>
                  </div>
                {/if}
              </section>
            </div>
          {/if}
        </div>
      </aside>

      <div bind:this={canvasContainerEl} class="min-w-0 flex-1 overflow-hidden rounded-2xl border border-gray-800 bg-gray-900/70">
        <SvelteFlowProvider>
          <TopologyCanvas
            labId={labId}
            {nodes}
            {networks}
            {topology}
            on:console={(event) => openConsole(event.detail.node)}
            on:changed={refreshLabFromCanvas}
          />
        </SvelteFlowProvider>
      </div>

      {#if consoleWorkspace}
        <div
          role="dialog"
          tabindex="-1"
          aria-label="Console workspace"
          class="pointer-events-auto fixed flex overflow-hidden rounded-2xl border border-gray-700 bg-gray-900/95 shadow-2xl"
          style={`left: ${consoleWorkspace.x}px; top: ${consoleWorkspace.y}px; width: ${consoleWorkspace.width}px; height: ${consoleWorkspace.minimized ? CONSOLE_HEADER_HEIGHT : consoleWorkspace.height}px; z-index: 50;`}
          on:pointerdown={focusConsoleWorkspace}
        >
          <div class="flex h-full w-full flex-col">
            <div
              role="toolbar"
              aria-label="Console workspace controls"
              tabindex="-1"
              class={`flex items-center justify-between border-b border-gray-800 bg-gray-900/95 px-3.5 py-2.5 backdrop-blur ${consoleWorkspace.maximized ? 'cursor-default' : 'cursor-move'}`}
              on:pointerdown={beginConsoleDrag}
            >
              <div class="flex items-center gap-3">
                <div class="flex items-center gap-2">
                  <button
                    class="h-3 w-3 rounded-full bg-red-400 transition hover:bg-red-300"
                    aria-label="Close console workspace"
                    on:pointerdown|stopPropagation
                    on:click={() => (consoleWorkspace = null)}
                  ></button>
                  <button
                    class="h-3 w-3 rounded-full bg-amber-300 transition hover:bg-amber-200"
                    aria-label={consoleWorkspace.minimized ? 'Restore console workspace' : 'Minimize console workspace'}
                    on:pointerdown|stopPropagation
                    on:click={toggleConsoleMinimize}
                  ></button>
                  <button
                    class="h-3 w-3 rounded-full bg-emerald-400 transition hover:bg-emerald-300"
                    aria-label={consoleWorkspace.maximized ? 'Restore console workspace' : 'Maximize console workspace'}
                    on:pointerdown|stopPropagation
                    on:click={toggleConsoleMaximize}
                  ></button>
                </div>
                <div>
                  <div class="text-[9px] uppercase tracking-[0.05em] text-gray-500">HTML5 Console Workspace</div>
                  <div class="mt-0.5 text-xs font-medium text-gray-100">{activeConsoleTabState?.nodeName || 'No console selected'}</div>
                  <div class="mt-1 font-mono text-[10px] text-gray-500">{labMeta?.path || labId}</div>
                </div>
              </div>
              <div class="flex items-center gap-2">
                <label class="sr-only" for="console-node-select">Console target</label>
                <select
                  id="console-node-select"
                  class="rounded-md border border-gray-700 bg-gray-950 px-2 py-1.5 text-[10px] uppercase tracking-[0.18em] text-gray-300 outline-none hover:border-blue-500"
                  value={activeConsoleTabState?.nodeId ?? ''}
                  on:pointerdown|stopPropagation
                  on:click|stopPropagation
                  on:change={handleConsoleNodeChange}
                >
                  <option value="" disabled selected={activeConsoleTabState == null}>Select</option>
                  {#each runningConsoleNodes as node}
                    <option value={node.id}>{node.name}</option>
                  {/each}
                </select>
                <button
                  class="rounded-md border border-gray-700 bg-gray-950 px-2.5 py-1.5 text-[10px] uppercase tracking-[0.18em] text-gray-300 hover:border-blue-500 hover:text-white"
                  on:pointerdown|stopPropagation
                  on:click={reloadConsole}
                  disabled={activeConsoleTabState == null}
                >
                  Reload
                </button>
              </div>
            </div>
            {#if !consoleWorkspace.minimized}
              <div class="flex min-h-0 flex-1 flex-col bg-gray-950">
                <div class="flex items-center gap-1 border-b border-gray-800 bg-gray-950/90 px-3.5 py-2">
                  {#each consoleWorkspace.tabs as tab}
                    <div class={`flex items-center gap-1 rounded-md border ${tab.id === consoleWorkspace.activeTabId ? 'border-blue-500 bg-blue-500/10 text-white' : 'border-gray-700 bg-gray-900 text-gray-300 hover:border-blue-500 hover:text-white'}`}>
                      <button
                        type="button"
                        class="px-3 py-1.5 text-[10px] uppercase tracking-[0.18em]"
                        on:pointerdown|stopPropagation
                        on:click={() => selectConsoleTab(tab.id)}
                      >
                        {tab.nodeName}
                      </button>
                      <button
                        type="button"
                        class="pr-2 text-gray-500 hover:text-red-400"
                        aria-label={`Close ${tab.nodeName}`}
                        on:pointerdown|stopPropagation
                        on:click={() => closeConsole(tab.id)}
                      >
                        ×
                      </button>
                    </div>
                  {/each}
                </div>
                <div class="relative min-h-0 flex-1 border-t border-gray-950 bg-black">
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
              on:pointerdown={beginConsoleResize}
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
