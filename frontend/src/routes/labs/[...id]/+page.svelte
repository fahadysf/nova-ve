<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { page } from '$app/stores';
  import { goto } from '$app/navigation';
  import { authStore } from '$lib/stores/auth';
  import { apiGetData, ApiError } from '$lib/api';
  import { toastStore } from '$lib/stores/toasts';
  import { SvelteFlowProvider } from '@xyflow/svelte';
  import TopologyCanvas from '$lib/components/canvas/TopologyCanvas.svelte';
  import type { LabMeta, NetworkData, NodeData, TopologyLink } from '$lib/types';

  let labId = '';
  let labMeta: LabMeta | null = null;
  let nodes: Record<string, NodeData> = {};
  let networks: Record<string, NetworkData> = {};
  let topology: TopologyLink[] = [];
  let loading = true;
  let error = '';
  type ConsoleBounds = { x: number; y: number; width: number; height: number };
  type ConsoleWindowState = ConsoleBounds & {
    id: number;
    nodeId: number;
    nodeName: string;
    src: string;
    requestId: number;
    maximized: boolean;
    minimized: boolean;
    restoreBounds: ConsoleBounds | null;
    zIndex: number;
  };

  const CONSOLE_HEADER_HEIGHT = 56;

  let consoleWindows: ConsoleWindowState[] = [];
  let consoleWindowCounter = 0;
  let summaryCollapsed = false;
  let inventoryCollapsed = false;
  let dragState:
    | { windowId: number; startX: number; startY: number; originX: number; originY: number }
    | null = null;
  let resizeState:
    | { windowId: number; startX: number; startY: number; width: number; height: number }
    | null = null;

  $: labId = $page.params.id ?? '';
  $: nodeList = Object.values(nodes).sort((a, b) => a.id - b.id);
  $: networkList = Object.values(networks).sort((a, b) => a.id - b.id);
  $: runningConsoleNodes = nodeList.filter((node) => node.status === 2);

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
      consoleWindows = consoleWindows
        .filter((window) => nodeData[String(window.nodeId)]?.status === 2)
        .map((window) => ({
          ...window,
          nodeName: nodeData[String(window.nodeId)]?.name || window.nodeName
        }));
    } catch (e) {
      error = e instanceof ApiError ? e.message : 'Unable to load the lab.';
    } finally {
      loading = false;
    }
  }

  function nextConsoleWindowId(): number {
    consoleWindowCounter += 1;
    return consoleWindowCounter;
  }

  function nextWindowZIndex(): number {
    return consoleWindows.reduce((highest, window) => Math.max(highest, window.zIndex), 39) + 1;
  }

  function defaultConsoleBounds(windowOffset = consoleWindows.length): ConsoleBounds {
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
    windowId: number,
    updater: (window: ConsoleWindowState) => ConsoleWindowState
  ) {
    consoleWindows = consoleWindows.map((window) => (window.id === windowId ? updater(window) : window));
  }

  function consoleIframe(windowId: number): HTMLIFrameElement | null {
    return document.querySelector(`iframe[data-console-window-id="${windowId}"]`);
  }

  function focusGuacamoleWindow(windowId: number, attempts = 12) {
    const iframe = consoleIframe(windowId);
    if (!iframe) {
      if (attempts > 0) {
        setTimeout(() => focusGuacamoleWindow(windowId, attempts - 1), 200);
      }
      return;
    }

    iframe.focus();
    try {
      iframe.contentWindow?.focus();
      const frameDocument = iframe.contentDocument;
      const keyboardTarget =
        (frameDocument?.querySelector('textarea.clipboard-service-target') as HTMLTextAreaElement | null) ??
        (frameDocument?.querySelector('textarea') as HTMLTextAreaElement | null);
      const canvas = frameDocument?.querySelector('canvas') as HTMLCanvasElement | null;

      if (keyboardTarget) {
        keyboardTarget.focus();
      } else {
        frameDocument?.body?.focus();
      }

      iframe.contentWindow?.localStorage?.removeItem('GUAC_AUTH_TOKEN');

      if (canvas) {
        const pointerEvent = { bubbles: true, cancelable: true, view: iframe.contentWindow };
        canvas.dispatchEvent(new MouseEvent('mousedown', pointerEvent));
        canvas.dispatchEvent(new MouseEvent('mouseup', pointerEvent));
        canvas.dispatchEvent(new MouseEvent('click', pointerEvent));
      }
    } catch (_error) {
      return;
    }

    if (attempts > 0) {
      setTimeout(() => focusGuacamoleWindow(windowId, attempts - 1), 200);
    }
  }

  function focusConsoleWindow(windowId: number) {
    const zIndex = nextWindowZIndex();
    replaceConsoleWindow(windowId, (window) => ({ ...window, zIndex }));
    setTimeout(() => focusGuacamoleWindow(windowId), 0);
  }

  function buildConsoleWindow(node: NodeData, existingWindow?: ConsoleWindowState): ConsoleWindowState {
    const priorBounds = existingWindow?.maximized
      ? maximizedConsoleBounds()
      : existingWindow ?? defaultConsoleBounds();
    const bounds = clampConsoleBounds({
      x: priorBounds.x,
      y: priorBounds.y,
      width: priorBounds.width,
      height: priorBounds.height
    });

    return {
      ...bounds,
      id: existingWindow?.id ?? nextConsoleWindowId(),
      nodeId: node.id,
      nodeName: node.name,
      src: `/api/labs/${labId}/nodes/${node.id}/html5?ts=${Date.now()}`,
      requestId: Date.now(),
      maximized: existingWindow?.maximized ?? false,
      minimized: false,
      restoreBounds: existingWindow?.restoreBounds ?? bounds,
      zIndex: nextWindowZIndex()
    };
  }

  async function openConsole(node: NodeData) {
    if (node.status !== 2) {
      toastStore.push('Start the node before opening the HTML5 console.');
      return;
    }

    const existingWindow = consoleWindows.find((window) => window.nodeId === node.id);
    if (existingWindow) {
      focusConsoleWindow(existingWindow.id);
      replaceConsoleWindow(existingWindow.id, (window) => ({ ...window, minimized: false }));
      return;
    }

    const nextWindow = buildConsoleWindow(node);
    await tick();
    consoleWindows = [...consoleWindows, nextWindow];
    focusGuacamoleWindow(nextWindow.id);
  }

  async function handleConsoleNodeChange(windowId: number, event: Event) {
    const selectedId = Number((event.currentTarget as HTMLSelectElement).value);
    const node = nodeList.find((candidate) => candidate.id === selectedId);
    if (node) {
      const existingTargetWindow = consoleWindows.find(
        (window) => window.nodeId === node.id && window.id !== windowId
      );
      if (existingTargetWindow) {
        focusConsoleWindow(existingTargetWindow.id);
        replaceConsoleWindow(existingTargetWindow.id, (window) => ({ ...window, minimized: false }));
        return;
      }
      const existingWindow = consoleWindows.find((window) => window.id === windowId);
      if (!existingWindow) return;
      const nextWindow = buildConsoleWindow(node, existingWindow);
      replaceConsoleWindow(windowId, () => nextWindow);
      setTimeout(() => focusGuacamoleWindow(windowId), 0);
    }
  }

  function closeConsole(windowId: number) {
    consoleWindows = consoleWindows.filter((window) => window.id !== windowId);
    if (dragState?.windowId === windowId) {
      dragState = null;
    }
    if (resizeState?.windowId === windowId) {
      resizeState = null;
    }
  }

  async function reloadConsole(windowId: number) {
    const targetWindow = consoleWindows.find((window) => window.id === windowId);
    if (!targetWindow) return;
    replaceConsoleWindow(windowId, (window) => ({
      ...window,
      minimized: false,
      src: `/api/labs/${labId}/nodes/${targetWindow.nodeId}/html5?ts=${Date.now()}`,
      requestId: Date.now(),
      zIndex: nextWindowZIndex()
    }));
    setTimeout(() => focusGuacamoleWindow(windowId), 0);
  }

  function toggleConsoleMaximize(windowId: number) {
    const targetWindow = consoleWindows.find((window) => window.id === windowId);
    if (!targetWindow) return;

    if (targetWindow.maximized) {
      const restored = clampConsoleBounds(targetWindow.restoreBounds ?? defaultConsoleBounds());
      replaceConsoleWindow(windowId, (window) => ({
        ...window,
        ...restored,
        maximized: false,
        minimized: false,
        restoreBounds: restored,
        zIndex: nextWindowZIndex()
      }));
      return;
    }

    const currentBounds = {
      x: targetWindow.x,
      y: targetWindow.y,
      width: targetWindow.width,
      height: targetWindow.height
    };
    replaceConsoleWindow(windowId, (window) => ({
      ...window,
      ...maximizedConsoleBounds(),
      maximized: true,
      minimized: false,
      restoreBounds: currentBounds,
      zIndex: nextWindowZIndex()
    }));
  }

  function toggleConsoleMinimize(windowId: number) {
    const targetWindow = consoleWindows.find((window) => window.id === windowId);
    if (!targetWindow) return;
    replaceConsoleWindow(windowId, (window) => ({
      ...window,
      minimized: !window.minimized,
      maximized: window.minimized ? window.maximized : false,
      zIndex: nextWindowZIndex()
    }));
  }

  function beginConsoleDrag(windowId: number, event: MouseEvent) {
    const targetWindow = consoleWindows.find((window) => window.id === windowId);
    if (!targetWindow || targetWindow.maximized) return;
    focusConsoleWindow(windowId);
    dragState = {
      windowId,
      startX: event.clientX,
      startY: event.clientY,
      originX: targetWindow.x,
      originY: targetWindow.y
    };
  }

  function beginConsoleResize(windowId: number, event: MouseEvent) {
    const targetWindow = consoleWindows.find((window) => window.id === windowId);
    if (!targetWindow || targetWindow.maximized || targetWindow.minimized) return;
    event.stopPropagation();
    focusConsoleWindow(windowId);
    resizeState = {
      windowId,
      startX: event.clientX,
      startY: event.clientY,
      width: targetWindow.width,
      height: targetWindow.height
    };
  }

  function handleWindowPointerMove(event: MouseEvent) {
    if (dragState) {
      const drag = dragState;
      replaceConsoleWindow(drag.windowId, (window) => ({
        ...window,
        ...clampConsoleBounds({
          x: drag.originX + (event.clientX - drag.startX),
          y: drag.originY + (event.clientY - drag.startY),
          width: window.width,
          height: window.height
        })
      }));
    }

    if (resizeState) {
      const resize = resizeState;
      replaceConsoleWindow(resize.windowId, (window) => ({
        ...window,
        ...clampConsoleBounds({
          x: window.x,
          y: window.y,
          width: resize.width + (event.clientX - resize.startX),
          height: resize.height + (event.clientY - resize.startY)
        })
      }));
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

      {#each [...consoleWindows].sort((left, right) => left.zIndex - right.zIndex) as consoleWindow (consoleWindow.id)}
        <div
          role="dialog"
          tabindex="-1"
          aria-label={`Console window for ${consoleWindow.nodeName}`}
          class="pointer-events-auto fixed flex overflow-hidden rounded-2xl border border-gray-700 bg-gray-900/95 shadow-2xl"
          style={`left: ${consoleWindow.x}px; top: ${consoleWindow.y}px; width: ${consoleWindow.width}px; height: ${consoleWindow.minimized ? CONSOLE_HEADER_HEIGHT : consoleWindow.height}px; z-index: ${consoleWindow.zIndex};`}
          on:mousedown={() => focusConsoleWindow(consoleWindow.id)}
        >
          <div class="flex h-full w-full flex-col">
            <div
              role="toolbar"
              aria-label={`Console window controls for ${consoleWindow.nodeName}`}
              tabindex="-1"
              class={`flex items-center justify-between border-b border-gray-800 px-3 py-2 ${consoleWindow.maximized ? 'cursor-default' : 'cursor-move'}`}
              on:mousedown={(event) => beginConsoleDrag(consoleWindow.id, event)}
            >
              <div class="flex items-center gap-3">
                <div class="flex items-center gap-2">
                  <button
                    class="h-3 w-3 rounded-full bg-red-400 transition hover:bg-red-300"
                    aria-label={`Close ${consoleWindow.nodeName}`}
                    on:click={() => closeConsole(consoleWindow.id)}
                  ></button>
                  <button
                    class="h-3 w-3 rounded-full bg-amber-300 transition hover:bg-amber-200"
                    aria-label={`${consoleWindow.minimized ? 'Restore' : 'Minimize'} ${consoleWindow.nodeName}`}
                    on:click={() => toggleConsoleMinimize(consoleWindow.id)}
                  ></button>
                  <button
                    class="h-3 w-3 rounded-full bg-emerald-400 transition hover:bg-emerald-300"
                    aria-label={`${consoleWindow.maximized ? 'Restore' : 'Maximize'} ${consoleWindow.nodeName}`}
                    on:click={() => toggleConsoleMaximize(consoleWindow.id)}
                  ></button>
                </div>
                <div>
                  <div class="text-[9px] uppercase tracking-[0.24em] text-gray-500">HTML5 Console</div>
                  <div class="mt-0.5 text-xs font-medium text-gray-100">{consoleWindow.nodeName}</div>
                </div>
              </div>
              <div class="flex items-center gap-2">
                <label class="sr-only" for={`console-node-select-${consoleWindow.id}`}>Console target</label>
                <select
                  id={`console-node-select-${consoleWindow.id}`}
                  class="rounded-md border border-gray-700 bg-gray-900 px-2 py-1.5 text-[10px] uppercase tracking-[0.18em] text-gray-300 outline-none hover:border-blue-500"
                  value={consoleWindow.nodeId}
                  on:change={(event) => handleConsoleNodeChange(consoleWindow.id, event)}
                >
                  {#each runningConsoleNodes as node}
                    <option value={node.id}>{node.name}</option>
                  {/each}
                </select>
                <button
                  class="rounded-md border border-gray-700 px-2.5 py-1.5 text-[10px] uppercase tracking-[0.18em] text-gray-300 hover:border-blue-500 hover:text-white"
                  on:click={() => reloadConsole(consoleWindow.id)}
                >
                  Reload
                </button>
              </div>
            </div>
            {#if !consoleWindow.minimized}
              <div class="min-h-0 flex-1 bg-gray-950">
                {#key consoleWindow.requestId}
                  <iframe
                    title={`Guacamole console for ${consoleWindow.nodeName}`}
                    src={consoleWindow.src}
                    data-console-window-id={consoleWindow.id}
                    class="h-full w-full border-0"
                    on:load={() => focusGuacamoleWindow(consoleWindow.id)}
                  ></iframe>
                {/key}
              </div>
            {/if}
          </div>
          {#if !consoleWindow.maximized && !consoleWindow.minimized}
            <button
              class="absolute bottom-0 right-0 h-5 w-5 cursor-nwse-resize bg-transparent"
              aria-label="Resize console window"
              on:mousedown={(event) => beginConsoleResize(consoleWindow.id, event)}
            ></button>
          {/if}
        </div>
      {/each}
    </div>
  {/if}
</div>

<style>
  .console-rail-label {
    writing-mode: vertical-rl;
    transform: rotate(180deg);
  }
</style>
