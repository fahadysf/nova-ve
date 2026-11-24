<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  import { onDestroy, onMount, tick } from 'svelte';
  import { page } from '$app/stores';
  import { goto } from '$app/navigation';
  import {
    Activity,
    ChevronDown,
    ChevronRight,
    ChevronUp,
    ChevronsLeft,
    ChevronsRight,
    Cpu,
    ExternalLink,
    ListVideo,
    Monitor,
    Network as NetworkIcon,
    RefreshCw,
    Server
  } from 'lucide-svelte';
  import { authStore } from '$lib/stores/auth';
  import { apiGetData, apiRequest, ApiError } from '$lib/api';
  import { toastStore } from '$lib/stores/toasts';
  import { buildExternalConsoleUrl, externalConsoleStore } from '$lib/stores/externalConsole';
  import { SvelteFlowProvider } from '@xyflow/svelte';
  import TopologyCanvas from '$lib/components/canvas/TopologyCanvas.svelte';
  import RecordingsPanel from '$lib/components/canvas/RecordingsPanel.svelte';
  import RecordButton from '$lib/components/canvas/RecordButton.svelte';
  import InfoPanelStack from '$lib/components/canvas/InfoPanelStack.svelte';
  import { infoPanels } from '$lib/stores/infoPanels';
  import type {
    FolderListing,
    LabListItem,
    LabMeta,
    Link,
    NetworkData,
    NodeData,
    TopologyLink
  } from '$lib/types';
  import type { LinkChangeEvent } from '$lib/stores/labWs';
  // #245: labState is the single owner of lab editor state. The page holds
  // no independent nodes/networks/topology/links/defaults copies — it reads
  // $labState and writes through the store mutators.
  import { labState } from '$lib/stores/labState';
  import { createConsoleSessionTracker, type ConsoleSession } from '$lib/services/consoleSessions';

  let labId = '';
  let rootLabs: LabListItem[] = [];
  let loading = true;
  let error = '';
  let zenMode = false;
  let recordingsTarget: { nodeId: number | null; nodeName?: string } | null = null;
  let showConsolesInZen = false;
  $: hideConsoles = zenMode && !showConsolesInZen;
  type ConsoleBounds = { x: number; y: number; width: number; height: number };
  type FloatingWindowState = ConsoleBounds & {
    maximized: boolean;
    minimized: boolean;
    restoreBounds: ConsoleBounds | null;
  };
  type ConsoleTab = {
    id: number;
    nodeId: number;
    nodeName: string;
    // #268 — which declared console session this tab shows; a node with a
    // secondary console can have one tab per mode.
    mode: 'primary' | 'second';
    src: string;
    requestId: number;
    floating: FloatingWindowState;
  };
  type ConsoleMode = 'tabbed' | 'floating';
  // #269: advertised external console endpoint (host:port only — the browser
  // hands the scheme URL to the OS protocol handler, credentials never ride along).
  type ExternalConsoleInfo = {
    mode: string;
    scheme: string;
    host: string;
    port: number;
    console: string;
  };
  // #245: 'changed' events are action notifications only — lab state lives
  // in the labState store, so no state payloads ride along anymore.
  type CanvasChangeDetail = {
    reason: string;
    nodeId?: number;
    node?: NodeData;
  };
  type ConsoleWorkspaceState = ConsoleBounds & {
    tabs: ConsoleTab[];
    activeTabId: number | null;
    maximized: boolean;
    minimized: boolean;
    restoreBounds: ConsoleBounds | null;
    mode: ConsoleMode;
    zStack: number[];
  };

  // Roboto Mono at 10pt in Guacamole renders cells of ~9px × 18px. Reserve
  // enough room for 80 cols × 24 rows plus the floating window header.
  const CONSOLE_MIN_WIDTH = 760;
  const CONSOLE_MIN_HEIGHT = 504;
  const CONSOLE_CANVAS_INSET = 16;
  const CONSOLE_MODE_COOKIE = 'novaVeConsoleMode';
  const preferredLabPath = '/alpine-docker-demo.json';

  function readConsoleModeCookie(): ConsoleMode {
    if (typeof document === 'undefined') return 'tabbed';
    const match = document.cookie
      .split('; ')
      .find((entry) => entry.startsWith(`${CONSOLE_MODE_COOKIE}=`));
    const value = match?.split('=')[1];
    return value === 'floating' ? 'floating' : 'tabbed';
  }

  function writeConsoleModeCookie(mode: ConsoleMode) {
    if (typeof document === 'undefined') return;
    const oneYear = 60 * 60 * 24 * 365;
    document.cookie = `${CONSOLE_MODE_COOKIE}=${mode}; path=/; max-age=${oneYear}; SameSite=Lax`;
  }

  let consoleWorkspace: ConsoleWorkspaceState | null = null;
  let consoleMode: ConsoleMode = 'tabbed';
  let activeConsoleTabState: ConsoleTab | null = null;
  let consoleTabCounter = 0;
  let canvasContainerEl: HTMLDivElement | null = null;
  // Bound to <TopologyCanvas bind:this> so reload paths can flush pending
  // layout saves before GETting lab state (otherwise the GET races the
  // 500ms debounced PUT and returns pre-edit positions).
  let canvasRef: TopologyCanvas | null = null;
  let lastLoadedRoute = '';
  let dragState:
    | { startX: number; startY: number; originX: number; originY: number; windowId: number | null }
    | null = null;
  let resizeState:
    | { startX: number; startY: number; width: number; height: number; windowId: number | null }
    | null = null;
  let labRefreshInFlight = false;

  const compactIconButtonClass =
    'inline-flex h-7 w-7 items-center justify-center rounded-md border border-gray-800 bg-gray-950/80 text-gray-300 transition hover:border-blue-500/50 hover:text-white disabled:cursor-not-allowed disabled:opacity-40';
  const compactActionButtonClass =
    'inline-flex items-center rounded-md border border-gray-800 bg-gray-950/80 px-2.5 py-1.5 text-[10px] uppercase tracking-[0.18em] text-gray-300 transition hover:border-blue-500/50 hover:text-white disabled:cursor-not-allowed disabled:opacity-40';
  const chromePillClass =
    'inline-flex items-center rounded-full border border-gray-700/80 bg-gray-950/80 px-2.5 py-1 text-[9px] uppercase tracking-[0.18em] text-gray-300';
  $: labId = $page.params.id ?? '';
  $: isLabIndexRoute = labId === '';
  $: nodeList = Object.values($labState.nodes).sort((a, b) => a.id - b.id);
  $: networkList = Object.values($labState.networks)
    .filter((network) => network.implicit !== true && network.visibility !== false)
    .sort((a, b) => a.id - b.id);
  $: totalVCpu = nodeList.reduce((total, node) => total + (node.cpu ?? 0), 0);
  $: runningConsoleNodes = nodeList.filter((node) => node.status === 2 && !node.transientStatus);
  $: runningNodeCount = runningConsoleNodes.length;
  $: currentUserLabel = $authStore.user?.name || $authStore.user?.username || 'Operator';
  $: consoleTabCount = consoleWorkspace?.tabs.length ?? 0;
  $: consoleSelectorWindows =
    consoleMode === 'floating' && consoleWorkspace
      ? (() => {
          const stack = consoleWorkspace.zStack;
          const topId = stack.length ? stack[stack.length - 1] : null;
          return consoleWorkspace.tabs.map((tab) => ({
            id: tab.id,
            nodeName: tab.nodeName,
            isFront: tab.id === topId,
            isMinimized: tab.floating.minimized
          }));
        })()
      : [];
  // Sentinel id used by tabbed-mode minimized capsule (tab ids start at 1).
  const TABBED_WORKSPACE_BAR_ID = -1;
  $: consoleMinimizedWindows = (() => {
    if (!consoleWorkspace) return [];
    if (consoleMode === 'tabbed') {
      return consoleWorkspace.minimized
        ? [{ id: TABBED_WORKSPACE_BAR_ID, nodeName: activeConsoleTabState?.nodeName ?? 'Console' }]
        : [];
    }
    return consoleWorkspace.tabs
      .filter((tab) => tab.floating.minimized)
      .map((tab) => ({ id: tab.id, nodeName: tab.nodeName }));
  })();

  function handleConsoleRestore(tabId: number) {
    showConsolesInZen = zenMode;
    if (tabId === TABBED_WORKSPACE_BAR_ID) {
      if (consoleWorkspace?.minimized) toggleConsoleMinimize();
      focusConsoleWorkspace();
      return;
    }
    updateFloatingWindow(tabId, { minimized: false });
    bringFloatingWindowToFront(tabId);
    setTimeout(() => focusGuacamoleWindow(tabId), 0);
  }
  function handleConsoleClose(tabId: number) {
    if (tabId === TABBED_WORKSPACE_BAR_ID) {
      consoleWorkspace = null;
      return;
    }
    closeFloatingWindow(tabId);
  }
  function handleConsoleMaximizeFromBar(tabId: number) {
    showConsolesInZen = zenMode;
    if (tabId === TABBED_WORKSPACE_BAR_ID) {
      if (!consoleWorkspace) return;
      if (consoleWorkspace.minimized) toggleConsoleMinimize();
      if (!consoleWorkspace.maximized) toggleConsoleMaximize();
      focusConsoleWorkspace();
      return;
    }
    updateFloatingWindow(tabId, { minimized: false });
    const tab = consoleWorkspace?.tabs.find((candidate) => candidate.id === tabId);
    if (tab && !tab.floating.maximized) toggleFloatingMaximize(tabId);
    setTimeout(() => focusGuacamoleWindow(tabId), 0);
  }
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

  // #300: the server sees a console open but has no other way to learn that
  // one closed — closing a tab only unloads an iframe. Track every console
  // the page holds and report each one that ends, however it ends.
  const consoleSessions = createConsoleSessionTracker(reportConsoleClosed);
  $: consoleSessions.sync(consoleWorkspace?.tabs ?? [], labId);

  function reportConsoleClosed(session: ConsoleSession) {
    // keepalive: the report must still go out while the page is unloading.
    // It is deliberately not an apiRequest — this must neither show a
    // toast nor bounce to the sign-in page if the session just expired.
    void fetch(
      `/api/labs/${session.labId}/nodes/${session.nodeId}/console/close?console=${session.mode}`,
      { method: 'POST', credentials: 'include', keepalive: true }
    ).catch(() => {
      // A report that does not arrive only costs a "closed" row in the activity log.
    });
  }

  function reportOpenConsolesClosed() {
    consoleSessions.reportAll();
  }

  onMount(() => {
    // Closing the tab or window, reloading, or leaving the site.
    window.addEventListener('pagehide', reportOpenConsolesClosed);
    return () => window.removeEventListener('pagehide', reportOpenConsolesClosed);
  });
  // Moving to another page inside the app destroys this component instead
  // of unloading the page.
  onDestroy(reportOpenConsolesClosed);

  onMount(async () => {
    consoleMode = readConsoleModeCookie();
    if (!$authStore.authenticated) {
      goto('/login');
    }
  });

  $: {
    const routeKey = isLabIndexRoute ? 'labs-index' : `lab:${labId}`;
    if ($authStore.authenticated && routeKey !== lastLoadedRoute) {
      lastLoadedRoute = routeKey;
      zenMode = false;
      recordingsTarget = null;
      void (isLabIndexRoute ? loadLabIndex() : loadLab());
    }
  }

  function toggleZenMode(event: MouseEvent & { currentTarget: HTMLButtonElement }) {
    showConsolesInZen = false;
    zenMode = !zenMode;
    event.currentTarget.focus();
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
      labState.reset();
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
      // Fire-and-forget host-state reconciliation — provisions any missing
      // Linux bridges for this lab's networks. Restored labs and labs whose
      // bridges were lost across a host reboot/manual cleanup find their
      // host state already prepared by the time the user starts a node or
      // creates a link.
      apiRequest(`/labs/${labId}/runtime/reconcile`, {
        method: 'POST',
        suppressToast: true
      }).catch(() => {
        /* best-effort; surfacing the failure in a toast would be noisy
           on every lab open and isn't actionable for the user. */
      });

      const [meta, nodeData, networkData, topologyData, linksData] = await Promise.all([
        apiGetData<LabMeta>(`/labs/${labId}`),
        apiGetData<Record<string, NodeData>>(`/labs/${labId}/nodes`),
        apiGetData<Record<string, NetworkData>>(`/labs/${labId}/networks?include_hidden=true`),
        apiGetData<TopologyLink[]>(`/labs/${labId}/topology`),
        // US-082: v2 links[] feeds the canvas edge derivation. Catch errors so
        // an isolated /links failure doesn't block the rest of the lab load.
        apiGetData<Link[]>(`/labs/${labId}/links`).catch(() => [] as Link[])
      ]);

      // One owner (#245): the snapshot lands in labState; the canvas and
      // this page both read it. defaults stays whatever the store holds —
      // the parallel GETs do not fetch it. Positions with a pending layout
      // PUT (e.g. re-queued after a failed flush) survive the snapshot.
      labState.setLabId(labId);
      labState.applySnapshot(
        {
          meta,
          nodes: nodeData,
          networks: networkData,
          topology: topologyData ?? [],
          links: linksData ?? []
        },
        canvasRef ? canvasRef.pendingLayoutIds() : undefined
      );
      if (consoleWorkspace) {
        const priorActiveTabId = consoleWorkspace.activeTabId;
        const tabs = consoleWorkspace.tabs
          .filter((tab) => nodeData[String(tab.nodeId)]?.status === 2)
          .map((tab) => ({
            ...tab,
            nodeName: nodeData[String(tab.nodeId)]?.name || tab.nodeName
          }));
        const survivingIds = new Set(tabs.map((tab) => tab.id));
        consoleWorkspace = tabs.length
          ? {
              ...consoleWorkspace,
              tabs,
              zStack: consoleWorkspace.zStack.filter((id) => survivingIds.has(id)),
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

  function removeConsoleTabForNode(nodeId: number) {
    const tabId = consoleWorkspace?.tabs.find((tab) => tab.nodeId === nodeId)?.id;
    if (tabId != null) {
      closeConsole(tabId);
    }
  }

  async function refreshLabFromCanvas(
    event?: CustomEvent<CanvasChangeDetail>
  ) {
    const detail = event?.detail;
    const reason = detail?.reason;
    const nodeId = detail?.nodeId;
    const node = detail?.node;

    if (reason === 'node-start' && nodeId != null && node) {
      labState.upsertNode(nodeId, node);
      return;
    }

    if (reason === 'node-start-pending' && nodeId != null && node) {
      labState.upsertNode(nodeId, node);
      return;
    }

    if ((reason === 'node-stop' || reason === 'node-wipe') && nodeId != null && node) {
      labState.upsertNode(nodeId, node);
      removeConsoleTabForNode(nodeId);
      return;
    }

    if ((reason === 'node-stop-pending' || reason === 'node-wipe-pending') && nodeId != null && node) {
      labState.upsertNode(nodeId, node);
      return;
    }

    if (reason === 'node-edit' && nodeId != null && node) {
      labState.upsertNode(nodeId, node);
      return;
    }

    if (reason === 'node-delete' && nodeId != null) {
      removeConsoleTabForNode(nodeId);
      return;
    }

    // #245: a guarded write got 412 — the lab changed elsewhere. Re-adopt
    // the server snapshot FIRST (applySnapshot keeps our pending positions
    // and picks up the fresh revision), then retry the requeued flush with
    // the new If-Match token.
    if (reason === 'layout-conflict' && !isLabIndexRoute) {
      await loadLab({ blocking: false });
      if (canvasRef) {
        await canvasRef.flushPendingLayout();
      }
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

    // Flush any debounced layout PUT before reloading: the GET below must
    // observe the just-edited positions, not pre-edit ones. Without this,
    // node-delete (which falls through to reload) races the 500ms layout
    // debounce and reverts the canvas to the previous state.
    if (canvasRef) {
      await canvasRef.flushPendingLayout();
    }

    // Paired-template creation adds N nodes + an implicit bridge + 2N link
    // halves in one shot; full lab reload is the simplest way to pick all of
    // that up consistently in the labState store.
    if (reason === 'paired-create' && !isLabIndexRoute) {
      await loadLab({ blocking: false });
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
    if (canvasRef) {
      await canvasRef.flushPendingLayout();
    }
    await loadLab();
  }

  // #153: react to per-link WS mutations forwarded by TopologyCanvas so the
  // canvas updates without a manual reload. The canvas already reacts to
  // ``links`` prop changes, so reassigning the array is enough to trigger
  // edge re-derivation. Cascade deletes (orphan implicit-bridge halves) fire
  // one event per link_id; both splices land in the same microtask.
  function handleLinkChange(event: CustomEvent<LinkChangeEvent>) {
    const change = event.detail;
    if (change.kind === 'deleted') {
      labState.removeLink(change.link_id);
      return;
    }
    labState.upsertLink(change.link);
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

  function isRecordedTab(tab: ConsoleTab): boolean {
    // #291: in-built telnet consoles can be recorded; vnc/rdp cannot.
    const node = $labState.nodes?.[tab.nodeId];
    const mode = tab.mode === 'second' ? node?.secondConsole : node?.console;
    return (mode ?? 'telnet') === 'telnet';
  }

  // #291: on-demand recording state per console tab (recording off by default).
  let recordingStates: Record<number, boolean> = {};

  async function refreshRecordingState(tab: ConsoleTab) {
    try {
      const response = await apiRequest<{ recording: boolean }>(
        `/labs/${labId}/nodes/${tab.nodeId}/recording?console=${tab.mode}`
      );
      recordingStates = { ...recordingStates, [tab.id]: response.data.recording };
    } catch {
      // Status is best-effort; the record toggle surfaces real errors.
    }
  }

  async function toggleRecording(tab: ConsoleTab) {
    const action = recordingStates[tab.id] ? 'stop' : 'start';
    try {
      const response = await apiRequest<{ recording: boolean }>(
        `/labs/${labId}/nodes/${tab.nodeId}/recording`,
        { method: 'POST', body: { action, console: tab.mode } }
      );
      recordingStates = { ...recordingStates, [tab.id]: response.data.recording };
      toastStore.push(
        action === 'start'
          ? `Recording ${tab.nodeName} console — press the red button again to stop.`
          : `Recording of ${tab.nodeName} stopped and saved.`,
        'success'
      );
    } catch {
      // apiRequest already raised an error toast.
    }
  }

  function consoleIframe(tabId?: number): HTMLIFrameElement | null {
    const targetTabId = tabId ?? activeConsoleTabState?.id;
    if (targetTabId == null) return null;
    return document.querySelector(`iframe[data-console-tab-id="${targetTabId}"]`);
  }

  function focusGuacamoleWindow(tabId?: number, attempts = 4) {
    if (hideConsoles) return;

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
      restoreBounds: existingWorkspace?.restoreBounds ?? bounds,
      mode: existingWorkspace?.mode ?? consoleMode,
      zStack: existingWorkspace?.zStack ?? []
    };
  }

  function defaultFloatingWindowBounds(offsetIndex: number): FloatingWindowState {
    const base = defaultConsoleBounds(offsetIndex);
    return {
      ...base,
      maximized: false,
      minimized: false,
      restoreBounds: base
    };
  }

  function bringFloatingWindowToFront(tabId: number) {
    if (!consoleWorkspace) return;
    const zStack = [...consoleWorkspace.zStack.filter((id) => id !== tabId), tabId];
    consoleWorkspace = { ...consoleWorkspace, zStack, activeTabId: tabId };
  }

  function setConsoleMode(mode: ConsoleMode) {
    if (consoleMode === mode) return;
    consoleMode = mode;
    writeConsoleModeCookie(mode);
    if (!consoleWorkspace || consoleWorkspace.mode === mode) {
      if (consoleWorkspace) {
        consoleWorkspace = { ...consoleWorkspace, mode };
      }
      return;
    }
    let zStack = consoleWorkspace.zStack;
    let tabs = consoleWorkspace.tabs;
    if (mode === 'floating') {
      // Ensure every open tab has positioned floating bounds and a z-stack entry.
      const seen = new Set(zStack);
      tabs = tabs.map((tab, index) => ({
        ...tab,
        floating: tab.floating ?? defaultFloatingWindowBounds(index)
      }));
      const newOrder = [
        ...zStack.filter((id) => tabs.some((tab) => tab.id === id)),
        ...tabs.filter((tab) => !seen.has(tab.id)).map((tab) => tab.id)
      ];
      zStack = newOrder;
    }
    consoleWorkspace = { ...consoleWorkspace, mode, tabs, zStack };
  }

  function toggleConsoleMode() {
    setConsoleMode(consoleMode === 'tabbed' ? 'floating' : 'tabbed');
  }

  function updateFloatingWindow(tabId: number, patch: Partial<FloatingWindowState>) {
    if (!consoleWorkspace) return;
    consoleWorkspace = {
      ...consoleWorkspace,
      tabs: consoleWorkspace.tabs.map((tab) =>
        tab.id === tabId ? { ...tab, floating: { ...tab.floating, ...patch } } : tab
      )
    };
  }

  function toggleFloatingMaximize(tabId: number) {
    if (!consoleWorkspace) return;
    const tab = consoleWorkspace.tabs.find((candidate) => candidate.id === tabId);
    if (!tab) return;
    bringFloatingWindowToFront(tabId);
    if (tab.floating.maximized) {
      const restored = clampConsoleBounds(tab.floating.restoreBounds ?? defaultConsoleBounds());
      updateFloatingWindow(tabId, { ...restored, maximized: false, minimized: false, restoreBounds: restored });
      return;
    }
    const currentBounds: ConsoleBounds = {
      x: tab.floating.x,
      y: tab.floating.y,
      width: tab.floating.width,
      height: tab.floating.height
    };
    updateFloatingWindow(tabId, {
      ...maximizedConsoleBounds(),
      maximized: true,
      minimized: false,
      restoreBounds: currentBounds
    });
  }

  function toggleFloatingMinimize(tabId: number) {
    if (!consoleWorkspace) return;
    const tab = consoleWorkspace.tabs.find((candidate) => candidate.id === tabId);
    if (!tab) return;
    const minimized = !tab.floating.minimized;
    updateFloatingWindow(tabId, {
      minimized,
      maximized: minimized ? false : tab.floating.maximized
    });
  }

  function reloadFloatingConsole(tabId: number) {
    if (!consoleWorkspace) return;
    bringFloatingWindowToFront(tabId);
    consoleWorkspace = {
      ...consoleWorkspace,
      tabs: consoleWorkspace.tabs.map((tab) =>
        tab.id === tabId
          ? {
              ...tab,
              src: `/api/labs/${labId}/nodes/${tab.nodeId}/html5?ts=${Date.now()}`,
              requestId: Date.now(),
              floating: { ...tab.floating, minimized: false }
            }
          : tab
      )
    };
    setTimeout(() => focusGuacamoleWindow(tabId), 0);
  }

  function closeFloatingWindow(tabId: number) {
    if (!consoleWorkspace) return;
    const tabs = consoleWorkspace.tabs.filter((tab) => tab.id !== tabId);
    if (!tabs.length) {
      consoleWorkspace = null;
      dragState = null;
      resizeState = null;
      return;
    }
    const zStack = consoleWorkspace.zStack.filter((id) => id !== tabId);
    const activeTabId =
      tabId === consoleWorkspace.activeTabId
        ? zStack[zStack.length - 1] ?? tabs[tabs.length - 1].id
        : consoleWorkspace.activeTabId;
    consoleWorkspace = { ...consoleWorkspace, tabs, zStack, activeTabId };
  }

  // #269: with external mode on, console actions hand the node's console to
  // the OS protocol handler (telnet://, vnc://, rdp://) instead of the
  // embedded workspace. The backend advertises the reachable host:port for
  // running nodes; any failure falls back to the in-browser console.
  async function launchExternalConsole(
    node: NodeData,
    mode: 'primary' | 'second' = 'primary'
  ): Promise<boolean> {
    try {
      const response = await apiRequest<ExternalConsoleInfo>(
        `/api/labs/${labId}/nodes/${node.id}/external-console${mode === 'second' ? '?mode=second' : ''}`,
        { suppressToast: true }
      );
      triggerExternalUrl(buildExternalConsoleUrl(response.data));
      return true;
    } catch (error) {
      const reason = error instanceof ApiError ? error.message : 'External console is unavailable.';
      toastStore.push(`${reason} Opening the in-browser console instead.`, 'info');
      return false;
    }
  }

  function triggerExternalUrl(url: string) {
    // Navigating the page itself would be heavier than a synthetic anchor
    // click; the browser shows its own first-click "Open app?" prompt.
    if (typeof document === 'undefined') return;
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.style.display = 'none';
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  }

  async function openConsole(node: NodeData, mode: 'primary' | 'second' = 'primary') {
    if (node.status !== 2 || node.transientStatus) {
      toastStore.push('Start the node before opening the HTML5 console.');
      return;
    }

    if ($externalConsoleStore === 'on') {
      if (await launchExternalConsole(node, mode)) {
        return;
      }
      // External launch failed — fall through to the embedded console.
    }

    showConsolesInZen = zenMode;
    const existingTab = consoleWorkspace?.tabs.find(
      (tab) => tab.nodeId === node.id && (tab.mode ?? 'primary') === mode
    );
    if (existingTab) {
      if (consoleWorkspace?.mode === 'floating') {
        updateFloatingWindow(existingTab.id, { minimized: false });
        bringFloatingWindowToFront(existingTab.id);
      } else {
        replaceConsoleWindow((workspace) => ({ ...workspace, minimized: false, activeTabId: existingTab.id }));
      }
      await tick();
      focusGuacamoleWindow(existingTab.id);
      return;
    }

    await tick();
    const nextWorkspace = buildConsoleWorkspace(consoleWorkspace ?? undefined);
    const offsetIndex = nextWorkspace.tabs.length;
    const nextTab: ConsoleTab = {
      id: nextConsoleTabId(),
      nodeId: node.id,
      nodeName: mode === 'second' ? `${node.name} (second)` : node.name,
      mode,
      src: `/api/labs/${labId}/nodes/${node.id}/html5?ts=${Date.now()}${mode === 'second' ? '&console=second' : ''}`,
      requestId: Date.now(),
      floating: defaultFloatingWindowBounds(offsetIndex)
    };
    nextWorkspace.tabs = [...nextWorkspace.tabs, nextTab];
    nextWorkspace.activeTabId = nextTab.id;
    if (nextWorkspace.mode === 'floating') {
      nextWorkspace.zStack = [...nextWorkspace.zStack, nextTab.id];
    }
    consoleWorkspace = nextWorkspace;
    void refreshRecordingState(nextTab);
    await tick();
    focusGuacamoleWindow(nextTab.id);
  }

  async function handleConsoleNodeChange(event: Event) {
    const selectedId = Number((event.currentTarget as HTMLSelectElement).value);
    const node = nodeList.find((candidate) => candidate.id === selectedId);
    if (node) {
      if ($externalConsoleStore !== 'on') {
        const existingTab = consoleWorkspace?.tabs.find((tab) => tab.nodeId === node.id);
        if (existingTab) {
          replaceConsoleWindow((workspace) => ({ ...workspace, minimized: false, activeTabId: existingTab.id }));
          await tick();
          focusGuacamoleWindow(existingTab.id);
          return;
        }
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
    const zStack = consoleWorkspace.zStack.filter((id) => id !== tabId);
    const activeTabId =
      tabId === consoleWorkspace.activeTabId ? tabs[tabs.length - 1].id : consoleWorkspace.activeTabId;
    consoleWorkspace = { ...consoleWorkspace, tabs, zStack, activeTabId };
  }

  async function reloadConsole() {
    const activeTab = activeConsoleTab();
    if (!activeTab || !consoleWorkspace) return;
    // A reload opens a fresh console session, which starts unrecorded.
    recordingStates = { ...recordingStates, [activeTab.id]: false };
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
      originY: consoleWorkspace.y,
      windowId: null
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
      height: consoleWorkspace.height,
      windowId: null
    };
  }

  function beginFloatingDrag(event: PointerEvent, tabId: number) {
    if (!consoleWorkspace) return;
    const tab = consoleWorkspace.tabs.find((candidate) => candidate.id === tabId);
    if (!tab || tab.floating.maximized) return;
    if (event.button !== 0) return;
    const target = event.currentTarget as HTMLElement | null;
    target?.setPointerCapture?.(event.pointerId);
    event.preventDefault();
    bringFloatingWindowToFront(tabId);
    setTimeout(() => focusGuacamoleWindow(tabId), 0);
    dragState = {
      startX: event.clientX,
      startY: event.clientY,
      originX: tab.floating.x,
      originY: tab.floating.y,
      windowId: tabId
    };
  }

  function beginFloatingResize(event: PointerEvent, tabId: number) {
    if (!consoleWorkspace) return;
    const tab = consoleWorkspace.tabs.find((candidate) => candidate.id === tabId);
    if (!tab || tab.floating.maximized || tab.floating.minimized) return;
    if (event.button !== 0) return;
    event.stopPropagation();
    const target = event.currentTarget as HTMLElement | null;
    target?.setPointerCapture?.(event.pointerId);
    event.preventDefault();
    bringFloatingWindowToFront(tabId);
    resizeState = {
      startX: event.clientX,
      startY: event.clientY,
      width: tab.floating.width,
      height: tab.floating.height,
      windowId: tabId
    };
  }

  function handleWindowPointerMove(event: PointerEvent) {
    if (dragState && consoleWorkspace) {
      const drag = dragState;
      if (drag.windowId == null) {
        consoleWorkspace = {
          ...consoleWorkspace,
          ...clampConsoleBounds({
            x: drag.originX + (event.clientX - drag.startX),
            y: drag.originY + (event.clientY - drag.startY),
            width: consoleWorkspace.width,
            height: consoleWorkspace.height
          })
        };
      } else {
        const tab = consoleWorkspace.tabs.find((candidate) => candidate.id === drag.windowId);
        if (tab) {
          const next = clampConsoleBounds({
            x: drag.originX + (event.clientX - drag.startX),
            y: drag.originY + (event.clientY - drag.startY),
            width: tab.floating.width,
            height: tab.floating.height
          });
          updateFloatingWindow(drag.windowId, next);
        }
      }
    }

    if (resizeState && consoleWorkspace) {
      const resize = resizeState;
      if (resize.windowId == null) {
        consoleWorkspace = {
          ...consoleWorkspace,
          ...clampConsoleBounds({
            x: consoleWorkspace.x,
            y: consoleWorkspace.y,
            width: resize.width + (event.clientX - resize.startX),
            height: resize.height + (event.clientY - resize.startY)
          })
        };
      } else {
        const tab = consoleWorkspace.tabs.find((candidate) => candidate.id === resize.windowId);
        if (tab) {
          const next = clampConsoleBounds({
            x: tab.floating.x,
            y: tab.floating.y,
            width: resize.width + (event.clientX - resize.startX),
            height: resize.height + (event.clientY - resize.startY)
          });
          updateFloatingWindow(resize.windowId, next);
        }
      }
    }
  }

  function stopWindowPointerTracking() {
    dragState = null;
    resizeState = null;
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
    <header id="lab-editor-top-bar" hidden={zenMode} class="shrink-0 px-3 pt-2">
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
              <h1 class="truncate text-lg font-semibold tracking-tight text-gray-100">{$labState.meta?.name || labId}</h1>
              {#if consoleMode === 'tabbed'}
                <span class={chromePillClass}>{consoleTabCount} consoles</span>
              {/if}
            </div>
            <div class="mt-1 truncate font-mono text-[11px] text-gray-400">{$labState.meta?.path || labId}</div>
          </div>
        </div>

        <div class="ml-auto hidden shrink-0 grid-cols-2 gap-1.5 self-center sm:grid">
          <span class="inline-flex items-center gap-1.5 rounded-md border border-gray-800 bg-gray-950/70 px-2 py-1 text-[10px] tracking-tight text-gray-300">
            <Server class="h-3 w-3 text-gray-400" aria-hidden="true" />
            <span class="text-gray-500">Nodes:</span>
            <span class="font-mono text-gray-100">{nodeList.length}</span>
          </span>
          <span
            class={`inline-flex items-center gap-1.5 rounded-md border bg-gray-950/70 px-2 py-1 text-[10px] tracking-tight ${
              runningNodeCount ? 'border-emerald-500/40 text-emerald-100' : 'border-gray-800 text-gray-300'
            }`}
          >
            <Activity
              class={`h-3 w-3 ${runningNodeCount ? 'text-emerald-300' : 'text-gray-400'}`}
              aria-hidden="true"
            />
            <span class="text-gray-500">Running:</span>
            <span class="font-mono">{runningNodeCount}</span>
          </span>
          <span class="inline-flex items-center gap-1.5 rounded-md border border-gray-800 bg-gray-950/70 px-2 py-1 text-[10px] tracking-tight text-gray-300">
            <NetworkIcon class="h-3 w-3 text-gray-400" aria-hidden="true" />
            <span class="text-gray-500">Networks:</span>
            <span class="font-mono text-gray-100">{networkList.length}</span>
          </span>
          <span class="inline-flex items-center gap-1.5 rounded-md border border-gray-800 bg-gray-950/70 px-2 py-1 text-[10px] tracking-tight text-gray-300">
            <Cpu class="h-3 w-3 text-gray-400" aria-hidden="true" />
            <span class="text-gray-500">vCPU:</span>
            <span class="font-mono text-gray-100">{totalVCpu}</span>
          </span>
        </div>

        <button
          type="button"
          data-testid="open-recordings"
          aria-label="Lab recordings"
          title="Lab recordings"
          class={`${compactActionButtonClass} shrink-0 gap-1.5 self-center`}
          on:click={() => (recordingsTarget = { nodeId: null })}
        >
          <ListVideo class="h-4 w-4" aria-hidden="true" />
          <span class="hidden md:inline">Recordings</span>
        </button>
        <div class="hidden shrink-0 items-center gap-2 md:flex">
          <div class="flex flex-col gap-1 rounded-xl border border-gray-800 bg-gray-950/70 px-3 py-2">
            <div class="text-[10px] uppercase tracking-[0.05em] text-gray-500">External console</div>
            <button
              type="button"
              data-testid="external-console-toggle"
              aria-pressed={$externalConsoleStore === 'on'}
              title="Launch consoles in desktop apps via telnet://, vnc:// or rdp:// links. The OS needs a client registered for the scheme; the first click shows the browser's allow prompt."
              class={`inline-flex items-center justify-center gap-1.5 rounded-md border px-2 py-0.5 text-[10px] uppercase tracking-[0.18em] transition ${
                $externalConsoleStore === 'on'
                  ? 'border-blue-500/60 bg-blue-500/20 text-white'
                  : 'border-gray-800 bg-gray-900/80 text-gray-400 hover:text-white'
              }`}
              on:click={() => externalConsoleStore.toggle()}
            >
              <ExternalLink class="h-3 w-3" aria-hidden="true" />
              External
            </button>
          </div>
          <div class="flex flex-col gap-1 rounded-xl border border-gray-800 bg-gray-950/70 px-3 py-2">
            <div class="text-[10px] uppercase tracking-[0.05em] text-gray-500">Console layout</div>
            <div class="inline-flex items-center rounded-md border border-gray-800 bg-gray-900/80 p-0.5">
              <button
                type="button"
                class={`rounded px-2 py-0.5 text-[10px] uppercase tracking-[0.18em] transition ${consoleMode === 'tabbed' ? 'bg-blue-500/20 text-white' : 'text-gray-400 hover:text-white'}`}
                aria-pressed={consoleMode === 'tabbed'}
                on:click={() => setConsoleMode('tabbed')}
              >
                Tabbed
              </button>
              <button
                type="button"
                class={`rounded px-2 py-0.5 text-[10px] uppercase tracking-[0.18em] transition ${consoleMode === 'floating' ? 'bg-blue-500/20 text-white' : 'text-gray-400 hover:text-white'}`}
                aria-pressed={consoleMode === 'floating'}
                on:click={() => setConsoleMode('floating')}
              >
                Floating
              </button>
            </div>
          </div>
          <div class="rounded-xl border border-gray-800 bg-gray-950/70 px-3 py-2 text-right">
            <div class="text-[10px] uppercase tracking-[0.05em] text-gray-500">Operator</div>
            <div class="mt-1 text-[11px] text-gray-200">{currentUserLabel}</div>
          </div>
        </div>
      </div>
    </header>
    <div class="flex shrink-0 justify-center">
      <button
        type="button"
        class="flex h-5 w-9 items-center justify-center rounded text-gray-400 hover:bg-gray-800 hover:text-white focus-visible:outline focus-visible:outline-1 focus-visible:outline-blue-400"
        aria-label={zenMode ? 'Exit Zen mode' : 'Enter Zen mode'}
        aria-controls="lab-editor-top-bar"
        aria-expanded={!zenMode}
        title={zenMode ? 'Exit Zen mode' : 'Enter Zen mode'}
        on:mousedown={(event) => event.stopPropagation()}
        on:click={toggleZenMode}
      >
        {#if zenMode}
          <ChevronDown class="h-4 w-4" aria-hidden="true" />
        {:else}
          <ChevronUp class="h-4 w-4" aria-hidden="true" />
        {/if}
      </button>
    </div>
  {/if}

  {#if loading}
    <div class="flex flex-1 p-3">
      <div class="animate-pulse flex-1 rounded-2xl border border-gray-800 bg-gray-900/70 p-4">
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
    <div class="relative flex flex-1 overflow-hidden px-3 pb-3">
      <div hidden={zenMode} class:contents={!zenMode}>
        <InfoPanelStack nodesById={$labState.nodes} networksById={$labState.networks} />
      </div>
      <div bind:this={canvasContainerEl} class="min-w-0 flex-1 overflow-hidden rounded-2xl border border-gray-800 bg-gray-900/70">
        <SvelteFlowProvider>
          <TopologyCanvas
            bind:this={canvasRef}
            labId={labId}
            {zenMode}
            {consoleSelectorWindows}
            {consoleMinimizedWindows}
            on:console={(event) => openConsole(event.detail.node, event.detail.mode ?? 'primary')}
            on:recordings={({ detail: { node } }) => (recordingsTarget = { nodeId: node.id, nodeName: node.name })}
            on:console-select={(event) => handleConsoleRestore(event.detail.tabId)}
            on:console-restore={(event) => handleConsoleRestore(event.detail.tabId)}
            on:console-close={(event) => handleConsoleClose(event.detail.tabId)}
            on:console-maximize={(event) => handleConsoleMaximizeFromBar(event.detail.tabId)}
            on:changed={refreshLabFromCanvas}
            on:link-change={handleLinkChange}
          />
        </SvelteFlowProvider>
      </div>

      {#if consoleWorkspace && consoleWorkspace.mode === 'tabbed'}
        <div
          role="dialog"
          tabindex="-1"
          aria-label="Console workspace"
          class="pointer-events-auto fixed overflow-hidden rounded-2xl border border-gray-700 bg-gray-900/95 shadow-2xl"
          style={`left: ${consoleWorkspace.x}px; top: ${consoleWorkspace.y}px; width: ${consoleWorkspace.width}px; height: ${consoleWorkspace.height}px; z-index: 50; display: ${consoleWorkspace.minimized ? 'none' : 'flex'};`}
          style:visibility={hideConsoles ? 'hidden' : null}
          inert={hideConsoles}
          on:pointerdown={focusConsoleWorkspace}
        >
          <div class="flex h-full w-full flex-col">
            <div
              role="toolbar"
              aria-label="Console workspace controls"
              tabindex="-1"
              class={`flex items-center justify-between gap-3 border-b border-gray-800 bg-gray-900/95 px-3 py-1 backdrop-blur ${consoleWorkspace.maximized ? 'cursor-default' : 'cursor-move'}`}
              on:pointerdown={beginConsoleDrag}
            >
              <div class="flex items-center gap-2.5">
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
                <div class="flex items-baseline gap-2">
                  <span class="text-[9px] uppercase tracking-[0.05em] text-gray-500">Console</span>
                  <span class="whitespace-nowrap text-sm font-semibold leading-none text-gray-100">{activeConsoleTabState?.nodeName || 'No console selected'}</span>
                </div>
              </div>
              <div class="flex items-center gap-2">
                {#if activeConsoleTabState && isRecordedTab(activeConsoleTabState)}
                  <RecordButton
                    recording={!!recordingStates[activeConsoleTabState.id]}
                    onToggle={() => activeConsoleTabState && toggleRecording(activeConsoleTabState)}
                  />
                {/if}
                {#if activeConsoleTabState}
                  {@const tab = activeConsoleTabState}
                  <button
                    type="button"
                    aria-label={`Recordings for ${tab.nodeName}`}
                    title="Console recordings"
                    class={compactIconButtonClass}
                    on:pointerdown|stopPropagation
                    on:click={() => (recordingsTarget = { nodeId: tab.nodeId, nodeName: tab.nodeName })}
                  ><ListVideo class="h-3.5 w-3.5" aria-hidden="true" /></button>
                {/if}
                <label class="sr-only" for="console-node-select">Console target</label>
                <select
                  id="console-node-select"
                  class="rounded-md border border-gray-700 bg-gray-950 px-2 py-[3px] text-[10px] uppercase tracking-[0.18em] text-gray-300 outline-none hover:border-blue-500"
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
                  type="button"
                  aria-label="Reload console"
                  title="Reload console"
                  class="inline-flex items-center justify-center rounded-md border border-gray-700 bg-gray-950 px-1.5 py-[3px] text-gray-300 hover:border-blue-500 hover:text-white disabled:cursor-not-allowed disabled:opacity-40"
                  on:pointerdown|stopPropagation
                  on:click={reloadConsole}
                  disabled={activeConsoleTabState == null}
                >
                  <RefreshCw class="h-3.5 w-3.5" />
                </button>
              </div>
            </div>
            <div class="flex min-h-0 flex-1 flex-col bg-gray-950">
              <div class="flex items-center gap-1 border-b border-gray-800 bg-gray-950/90 px-3 py-1">
                {#each consoleWorkspace.tabs as tab}
                  <div class={`flex items-center gap-0.5 rounded-md border ${tab.id === consoleWorkspace.activeTabId ? 'border-blue-500 bg-blue-500/10 text-white' : 'border-gray-700 bg-gray-900 text-gray-300 hover:border-blue-500 hover:text-white'}`}>
                    <button
                      type="button"
                      class="px-2 py-0.5 text-[10px] uppercase tracking-[0.18em]"
                      on:pointerdown|stopPropagation
                      on:click={() => selectConsoleTab(tab.id)}
                    >
                      {#if recordingStates[tab.id]}
                        <span
                          class="mr-1 inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-red-500 align-middle"
                          title="Recording in progress — press the record button in the console window to stop"
                        ></span>
                      {/if}
                      {tab.nodeName}
                    </button>
                    <button
                      type="button"
                      class="pr-1.5 text-gray-500 hover:text-red-400"
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
          </div>
          {#if !consoleWorkspace.maximized && !consoleWorkspace.minimized}
            <button
              class="absolute bottom-0 right-0 h-5 w-5 cursor-nwse-resize bg-transparent"
              aria-label="Resize console workspace"
              on:pointerdown={beginConsoleResize}
            ></button>
          {/if}
        </div>
      {:else if consoleWorkspace && consoleWorkspace.mode === 'floating'}
        {#each consoleWorkspace.tabs as tab (tab.id)}
          {@const stackIndex = consoleWorkspace.zStack.indexOf(tab.id)}
          {@const zIndex = 50 + (stackIndex < 0 ? 0 : stackIndex)}
          <div
            role="dialog"
            tabindex="-1"
            aria-label={`Console window for ${tab.nodeName}`}
            class="pointer-events-auto fixed overflow-hidden rounded-2xl border border-gray-700 bg-gray-900/95 shadow-2xl"
            style={`left: ${tab.floating.x}px; top: ${tab.floating.y}px; width: ${tab.floating.width}px; height: ${tab.floating.height}px; z-index: ${zIndex}; display: ${tab.floating.minimized ? 'none' : 'flex'};`}
            style:visibility={hideConsoles ? 'hidden' : null}
            inert={hideConsoles}
            on:pointerdown={() => {
              bringFloatingWindowToFront(tab.id);
              setTimeout(() => focusGuacamoleWindow(tab.id), 0);
            }}
          >
            <div class="flex h-full w-full flex-col">
              <div
                role="toolbar"
                aria-label={`Controls for ${tab.nodeName} console`}
                tabindex="-1"
                class={`flex items-center justify-between gap-3 border-b border-gray-800 bg-gray-900/95 px-3 py-1 backdrop-blur ${tab.floating.maximized ? 'cursor-default' : 'cursor-move'}`}
                on:pointerdown={(event) => beginFloatingDrag(event, tab.id)}
              >
                <div class="flex items-center gap-2.5">
                  <div class="flex items-center gap-2">
                    <button
                      class="h-3 w-3 rounded-full bg-red-400 transition hover:bg-red-300"
                      aria-label={`Close ${tab.nodeName} console`}
                      on:pointerdown|stopPropagation
                      on:click={() => closeFloatingWindow(tab.id)}
                    ></button>
                    <button
                      class="h-3 w-3 rounded-full bg-amber-300 transition hover:bg-amber-200"
                      aria-label={tab.floating.minimized ? `Restore ${tab.nodeName} console` : `Minimize ${tab.nodeName} console`}
                      on:pointerdown|stopPropagation
                      on:click={() => toggleFloatingMinimize(tab.id)}
                    ></button>
                    <button
                      class="h-3 w-3 rounded-full bg-emerald-400 transition hover:bg-emerald-300"
                      aria-label={tab.floating.maximized ? `Restore ${tab.nodeName} console` : `Maximize ${tab.nodeName} console`}
                      on:pointerdown|stopPropagation
                      on:click={() => toggleFloatingMaximize(tab.id)}
                    ></button>
                  </div>
                  <div class="flex items-baseline gap-2">
                    <span class="text-[9px] uppercase tracking-[0.05em] text-gray-500">Console</span>
                    <span class="whitespace-nowrap text-sm font-semibold leading-none text-gray-100">{tab.nodeName}</span>
                  </div>
                </div>
                <div class="flex items-center gap-2">
                  {#if isRecordedTab(tab)}
                    <RecordButton
                      recording={!!recordingStates[tab.id]}
                      onToggle={() => toggleRecording(tab)}
                    />
                  {/if}
                  <button
                    type="button"
                    aria-label={`Recordings for ${tab.nodeName}`}
                    title="Console recordings"
                    class={compactIconButtonClass}
                    on:pointerdown|stopPropagation
                    on:click={() => (recordingsTarget = { nodeId: tab.nodeId, nodeName: tab.nodeName })}
                  ><ListVideo class="h-3.5 w-3.5" aria-hidden="true" /></button>
                  <button
                    type="button"
                    aria-label={`Reload ${tab.nodeName} console`}
                    title="Reload console"
                    class="inline-flex items-center justify-center rounded-md border border-gray-700 bg-gray-950 px-1.5 py-[3px] text-gray-300 hover:border-blue-500 hover:text-white"
                    on:pointerdown|stopPropagation
                    on:click={() => reloadFloatingConsole(tab.id)}
                  >
                    <RefreshCw class="h-3.5 w-3.5" />
                  </button>
                </div>
              </div>
              <div
                role="presentation"
                class="relative min-h-0 flex-1 border-t border-gray-950 bg-black"
                on:mouseenter={() => focusGuacamoleWindow(tab.id)}
              >
                <iframe
                  title={`Guacamole console for ${tab.nodeName}`}
                  src={tab.src}
                  data-console-tab-id={tab.id}
                  tabindex="-1"
                  class="h-full w-full border-0"
                  on:load={() => focusGuacamoleWindow(tab.id)}
                ></iframe>
              </div>
            </div>
            {#if !tab.floating.maximized && !tab.floating.minimized}
              <button
                class="absolute bottom-0 right-0 h-5 w-5 cursor-nwse-resize bg-transparent"
                aria-label={`Resize ${tab.nodeName} console`}
                on:pointerdown={(event) => beginFloatingResize(event, tab.id)}
              ></button>
            {/if}
          </div>
        {/each}
      {/if}
    </div>
  {/if}
</div>

{#if recordingsTarget}
  <RecordingsPanel {labId}
    labName={$labState.meta?.name || labId}
    {...recordingsTarget}
    onClose={() => (recordingsTarget = null)}
  />
{/if}

<style>
  .console-rail-label {
    writing-mode: vertical-rl;
    transform: rotate(180deg);
  }
</style>
