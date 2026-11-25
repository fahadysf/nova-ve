<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  import { createEventDispatcher, onDestroy, setContext } from 'svelte';
  import { onMount } from 'svelte';
  import { writable, get } from 'svelte/store';
  import {
    Check,
    ChevronLeft,
    Eraser,
    Info,
    ListVideo,
    LocateFixed,
    LoaderCircle,
    Lock,
    LockOpen,
    Map as MapIcon,
    Minus,
    Monitor,
    Pencil,
    Settings2,
    Play,
    Plus,
    RotateCcw,
    Share2,
    Square,
    Trash2,
    ZoomIn,
    ZoomOut,
  } from 'lucide-svelte';
  import {
    Background,
    MiniMap,
    Panel,
    SvelteFlow,
    useSvelteFlow,
    type Edge,
    type Node
  } from '@xyflow/svelte';
  import '@xyflow/svelte/dist/style.css';
  import { apiRequest, ApiError } from '$lib/api';
  import { anchoredMenu } from '$lib/services/menuPlacement';
  import { nodeIconSources } from '$lib/services/nodeIcons';
  import CustomNode from '$lib/components/canvas/CustomNode.svelte';
  import NodeConfigModal from '$lib/components/canvas/NodeConfigModal.svelte';
  import NetworkNode from '$lib/components/canvas/NetworkNode.svelte';
  import LinkEdge from '$lib/components/canvas/LinkEdge.svelte';
  import LinkPreview from '$lib/components/canvas/LinkPreview.svelte';
  import LinkConfirmModal from '$lib/components/canvas/LinkConfirmModal.svelte';
  import NetworkConfigModal from '$lib/components/canvas/NetworkConfigModal.svelte';
  import PortInfoPopover from '$lib/components/canvas/PortInfoPopover.svelte';
  import { selectedPortInfoStore, type SelectedPortInfo } from '$lib/stores/portInfo';
  import { canvasPrefs } from '$lib/stores/canvasPrefs';
  import { infoPanels } from '$lib/stores/infoPanels';
  import { networkSharing, type NetworkModalType } from '$lib/services/networkSharing';
  import { toastStore } from '$lib/stores/toasts';
  import {
    createLayoutDebouncer,
    LayoutConflictError,
    type LayoutDebouncer,
  } from '$lib/services/labApi';
  import {
    bestRouteAwarePortPosition,
    nudgeAwayFromPorts,
    placeInterfaces,
    separatePortPositions,
    type NodeBox,
  } from '$lib/services/portLayout';
  import type { Obstacle } from '$lib/services/router';
  import { createWsClient, type WsClient, type WsMessage } from '$lib/services/wsClient';
  import { labState } from '$lib/stores/labState';
  import { createLabWsStores, type LabWsStores, type LinkChangeEvent } from '$lib/stores/labWs';
  import { deriveEdges, attachRouteObstacles, parseIfaceInterfaceIndex, slotPosition, type DiscoveredLink } from '$lib/services/canvasEdges';
  import { createdFromLinkResponse, type LinkCreateResponse } from '$lib/services/linkCreate';
  import {
    dragLinkStore,
    getDragLinkSnapshot,
    type DragEndpoint,
    type DragLinkSnapshot,
  } from '$lib/stores/dragLink';
  import type {
    LabDefaults,
    LabViewport,
    Link,
    LinkEndpointPositions,
    LinkReconciliation,
    LinkStyle,
    LiveMacState,
    NetworkCreateConfig,
    NetworkData,
    NetworkType,
    NodeBatchCreateResult,
    NodeCatalog,
    NodeData,
    PortPosition,
    TemplateCapabilities,
    TopologyLink
  } from '$lib/types';

  export let labId: string;
  export let zenMode = false;
  /** v2 links[] — the source of truth for edges (US-082). */
  /** v2 lab defaults (notably ``link_style`` for edge routing). */
  export let consoleSelectorWindows: ConsoleSelectorWindow[] = [];
  export let consoleMinimizedWindows: ConsoleMinimizedBar[] = [];

  type PaletteNetworkItem = {
    kind: 'network';
    title: string;
    subtitle: string;
    networkType: NetworkModalType;
  };
  type PaletteItem = PaletteNetworkItem;

  // Single source of truth for the network palette. Add new entries here as
  // more network types ship; ``networkType`` is the wire value posted to
  // ``POST /api/labs/{path}/networks`` and must match ``NetworkType``, except
  // ``shared_network`` (#319), which posts the type of the network it joins.
  const NETWORK_PALETTE: PaletteNetworkItem[] = [
    { kind: 'network', title: 'Bridge Network', subtitle: 'linux_bridge', networkType: 'linux_bridge' },
    { kind: 'network', title: 'NAT-Cloud', subtitle: 'DHCP + outbound NAT', networkType: 'nat_cloud' },
    { kind: 'network', title: 'Bridge-Cloud', subtitle: 'Transparent host bridge', networkType: 'bridge_cloud' },
    { kind: 'network', title: 'Shared network', subtitle: 'Offered by another lab', networkType: 'shared_network' }
  ];
  type ConsoleSelectorWindow = {
    id: number;
    nodeName: string;
    isFront: boolean;
    isMinimized: boolean;
  };
  type ConsoleMinimizedBar = {
    id: number;
    nodeName: string;
  };
  type LinkEndpointKey = 'from' | 'to';
  type ConnectionPointRef = { linkId: string; endpointKey: LinkEndpointKey };
  type LabNodeAction = 'start' | 'stop';
  type NodeLifecycleAction = LabNodeAction | 'wipe';
  type ConnectionPointTarget =
    | {
        kind: 'node';
        nodeId: number;
        interfaceIndex: number;
        linkId?: string;
        endpointKey?: LinkEndpointKey;
      }
    | { kind: 'network'; networkId: number; linkId: string; endpointKey?: LinkEndpointKey };

  const dispatch = createEventDispatcher<{
    console: { nodeId: number; node: NodeData; mode: 'primary' | 'second' };
    recordings: { node: NodeData };
    'console-select': { tabId: number };
    'console-restore': { tabId: number };
    'console-close': { tabId: number };
    'console-maximize': { tabId: number };
    changed: {
      reason: string;
      nodeId?: number;
      node?: NodeData;
      nodes?: Record<string, NodeData>;
      networks?: Record<string, NetworkData>;
      topology?: TopologyLink[];
    };
    // #153: per-link mutation arriving via WS so the page can splice
    // links[] without a full loadLab(). Cascade deletes (e.g. orphan
    // implicit-bridge halves) deliver one event per link_id.
    'link-change': LinkChangeEvent;
  }>();

  const nodeTypes = {
    device: CustomNode,
    network: NetworkNode
  } as unknown as Record<string, never>;
  const edgeTypes = {
    link: LinkEdge
  } as unknown as Record<string, never>;
  const { fitView, screenToFlowPosition, zoomIn, zoomOut } = useSvelteFlow();

  // #248: every mutating endpoint response carries the post-write lab
  // revision; adopting it keeps the If-Match token fresh without a reload,
  // so single-session edits no longer spuriously 412 on the next write.
  function adoptRevision(response: unknown): void {
    const revision = (response as { revision?: unknown } | null)?.revision;
    if (typeof revision === 'number') {
      labState.setRevision(revision);
    }
  }

  const layoutDebouncer: LayoutDebouncer = createLayoutDebouncer(labId, {
    delayMs: 500,
    // #245: guard writes with the store's revision; the response carries the
    // post-write revision so the next write is checked against fresh state.
    versionProvider: () => $labState.revision,
    onApplied: (result) => {
      adoptRevision(result);
    },
    onError: (err) => {
      if (err instanceof LayoutConflictError) {
        // The payload requeued; the page re-adopts the server snapshot
        // (keeping our pending positions) and retries the flush with the
        // fresh revision via the 'layout-conflict' handler.
        toastStore.push('Lab changed in another session — reloading latest state.', 'error');
        dispatchCanvasChange('layout-conflict');
        return;
      }
      const message = err instanceof Error ? err.message : 'Layout save failed';
      toastStore.push(message, 'error');
    },
  });
  let dragLinkSourceAnchor: { x: number; y: number } | null = null;
  const defaultTemplateCapabilities: TemplateCapabilities = {
    hotplug: true,
    max_nics: 8,
    machine: 'q35',
  };
  const NODE_CARD_HEIGHT = 58;
  const NETWORK_CARD_HEIGHT = 50;
  let topologyChangeBanner:
    | {
        message: string;
        nodeNames: string[];
      }
    | null = null;
  let topologyChangeBannerTimer: ReturnType<typeof setTimeout> | null = null;

  $: selectedPortInfo = $selectedPortInfoStore;

  function resolveInterfacePeerLabel(nodeId: number, interfaceIndex: number): string | null {
    for (const link of $labState.links) {
      const fromMatches =
        link.from?.node_id === nodeId && link.from?.interface_index === interfaceIndex;
      const toMatches =
        link.to?.node_id === nodeId && link.to?.interface_index === interfaceIndex;
      if (!fromMatches && !toMatches) continue;
      const other = fromMatches ? link.to : link.from;
      if (!other) continue;
      if (typeof other.network_id === 'number') {
        const network = $labState.networks[String(other.network_id)];
        return network?.name ?? `network ${other.network_id}`;
      }
      if (typeof other.node_id === 'number') {
        const peerNode = $labState.nodes[String(other.node_id)];
        if (!peerNode) return `node ${other.node_id}`;
        const peerIface =
          typeof other.interface_index === 'number'
            ? peerNode.interfaces[other.interface_index]
            : undefined;
        if (peerIface) return `${peerNode.name} · ${peerIface.name}`;
        return peerNode.name;
      }
    }
    return null;
  }

  function resolveNetworkPeerLabel(networkId: number): string | null {
    const peers: string[] = [];
    for (const link of $labState.links) {
      const fromMatches = link.from?.network_id === networkId;
      const toMatches = link.to?.network_id === networkId;
      if (!fromMatches && !toMatches) continue;
      const other = fromMatches ? link.to : link.from;
      if (!other) continue;
      if (typeof other.node_id === 'number') {
        const peerNode = $labState.nodes[String(other.node_id)];
        const nodeLabel = peerNode?.name ?? `node ${other.node_id}`;
        const peerIface =
          peerNode && typeof other.interface_index === 'number'
            ? peerNode.interfaces[other.interface_index]
            : undefined;
        peers.push(peerIface ? `${nodeLabel} · ${peerIface.name}` : nodeLabel);
      } else if (typeof other.network_id === 'number') {
        const network = $labState.networks[String(other.network_id)];
        peers.push(network?.name ?? `network ${other.network_id}`);
      }
    }
    if (peers.length === 0) return null;
    if (peers.length === 1) return peers[0];
    return `${peers.length} peers: ${peers.join(', ')}`;
  }

  function getLiveMacFor(nodeId: number, interfaceIndex: number): LiveMacState | null {
    if (!labWsStores) return null;
    const key = `${nodeId}:${interfaceIndex}`;
    return get(labWsStores.liveMacs)[key] ?? null;
  }

  function closePortInfo() {
    selectedPortInfoStore.set(null);
  }

  $: {
    const snapshot = $dragLinkStore;
    if (snapshot.state === 'idle') {
      dragLinkSourceAnchor = null;
    } else if (snapshot.state === 'port_pressed' && snapshot.pointer) {
      dragLinkSourceAnchor = { x: snapshot.pointer.x, y: snapshot.pointer.y };
    }
  }

  $: {
    const snapshot = $dragLinkStore;
    if (snapshot.state === 'confirming') {
      gateLinkConfirmIfNeeded(snapshot);
    }
  }

  const nodesStore = writable<Node[]>([]);
  const edgesStore = writable<Edge[]>([]);

  let saveTimer: ReturnType<typeof setTimeout> | null = null;
  let saveState: 'idle' | 'saving' | 'saved' = 'idle';
  let selectedEdgeId: string | null = null;
  let paletteLoading = true;
  let paletteItems: PaletteItem[] = [];
  let networkPaletteItems: PaletteNetworkItem[] = [];
  let addMenuStep: 'closed' | 'kind' | 'item' = 'closed';
  let addMenuKind: 'network' | null = null;
  let canvasLocked = false;
  let nodeCatalog: NodeCatalog | null = null;
  let nodeModalOpen = false;
  let nodeModalMode: 'create' | 'edit' = 'create';
  let nodeModalNode: NodeData | null = null;
  let nodeModalSubmitting = false;
  let nodeCreateAnchor = { x: 200, y: 200 };

  let networkModalOpen = false;
  let networkModalDefaultName = '';
  let networkModalDefaultType: NetworkModalType = 'linux_bridge';
  /** #320: set while the modal edits an existing NAT-Cloud. */
  let editingNetwork: NetworkData | null = null;
  let networkSaving = false;
  let networkSaveError: { message: string; field?: string } | null = null;
  let pendingNetworkPosition: { x: number; y: number } | null = null;
  let zenDialog: 'node' | 'network' | null = null;
  $: if (!zenMode) zenDialog = null;
  $: hideNodeModal = zenMode && zenDialog !== 'node';
  $: hideNetworkModal = zenMode && zenDialog !== 'network';
  let nodeActionSequence = 0;
  const nodeActionTokens = new Map<number, number>();
  const nodeActionPollSchedule = [0, 250, 500, 1000, 2000];
  let canvasDestroyed = false;
  let labNodeAction: { action: LabNodeAction; completed: number; total: number } | null = null;
  const labControlButtonClass = 'inline-flex h-8 w-8 items-center justify-center rounded-lg border border-gray-800 bg-gray-950/80 text-gray-200 transition enabled:hover:border-blue-500/40 enabled:hover:text-white disabled:cursor-not-allowed disabled:opacity-40';

  $: canStartLabNodes = Object.values($labState.nodes).some((node) => isEligibleForLabAction(node, 'start'));
  $: canStopLabNodes = Object.values($labState.nodes).some((node) => isEligibleForLabAction(node, 'stop'));
  let menu:
    | {
        x: number;
        y: number;
        targetId: string;
        targetType: 'node' | 'network' | 'edge' | 'port';
      }
    | null = null;
  let portMenuTarget: ConnectionPointTarget | null = null;

  // #268 — the node the context menu currently targets. The Console entry
  // renders one pill per available console mode from this node's config.
  $: menuNode = (() => {
    if (!menu || menu.targetType !== 'node') return null;
    const decoded = decodeId(menu.targetId);
    if (!decoded) return null;
    return $labState.nodes[String(decoded.id)] ?? null;
  })();

  $: menuNetwork = (() => {
    if (!menu || menu.targetType !== 'network') return null;
    const decoded = decodeId(menu.targetId);
    if (!decoded) return null;
    return $labState.networks[String(decoded.id)] ?? null;
  })();
  $: menuSharing = menuNetwork ? networkSharing(menuNetwork) : null;

  const nodeColor = (node: Node) => {
    if (node.type === 'network') return '#3b82f6';
    if (node.data?.status === 2) return '#10b981';
    return '#4b5563';
  };

  function deepClone<T>(value: T): T {
    return JSON.parse(JSON.stringify(value)) as T;
  }

  function clearTopologyChangeBanner() {
    if (topologyChangeBannerTimer) {
      clearTimeout(topologyChangeBannerTimer);
      topologyChangeBannerTimer = null;
    }
    topologyChangeBanner = null;
  }

  function showTopologyChangeBanner(nodeNames: string[]) {
    const uniqueNodeNames = [...new Set(nodeNames)].filter((name) => name.length > 0);
    const suffix = uniqueNodeNames.length ? `: ${uniqueNodeNames.join(', ')}` : '';
    topologyChangeBanner = {
      message: `Restart required to change topology${suffix}`,
      nodeNames: uniqueNodeNames,
    };

    if (topologyChangeBannerTimer) {
      clearTimeout(topologyChangeBannerTimer);
    }
    topologyChangeBannerTimer = setTimeout(() => {
      topologyChangeBanner = null;
      topologyChangeBannerTimer = null;
    }, 5000);
  }

  function resolveNodeCapabilities(node: NodeData): TemplateCapabilities {
    const templateCapabilities = node.capabilities ?? nodeCatalog?.templates.find(
      (template) => template.key === node.template && template.type === node.type
    )?.capabilities;
    const capabilities = templateCapabilities ?? defaultTemplateCapabilities;
    if (node.type === 'iol') {
      return { ...capabilities, hotplug: false, machine: null };
    }
    return capabilities;
  }

  function resolveEndpointNode(endpoint: DragEndpoint | null): NodeData | null {
    if (endpoint?.kind !== 'interface' && endpoint?.kind !== 'node-slot') {
      return null;
    }
    return $labState.nodes[String(endpoint.nodeId)] ?? null;
  }

  /**
   * True when the endpoint targets a NIC that does not exist at boot time.
   * Attaching those needs QMP hot-plug; links to boot interfaces are
   * host-only (issue #174) and work on any machine type while running.
   *
   * A node-slot endpoint is NOT a new NIC: the confirm modal's interface
   * picker only offers existing unconnected interfaces, so a slot drag
   * always resolves to a boot NIC. Gating it here would block the only UI
   * path to unused interfaces (ports render only for linked interfaces).
   */
  function endpointNeedsNewNic(endpoint: DragEndpoint | null): boolean {
    if (!endpoint) return false;
    if (endpoint.kind === 'node-slot') return false;
    if (endpoint.kind === 'interface') {
      const node = $labState.nodes[String(endpoint.nodeId)];
      const bootCount = node?.interfaces?.length ?? node?.ethernet ?? 0;
      return endpoint.interfaceIndex >= bootCount;
    }
    return false;
  }

  function blockedTopologyChangeNodes(snapshot: DragLinkSnapshot): NodeData[] {
    const seenNodeIds = new Set<number>();
    const blocked: NodeData[] = [];
    for (const endpoint of [snapshot.source, snapshot.target]) {
      const node = resolveEndpointNode(endpoint);
      if (!node || node.status !== 2) continue;
      const blockedHere =
        node.type === 'iol' ||
        (endpointNeedsNewNic(endpoint) && resolveNodeCapabilities(node).hotplug === false);
      if (!blockedHere) continue;
      if (!seenNodeIds.has(node.id)) {
        seenNodeIds.add(node.id);
        blocked.push(node);
      }
    }
    return blocked;
  }

  function gateLinkConfirmIfNeeded(snapshot: DragLinkSnapshot): boolean {
    const blockedNodes = blockedTopologyChangeNodes(snapshot);
    if (blockedNodes.length === 0) {
      return false;
    }

    dragLinkStore.cancel();
    dragLinkSourceAnchor = null;
    showTopologyChangeBanner(blockedNodes.map((node) => node.name));
    return true;
  }

  function dispatchCanvasChange(reason: string, detail: Record<string, unknown> = {}) {
    dispatch('changed', {
      reason,
      ...detail,
    });
  }

  function pause(ms: number) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  async function settleNodeAction(
    nodeId: number,
    actionLabId: string,
    actionIsCurrent: () => boolean,
    action: NodeLifecycleAction,
    targetStatus: 0 | 2,
    transientStatus: 'starting' | 'stopping',
    bulk: boolean
  ) {
    for (const delayMs of nodeActionPollSchedule) {
      if (delayMs > 0) {
        await pause(delayMs);
      }
      if (!actionIsCurrent()) return false;

      const nodesPath = `/labs/${actionLabId}/nodes`;
      const options = { suppressToast: true };
      // Bulk actions only need to refresh the affected node. Listing every
      // node here would reconcile the entire lab once per action and poll.
      const refreshedNode = bulk
        ? (await apiRequest<NodeData>(`${nodesPath}/${nodeId}`, options)).data
        : (await apiRequest<Record<string, NodeData>>(nodesPath, options)).data?.[String(nodeId)];
      const currentNode = $labState.nodes[String(nodeId)];
      if (!actionIsCurrent() || !refreshedNode || !currentNode) return false;

      // A bulk run can outlast local canvas edits. Adopt just the refreshed
      // runtime fields and preserve the current node's geometry and config.
      const nextNode = bulk
        ? {
          ...currentNode,
          status: refreshedNode.status,
          cpu_usage: refreshedNode.cpu_usage,
          ram_usage: refreshedNode.ram_usage,
        }
        : { ...refreshedNode };
      if (refreshedNode.status === targetStatus) {
        delete nextNode.transientStatus;
        publishNodeAction(nextNode, `node-${action}`);
        return true;
      }
      nextNode.transientStatus = transientStatus;
      publishNodeAction(nextNode, `node-${action}-pending`);
    }

    return false;
  }

  function publishNodeAction(node: NodeData, reason: string) {
    labState.upsertNode(node.id, node);
    publishFlowState();
    dispatchCanvasChange(reason, { nodeId: node.id, node: deepClone(node) });
  }

  // #275 (revision): hover focus — while a node is hovered (and no wire
  // drag is in flight) the related set — the hovered node and its
  // connected nodes — pulses its outlines instead of dimming everything
  // else. Class changes touch only the few related nodes, never the
  // N-1 remainder, and edges are left untouched.
  const HOVER_CLEAR_GRACE_MS = 120;
  let hoverFocusId: string | null = null;
  // Rapid pointer crossings fire mouseleave on the old node BEFORE
  // mouseenter on the new one; clearing focus synchronously on leave
  // would flash everything to full opacity between the two events
  // (visible flicker when sweeping across nodes). The clear is therefore
  // debounced by a short grace period that any new mouseenter cancels;
  // entering a node still applies its focus immediately.
  let hoverClearTimer: ReturnType<typeof setTimeout> | null = null;

  function cancelPendingHoverClear() {
    if (hoverClearTimer !== null) {
      clearTimeout(hoverClearTimer);
      hoverClearTimer = null;
    }
  }

  function setHoverFocus(nodeId: string | null) {
    cancelPendingHoverClear();
    if (hoverFocusId === nodeId) return;
    hoverFocusId = nodeId;
    publishFlowState();
  }

  function scheduleHoverClear() {
    if (hoverFocusId === null || hoverClearTimer !== null) return;
    hoverClearTimer = setTimeout(() => {
      hoverClearTimer = null;
      if (hoverFocusId !== null) {
        hoverFocusId = null;
        publishFlowState();
      }
    }, HOVER_CLEAR_GRACE_MS);
  }

  onDestroy(() => cancelPendingHoverClear());

  function computeHoverFocus(hoveredId: string, edges: Edge[]): Set<string> {
    const focusNodes = new Set<string>([hoveredId]);
    const adoptEndpoints = (edge: Edge) => {
      focusNodes.add(edge.source);
      focusNodes.add(edge.target);
    };
    // Direct links of the hovered node.
    edges
      .filter((edge) => edge.source === hoveredId || edge.target === hoveredId)
      .forEach(adoptEndpoints);
    // Network-brokered connections extend one hop THROUGH networks: a
    // node → network → node path lights both endpoint nodes, without
    // opening device nodes' other links.
    for (const id of [...focusNodes]) {
      if (!id.startsWith('network')) continue;
      edges
        .filter((edge) => edge.source === id || edge.target === id)
        .forEach(adoptEndpoints);
    }
    return focusNodes;
  }

  function publishFlowState() {
    const builtNodes = buildFlowNodes();
    const builtEdges = buildFlowEdges();
    // A stale hover target (node removed while hovered) must not light
    // ghosts — focus only applies to nodes that still render.
    const focusNodes =
      hoverFocusId && builtNodes.some((node) => node.id === hoverFocusId)
        ? computeHoverFocus(hoverFocusId, builtEdges)
        : null;
    nodesStore.set(
      focusNodes
        ? builtNodes.map((node) =>
            focusNodes.has(node.id)
              ? { ...node, class: `${node.class ? `${node.class} ` : ''}nv-focus-pulse` }
              : node
          )
        : builtNodes
    );
    edgesStore.set(builtEdges);
  }

  // #245: the canvas is a pure view of the shared labState store. Any store
  // change — canvas mutations, loadLab snapshots, WS-driven updates —
  // repaints the flow elements. The old props/lastXRef identity-diff sync
  // and its local deep clones are gone.
  $: if ($labState) publishFlowState();

  $: networkPaletteItems = paletteItems.filter(
    (item): item is PaletteNetworkItem => item.kind === 'network'
  );

  function nodeCardWidth(node: NodeData): number {
    const savedWidth = node.width ? Number.parseInt(node.width, 10) : 0;
    const hostnameWidth = Math.min(Math.max(node.name.length * 5.5 + 44, 152), 220);
    return Math.max(Number.isFinite(savedWidth) ? savedWidth + 48 : 0, hostnameWidth);
  }

  function networkCardWidth(network: NetworkData): number {
    return network.width ? network.width + 40 : 130;
  }

  function nodeBox(nodeId: number): NodeBox | null {
    const node = $labState.nodes[String(nodeId)];
    if (!node) return null;
    return {
      x: node.left,
      y: node.top,
      w: nodeCardWidth(node),
      h: NODE_CARD_HEIGHT,
    };
  }

  function networkBox(networkId: number): NodeBox | null {
    const network = $labState.networks[String(networkId)];
    if (!network) return null;
    return {
      x: network.left || 100,
      y: network.top || 100,
      w: networkCardWidth(network),
      h: NETWORK_CARD_HEIGHT,
    };
  }

  function endpointBox(endpoint: Link['from'] | Link['to'] | null | undefined): NodeBox | null {
    if (!endpoint) return null;
    if (typeof endpoint.node_id === 'number') return nodeBox(endpoint.node_id);
    if (typeof endpoint.network_id === 'number') return networkBox(endpoint.network_id);
    return null;
  }

  function implicitLinkPairsByNetwork(): Map<number, Link[]> {
    const pairs = new Map<number, Link[]>();
    for (const link of $labState.links) {
      const networkId = link.to?.network_id;
      if (typeof networkId !== 'number') continue;
      const network = $labState.networks[String(networkId)];
      if (!network || network.implicit !== true || network.visibility) continue;
      if (!pairs.has(networkId)) pairs.set(networkId, []);
      pairs.get(networkId)!.push(link);
    }
    return pairs;
  }

  function autoPeerEndpointFor(
    link: Link,
    key: LinkEndpointKey,
    implicitPairs: Map<number, Link[]>
  ): Link['from'] | Link['to'] | null {
    const otherKey: LinkEndpointKey = key === 'from' ? 'to' : 'from';
    const ownEndpoint = link[key];
    const otherEndpoint = link[otherKey];

    if (key === 'from' && typeof ownEndpoint?.node_id === 'number') {
      const networkId = otherEndpoint?.network_id;
      if (typeof networkId === 'number') {
        const pair = implicitPairs.get(networkId);
        if (pair?.length === 2) {
          const peer = pair.find((candidate) => candidate.id !== link.id);
          if (peer?.from && typeof peer.from.node_id === 'number') {
            return peer.from;
          }
        }
      }
    }

    return otherEndpoint ?? null;
  }

  function endpointPortPosition(
    link: Link,
    key: LinkEndpointKey,
    implicitPairs: Map<number, Link[]>,
    routeBoxes: ReadonlyMap<string, NodeBox>
  ): PortPosition | null {
    // Fully automatic (#258): the side faces the peer and the offset projects
    // toward the peer center — relocated to an unconstricted side when an
    // unrelated card blocks the gap-facing exit (bestRouteAwarePortPosition).
    // Persisted manual placements (endpoint_positions.mode 'manual') are no
    // longer honored — NetBird-style auto-positioning is the only truth.
    const ownEndpoint = link[key];
    const peerEndpoint = autoPeerEndpointFor(link, key, implicitPairs);
    const ownBox = endpointBox(ownEndpoint);
    const peerBox = endpointBox(peerEndpoint);
    if (!ownBox || !peerBox) return null;

    const flowId = (endpoint: Link['from'] | Link['to'] | null | undefined): string | null => {
      if (typeof endpoint?.node_id === 'number') return `node${endpoint.node_id}`;
      if (typeof endpoint?.network_id === 'number') return `network${endpoint.network_id}`;
      return null;
    };
    const ownId = flowId(ownEndpoint);
    const peerId = flowId(peerEndpoint);
    const obstacles = [...routeBoxes.entries()]
      .filter(([flowKey]) => flowKey !== ownId && flowKey !== peerId)
      .map(([, box]) => box);

    return bestRouteAwarePortPosition(ownBox, peerBox, obstacles);
  }

  function buildConnectionPointPositions(): {
    node: Map<number, Record<string, PortPosition>>;
    nodeRefs: Map<number, Record<string, ConnectionPointRef>>;
    network: Map<number, Record<string, PortPosition>>;
    networkRefs: Map<number, Record<string, ConnectionPointRef>>;
    nodeOpenSlot: Map<number, PortPosition>;
    networkOpenSlot: Map<number, PortPosition>;
  } {
    const node = new Map<number, Record<string, PortPosition>>();
    const nodeRefs = new Map<number, Record<string, ConnectionPointRef>>();
    const network = new Map<number, Record<string, PortPosition>>();
    const networkRefs = new Map<number, Record<string, ConnectionPointRef>>();
    const nodeOpenSlot = new Map<number, PortPosition>();
    const networkOpenSlot = new Map<number, PortPosition>();
    const implicitPairs = implicitLinkPairsByNetwork();

    // Card boxes (visible ones only) so port placement can tell when the
    // gap-facing side's exit is boxed in by an unrelated card (#258).
    const routeBoxes = new Map<string, NodeBox>();
    for (const nodeRecord of Object.values($labState.nodes)) {
      const box = nodeBox(nodeRecord.id);
      if (box) routeBoxes.set(`node${nodeRecord.id}`, box);
    }
    for (const networkRecord of Object.values($labState.networks)) {
      const box = networkBox(networkRecord.id);
      if (!box) continue;
      const networkLinkCount = $labState.links.filter(
        (link) => link?.id && link.to?.network_id === networkRecord.id
      ).length;
      const forceVisible = networkRecord.implicit === true && networkLinkCount !== 2;
      if (!networkRecord.visibility && !forceVisible) continue;
      routeBoxes.set(`network${networkRecord.id}`, box);
    }

    for (const link of $labState.links) {
      for (const key of ['from', 'to'] as const) {
        const endpoint = link[key];
        const port = endpointPortPosition(link, key, implicitPairs, routeBoxes);
        if (!endpoint || !port) continue;

        if (typeof endpoint.node_id === 'number' && typeof endpoint.interface_index === 'number') {
          const perNode = node.get(endpoint.node_id) ?? {};
          perNode[String(endpoint.interface_index)] = port;
          node.set(endpoint.node_id, perNode);

          const refs = nodeRefs.get(endpoint.node_id) ?? {};
          refs[String(endpoint.interface_index)] = { linkId: link.id, endpointKey: key };
          nodeRefs.set(endpoint.node_id, refs);
        } else if (typeof endpoint.network_id === 'number') {
          const perNetwork = network.get(endpoint.network_id) ?? {};
          perNetwork[link.id] = port;
          network.set(endpoint.network_id, perNetwork);

          const refs = networkRefs.get(endpoint.network_id) ?? {};
          refs[link.id] = { linkId: link.id, endpointKey: key };
          networkRefs.set(endpoint.network_id, refs);
        }
      }
    }

    for (const [nodeId, positions] of node.entries()) {
      const box = nodeBox(nodeId);
      if (box) node.set(nodeId, separatePositionRecord(positions, box));
    }

    for (const [networkId, positions] of network.entries()) {
      const box = networkBox(networkId);
      if (box) network.set(networkId, separatePositionRecord(positions, box));
    }

    // Dotted "new connection" handles (#258): the open slot obeys the same
    // aversion rules as placed ports — it keeps its canonical spot unless a
    // placed port on the same side sits within the min gap, in which case it
    // shifts to the nearest free stretch of that side (placed ports never
    // move, so no edge geometry changes).
    for (const nodeRecord of Object.values($labState.nodes)) {
      const slot = firstFreeInterfaceDefaultPosition(nodeRecord);
      const box = nodeBox(nodeRecord.id);
      if (!slot || !box) continue;
      nodeOpenSlot.set(
        nodeRecord.id,
        nudgeAwayFromPorts(slot, Object.values(node.get(nodeRecord.id) ?? {}), box)
      );
    }

    for (const networkRecord of Object.values($labState.networks)) {
      const box = networkBox(networkRecord.id);
      if (!box) continue;
      const linkCount = $labState.links.filter(
        (link) => link?.id && link.to?.network_id === networkRecord.id
      ).length;
      const openSlot = slotPosition(linkCount, linkCount + 1);
      networkOpenSlot.set(
        networkRecord.id,
        nudgeAwayFromPorts(
          openSlot,
          Object.values(network.get(networkRecord.id) ?? {}),
          box
        )
      );
    }

    return { node, nodeRefs, network, networkRefs, nodeOpenSlot, networkOpenSlot };
  }

  /**
   * Canonical perimeter position of a node's dotted "new connection" slot —
   * the first interface that is neither link-connected nor attached to a
   * network, mirroring PortLayer's firstAvailableEntry selection exactly.
   */
  function firstFreeInterfaceDefaultPosition(node: NodeData): PortPosition | null {
    const interfaces = node.interfaces ?? [];
    if (interfaces.length === 0) return null;
    const connected = new Set(connectedInterfaceIndexesForNode(node));
    const index = interfaces.findIndex(
      (iface, i) => !connected.has(iface.index ?? i) && (iface.network_id ?? 0) <= 0
    );
    if (index < 0) return null;
    return placeInterfaces(interfaces.length)[index] ?? null;
  }

  function separatePositionRecord(
    positionsByKey: Record<string, PortPosition>,
    box: NodeBox
  ): Record<string, PortPosition> {
    const keys = Object.keys(positionsByKey);
    const separated = separatePortPositions(
      keys.map((key) => positionsByKey[key]),
      box
    );
    return Object.fromEntries(keys.map((key, index) => [key, separated[index]]));
  }

  function projectNodeIcons(flowNodes: Node[]): Node[] {
    const iconAssets = new Set(nodeCatalog?.icon_assets);
    if (iconAssets.size === 0) return flowNodes;
    const templateIcons = new Map(
      nodeCatalog?.templates.map((template) => [
        `${template.type}:${template.key}`,
        template.defaults.icon
      ])
    );
    return flowNodes.map((flowNode) => {
      const node = flowNode.type === 'device' ? $labState.nodes[String(flowNode.data.nodeId)] : undefined;
      if (!node) return flowNode;
      const templateIcon = templateIcons.get(`${node.type}:${node.template}`);
      const iconSources = nodeIconSources(node.icon, templateIcon, iconAssets);
      return { ...flowNode, data: { ...flowNode.data, iconSources } };
    });
  }

  function buildFlowNodes(): Node[] {
    const flowNodes: Node[] = [];
    const connectionPointPositions = buildConnectionPointPositions();

    for (const node of Object.values($labState.nodes)) {
      flowNodes.push({
        id: `node${node.id}`,
        type: 'device',
        position: { x: node.left, y: node.top },
        data: {
          label: node.name,
          status: node.status,
          transientStatus: node.transientStatus,
          type: node.type,
          template: node.template,
          console: node.console,
          nodeId: node.id,
          interfaces: node.interfaces ?? [],
          connectedInterfaceIndexes: connectedInterfaceIndexesForNode(node),
          portPositionsByInterfaceIndex: connectionPointPositions.node.get(node.id) ?? {},
          portRefsByInterfaceIndex: connectionPointPositions.nodeRefs.get(node.id) ?? {},
          newConnectionPortPosition: connectionPointPositions.nodeOpenSlot.get(node.id) ?? null,
          highlightedInterfaceIndex: highlightedTargetForNode(node.id),
          highlightedNewConnection: highlightedNewConnectionForNode(node.id),
          interface_naming_scheme: node.interface_naming_scheme ?? null
        },
        style: `width: ${nodeCardWidth(node)}px;`
      });
    }

    const linkIdsByNetwork = new Map<number, string[]>();
    for (const link of $labState.links) {
      if (!link?.id) continue;
      const networkId = link.to?.network_id;
      if (typeof networkId !== 'number') continue;
      if (!linkIdsByNetwork.has(networkId)) linkIdsByNetwork.set(networkId, []);
      linkIdsByNetwork.get(networkId)!.push(link.id);
    }

    for (const network of Object.values($labState.networks)) {
      // Implicit bridges normally render as paired collapsed edges (handled
      // in canvasEdges.ts) and the bridge node itself stays hidden. But an
      // implicit network with refcount != 2 is in a broken state — orphan
      // half left over after the peer was repointed. Force-show those so
      // the orphan edge has a target node to land on; otherwise the link
      // disappears from the canvas and the corruption is invisible.
      const networkLinkIds = linkIdsByNetwork.get(network.id) ?? [];
      const isBrokenImplicit =
        network.implicit === true && networkLinkIds.length !== 2;
      if (!network.visibility && !isBrokenImplicit) continue;
      flowNodes.push({
        id: `network${network.id}`,
        type: 'network',
        position: { x: network.left || 100, y: network.top || 100 },
        data: {
          label: network.name,
          icon: network.icon,
          type: 'network',
          count: network.count ?? 0,
          networkId: network.id,
          sharingRole: networkSharing(network).role,
          linkIds: networkLinkIds,
          portPositionsByLinkId: connectionPointPositions.network.get(network.id) ?? {},
          portRefsByLinkId: connectionPointPositions.networkRefs.get(network.id) ?? {},
          openSlotPosition: connectionPointPositions.networkOpenSlot.get(network.id) ?? null,
          brokenImplicit: isBrokenImplicit
        },
        style: `width: ${networkCardWidth(network)}px;`
      });
    }

    return projectNodeIcons(flowNodes);
  }

  function buildFlowEdges(): Edge[] {
    // Partition the unified reconciliation store into the two visual overlays.
    // When labWsStores is not yet mounted (SSR / pre-mount) both maps are empty
    // and links render with their default styling.
    const recon = labWsStores ? get(labWsStores.linkReconciliation) : {};
    const entries = Object.values(recon);

    // US-404: declared links with no kernel veth/TAP → keyed by link_id.
    const divergentByLinkId: Record<string, LinkReconciliation> = {};
    // US-403: kernel-only ifaces not in links[] → DiscoveredLink list for 3rd arg.
    const discoveredList: DiscoveredLink[] = [];

    for (const entry of entries) {
      if (entry.kind === 'divergent' && entry.link_id) {
        divergentByLinkId[entry.link_id] = entry;
      } else if (
        entry.kind === 'discovered' &&
        entry.iface &&
        typeof entry.network_id === 'number' &&
        typeof entry.bridge_name === 'string'
      ) {
        discoveredList.push({
          iface: entry.iface,
          bridge_name: entry.bridge_name,
          network_id: entry.network_id,
          peer_node_id: entry.peer_node_id ?? null,
          peer_interface_index: entry.peer_interface_index ?? null,
        });
      }
    }

    // Obstacle-avoiding orthogonal routes (#254/#258): hand every edge the
    // other cards' boxes so the router adds joints and detours around them
    // instead of passing underneath. Hidden implicit bridges stay
    // non-obstacles — nothing is drawn there to avoid.
    const routeObstacles = new Map<string, Obstacle>();
    for (const node of Object.values($labState.nodes)) {
      routeObstacles.set(`node${node.id}`, nodeBox(node.id)!);
    }
    for (const network of Object.values($labState.networks)) {
      const networkLinkCount = $labState.links.filter(
        (link) => link?.id && link.to?.network_id === network.id
      ).length;
      const forceVisible = network.implicit === true && networkLinkCount !== 2;
      if (!network.visibility && !forceVisible) continue;
      routeObstacles.set(`network${network.id}`, networkBox(network.id)!);
    }

    return attachRouteObstacles(
      deriveEdges($labState.links, $labState.defaults, discoveredList, divergentByLinkId, $labState.networks),
      routeObstacles
    );
  }

  $: {
    publishFlowState();
  }

  function closeMenu() {
    menu = null;
    portMenuTarget = null;
  }

  function closeAddMenu() {
    addMenuStep = 'closed';
    addMenuKind = null;
  }

  function toggleAddMenu() {
    if (addMenuStep === 'closed') {
      addMenuStep = 'kind';
      return;
    }

    closeAddMenu();
  }

  function openCreateNodeModal() {
    nodeCreateAnchor =
      typeof window !== 'undefined'
        ? screenToFlowPosition(
            { x: Math.round(window.innerWidth * 0.5), y: Math.round(window.innerHeight * 0.5) },
            { snapToGrid: true }
          )
        : { x: 200, y: 200 };
    closeAddMenu();
    closeMenu();
    nodeModalMode = 'create';
    nodeModalNode = null;
    nodeModalOpen = true;
    if (zenMode) zenDialog = 'node';
  }

  function openEditNodeModal(targetId: string) {
    const decoded = decodeId(targetId);
    if (!decoded || decoded.type !== 'node') {
      return;
    }

    const node = $labState.nodes[String(decoded.id)];
    if (!node) {
      return;
    }

    closeMenu();
    nodeModalMode = 'edit';
    nodeModalNode = deepClone(node);
    nodeModalOpen = true;
    if (zenMode) zenDialog = 'node';
  }

  function openAddKind(kind: 'node' | 'network') {
    if (kind === 'node') {
      openCreateNodeModal();
      return;
    }

    addMenuKind = kind;
    addMenuStep = 'item';
  }

  function toggleCanvasLock() {
    canvasLocked = !canvasLocked;
    closeMenu();
    closeAddMenu();
  }

  async function handleZoomIn() {
    if (canvasLocked) return;
    await zoomIn({ duration: 120 });
  }

  async function handleZoomOut() {
    if (canvasLocked) return;
    await zoomOut({ duration: 120 });
  }

  async function handleFitView() {
    if (canvasLocked) return;
    await fitView({ duration: 180, padding: 0.2 });
  }

  function eventPoint(event: MouseEvent | TouchEvent): { x: number; y: number } {
    if ('clientX' in event) {
      return { x: event.clientX, y: event.clientY };
    }

    const touch = event.touches[0] ?? event.changedTouches[0];
    return {
      x: touch?.clientX ?? 0,
      y: touch?.clientY ?? 0
    };
  }

  onMount(async () => {
    try {
      const catalogResponse = await apiRequest<NodeCatalog>(`/labs/${labId}/node-catalog`, {
        suppressToast: true
      });
      nodeCatalog = catalogResponse.data;
      nodesStore.update(projectNodeIcons);
      paletteItems = NETWORK_PALETTE;
    } catch (_error) {
      paletteItems = NETWORK_PALETTE;
    } finally {
      paletteLoading = false;
    }
  });

  function scheduleSave() {
    if (saveTimer) {
      clearTimeout(saveTimer);
    }

    saveState = 'saving';
    saveTimer = setTimeout(async () => {
      try {
        const expected = $labState.revision;
        const result = await apiRequest<{ revision?: number }>(`/labs/${labId}/topology`, {
          method: 'PUT',
          headers:
            expected !== null && expected !== undefined
              ? { 'If-Match': String(expected) }
              : undefined,
          // Topology-only payload. Node/network deletion is already
          // persisted by DELETE /nodes/{id} and DELETE /networks/{id}
          // (both filter topology[] server-side). Echoing full
          // $labState.nodes/$labState.networks here re-sent positions captured
          // before a racing loadLab() reload, reverting layout saves
          // that PUT /layout had already persisted. Positions travel
          // exclusively via layoutDebouncer → PUT /layout.
          body: {
            topology: $labState.topology
          }
        });
        if (typeof result?.revision === 'number') {
          labState.setRevision(result.revision);
        }
        saveState = 'saved';
      } catch (error) {
        if (error instanceof ApiError && error.statusCode === 412) {
          // #245: lab changed elsewhere. Surface it and let the page
          // re-adopt the server snapshot, then retry the (queued) layout
          // flush; the topology payload itself is re-sent by the next
          // mutation, so no retry here.
          saveState = 'idle';
          toastStore.push('Lab changed in another session — reloading latest state.', 'error');
          dispatchCanvasChange('layout-conflict');
          return;
        }
        saveState = 'idle';
      }
    }, 500);
  }

  function decodeId(elementId: string): { type: 'node' | 'network'; id: number } | null {
    if (elementId.startsWith('node')) {
      return { type: 'node', id: Number(elementId.slice(4)) };
    }
    if (elementId.startsWith('network')) {
      return { type: 'network', id: Number(elementId.slice(7)) };
    }
    return null;
  }

  function recalculateNetworks() {
    const usage = new Map<number, number>();
    const nextNetworks = { ...$labState.networks };

    for (const node of Object.values($labState.nodes)) {
      for (const iface of node.interfaces ?? []) {
        if (iface.network_id > 0) {
          usage.set(iface.network_id, (usage.get(iface.network_id) ?? 0) + 1);
        }
      }
    }

    for (const [networkKey, network] of Object.entries(nextNetworks)) {
      const count = usage.get(Number(networkKey)) ?? 0;
      network.count = count;
      if (!network.visibility && count === 0) {
        delete nextNetworks[networkKey];
      }
    }

    labState.setNetworks(nextNetworks);
  }

  function handleNodeDragStop(event: CustomEvent<{ targetNode: Node | null }>) {
    const movedNode = event.detail.targetNode;
    if (!movedNode) {
      return;
    }

    const decoded = decodeId(movedNode.id);
    if (!decoded) {
      return;
    }

    const left = Math.round(movedNode.position.x);
    const top = Math.round(movedNode.position.y);

    // #245 regression guard: never assign THROUGH $labState member paths —
    // Svelte 5 legacy compilation turns `$store.x.y = v` into
    // `store.set($store, …)`, and the labState facade has no `.set()`,
    // which threw `TypeError: e.set is not a function` on every drag stop
    // and silently killed all layout saves. Positions go through
    // labState.applyDragStop; the debouncer only fires when adopted.
    const applied =
      decoded.type === 'node'
        ? labState.applyDragStop('node', decoded.id, left, top)
        : labState.applyDragStop('network', decoded.id, left, top);
    if (!applied) {
      return;
    }
    if (decoded.type === 'node') {
      layoutDebouncer.pushNodePosition(decoded.id, left, top);
    } else {
      layoutDebouncer.pushNetworkPosition(decoded.id, left, top);
    }
  }

  function handleViewportEnd(event: CustomEvent<LabViewport>) {
    const viewport = event.detail;
    if (!viewport) return;
    layoutDebouncer.pushViewport(viewport);
  }

  function getLinkIndexesForEdge(edgeId: string): number[] {
    const prefix = 'link:';
    if (edgeId.startsWith(prefix)) {
      const linkId = edgeId.slice(prefix.length);
      const index = $labState.links.findIndex((entry) => entry.id === linkId);
      return index >= 0 ? [index] : [];
    }

    const edge = buildFlowEdges().find((entry) => entry.id === edgeId);
    const implicitLinkIds = (edge?.data as { implicit_bridge_link_ids?: unknown } | undefined)
      ?.implicit_bridge_link_ids;
    if (!Array.isArray(implicitLinkIds)) return [];

    const ids = new Set(implicitLinkIds.map((id) => String(id)));
    return $labState.links
      .map((entry, index) => (entry?.id && ids.has(String(entry.id)) ? index : -1))
      .filter((index) => index >= 0);
  }

  function isDiscoveredEdgeId(edgeId: string | null | undefined): boolean {
    return typeof edgeId === 'string' && edgeId.startsWith('discovered:');
  }

  /**
   * US-403 — promote a kernel-discovered link into ``links[]`` by POSTing a
   * real link to ``/labs/{labId}/links``.  On success we drop the discovered
   * overlay locally; the next discovery cycle would clear it anyway, but
   * optimistic removal keeps the canvas snappy.
   */
  async function promoteDiscoveredLink(edgeId: string) {
    if (!isDiscoveredEdgeId(edgeId)) return;
    const iface = edgeId.slice('discovered:'.length);
    const recon = labWsStores ? get(labWsStores.linkReconciliation) : {};
    const entry = recon[`iface:${iface}`];
    if (!entry || entry.kind !== 'discovered') return;

    if (typeof entry.peer_node_id !== 'number') {
      toastStore.push(
        'Cannot promote discovered link: peer node could not be decoded from iface name.',
        'error',
      );
      return;
    }
    const ifaceIndex = entry.peer_interface_index ?? parseIfaceInterfaceIndex(iface);
    if (typeof ifaceIndex !== 'number') {
      toastStore.push(
        'Cannot promote discovered link: interface index could not be decoded from iface name.',
        'error',
      );
      return;
    }
    if (typeof entry.network_id !== 'number') {
      toastStore.push(
        'Cannot promote discovered link: network_id is missing from reconciliation entry.',
        'error',
      );
      return;
    }

    const fromEndpoint = { node_id: entry.peer_node_id, interface_index: ifaceIndex };
    const toEndpoint = { network_id: entry.network_id };

    try {
      const response = await apiRequest<LinkCreateResponse>(`/labs/${labId}/links`, {
        method: 'POST',
        body: { from: fromEndpoint, to: toEndpoint },
        suppressToast: true,
      });
      const created = createdFromLinkResponse(response);
      if (created.link) {
        labState.applyCreatedLink(created.link, created.network);
      }
      adoptRevision(response);
      // Optimistically remove the discovered overlay immediately (HIGH-1).
      // The next discovery cycle would also clear it, but removing it now
      // avoids a duplicate amber+gray edge flash while the cycle completes.
      if (labWsStores) {
        labWsStores.deleteReconciliation(`iface:${iface}`);
      }
      publishFlowState();
      dispatchCanvasChange('topology', {
      });
      toastStore.push('Promoted discovered link to declared link.', 'info');
    } catch (error) {
      const message =
        error instanceof ApiError
          ? error.message
          : error instanceof Error
            ? error.message
            : 'Failed to promote discovered link';
      toastStore.push(message, 'error');
    }
  }

  async function deleteLink(edgeId: string) {
    const indexes = getLinkIndexesForEdge(edgeId);
    if (indexes.length === 0) {
      return;
    }

    const deleteIndexes = new Set(indexes);
    const linksToDelete = indexes
      .map((index) => $labState.links[index])
      .filter((link): link is Link => !!link?.id);
    // Capture pre-mutation snapshot for rollback on API failure.
    const previousLinks = $labState.links;
    const previousNodes = $labState.nodes;
    const previousNetworks = $labState.networks;
    const previousTopology = $labState.topology;

    for (const v2Link of linksToDelete) {
      if (
        typeof v2Link.from?.node_id === 'number' &&
        typeof v2Link.from.interface_index === 'number'
      ) {
        const node = $labState.nodes[String(v2Link.from.node_id)];
        const iface = node?.interfaces[v2Link.from.interface_index];
        if (iface) iface.network_id = 0;
      }
      if (
        typeof v2Link.to?.node_id === 'number' &&
        typeof v2Link.to.interface_index === 'number'
      ) {
        const node = $labState.nodes[String(v2Link.to.node_id)];
        const iface = node?.interfaces[v2Link.to.interface_index];
        if (iface) iface.network_id = 0;
      }
    }

    labState.setNodes({ ...$labState.nodes });
    labState.setTopology($labState.topology.filter((_, currentIndex) => !deleteIndexes.has(currentIndex)));
    labState.setLinks($labState.links.filter((_, currentIndex) => !deleteIndexes.has(currentIndex)));
    recalculateNetworks();
    publishFlowState();
    dispatchCanvasChange('topology', {
    });

    // Issue #174 follow-up: link deletion is a v2 link operation. Use
    // DELETE /links/{id} (the proper REST surface) instead of the legacy
    // PUT /topology — that endpoint regenerates links[] from topology[]
    // and used to silently wipe ALL surviving links on this lab when the
    // delete trimmed an entry from a $labState.topology that was already empty
    // (v2-only labs). tmp_ ids are placeholders for in-flight POST /links;
    // skip the DELETE because there's no server-side link yet.
    const persistedLinks = linksToDelete.filter((link) => !String(link.id).startsWith('tmp_'));
    if (persistedLinks.length === 0) return;

    try {
      const responses = await Promise.all(
        persistedLinks.map((link) =>
          apiRequest(`/labs/${labId}/links/${encodeURIComponent(link.id)}`, {
            method: 'DELETE',
            suppressToast: true,
          })
        )
      );
      responses.forEach(adoptRevision);
    } catch (error) {
      labState.setLinks(previousLinks);
      labState.setNodes(previousNodes);
      labState.setNetworks(previousNetworks);
      labState.setTopology(previousTopology);
      publishFlowState();
      dispatchCanvasChange('topology', {
      });
      const message =
        error instanceof ApiError
          ? error.message
          : error instanceof Error
            ? error.message
            : 'Failed to delete link';
      toastStore.push(message, 'error');
    }
  }

  async function setLinkStyle(edgeId: string, style: LinkStyle | null) {
    const indexes = getLinkIndexesForEdge(edgeId);
    if (indexes.length === 0) return;

    const updateIndexes = new Set(indexes);
    const targets = indexes
      .map((index) => $labState.links[index])
      .filter((link): link is Link => !!link?.id);
    if (targets.length === 0) return;

    const previousStyles = new Map(targets.map((link) => [link.id, link.style_override ?? null]));
    labState.setLinks($labState.links.map((link, i) =>
      updateIndexes.has(i) ? { ...link, style_override: style } : link
    ));
    publishFlowState();

    // tmp_ ids are placeholders for in-flight POST /links; the eventual
    // server response will carry the override, so skip the PATCH.
    const persistedTargets = targets.filter((link) => !String(link.id).startsWith('tmp_'));
    if (persistedTargets.length === 0) return;

    try {
      const responses = await Promise.all(
        persistedTargets.map((link) =>
          apiRequest(`/labs/${labId}/links/${encodeURIComponent(link.id)}`, {
            method: 'PATCH',
            body: { style_override: style },
            suppressToast: true,
          })
        )
      );
      responses.forEach(adoptRevision);
    } catch (error) {
      labState.setLinks($labState.links.map((link, i) =>
        updateIndexes.has(i) ? { ...link, style_override: previousStyles.get(link.id) ?? null } : link
      ));
      publishFlowState();
      const message = error instanceof Error ? error.message : 'Failed to update link style';
      toastStore.push(message, 'error');
    }
  }

  // #268 — pill label for a console mode. A paired-template serial primary
  // is served by the runtime as a telnet server, so the HTML5 session (and
  // the pill) reports telnet.
  function consolePillLabel(mode: NodeData['console'] | null | undefined): string {
    if (!mode) return '';
    return mode === 'serial' ? 'telnet' : mode;
  }

  function isEligibleForLabAction(node: NodeData, action: LabNodeAction) {
    return node.status === (action === 'start' ? 0 : 2) && !node.transientStatus;
  }

  async function runLabNodeAction(action: LabNodeAction) {
    if (labNodeAction) return;
    const candidates = Object.values($labState.nodes).filter((node) => isEligibleForLabAction(node, action));
    if (!candidates.length) return;

    const actionLabId = labId;
    const stillInLab = () => !canvasDestroyed && labId === actionLabId;
    const failures: Array<{ name: string; message: string }> = [];
    let succeeded = 0;
    labNodeAction = { action, completed: 0, total: candidates.length };
    try {
      // Run one lifecycle operation at a time so large labs don't flood the
      // runtime with concurrent image copies, launches, or status requests.
      for (const [index, candidate] of candidates.entries()) {
        if (!stillInLab()) return;
        const currentNode = $labState.nodes[String(candidate.id)];
        if (currentNode && isEligibleForLabAction(currentNode, action) && !nodeActionTokens.has(currentNode.id)) {
          try {
            const settled = await runNodeLifecycle(action, currentNode, true);
            if (settled) {
              succeeded += 1;
            } else {
              failures.push({ name: currentNode.name, message: 'Node did not reach the requested state.' });
            }
          } catch (error) {
            failures.push({
              name: currentNode.name,
              message: error instanceof Error ? error.message : 'The lifecycle request failed.',
            });
          }
        }
        labNodeAction = { action, completed: index + 1, total: candidates.length };
      }

      if (!stillInLab()) return;
      const pastTense = action === 'start' ? 'Started' : 'Stopped';
      const success = `${pastTense} ${succeeded} ${succeeded === 1 ? 'node' : 'nodes'}.`;
      if (failures.length) {
        const [first, ...remaining] = failures;
        const more = remaining.length ? ` and ${remaining.length} more` : '';
        toastStore.push(`${success} Failed to ${action} ${first.name}${more}. ${first.message}`, 'error');
      } else {
        toastStore.push(success, 'success');
      }
    } finally {
      labNodeAction = null;
    }
  }

  async function runNodeLifecycle(action: NodeLifecycleAction, node: NodeData, bulk = false) {
    const { id: nodeId } = node;
    if (nodeActionTokens.has(nodeId)) return false;
    const actionLabId = labId;
    const token = ++nodeActionSequence;
    const actionIsCurrent = () => !canvasDestroyed && labId === actionLabId && nodeActionTokens.get(nodeId) === token;
    const transientStatus = action === 'start' ? 'starting' : 'stopping';
    const targetStatus = action === 'start' ? 2 : 0;

    nodeActionTokens.set(nodeId, token);
    publishNodeAction({ ...node, transientStatus }, `node-${action}-pending`);
    try {
      const options = bulk ? { suppressToast: true, retry: 0 } : undefined;
      await apiRequest(`/labs/${actionLabId}/nodes/${nodeId}/${action}`, options);
      return await settleNodeAction(nodeId, actionLabId, actionIsCurrent, action, targetStatus, transientStatus, bulk);
    } finally {
      if (actionIsCurrent()) {
        const currentNode = $labState.nodes[String(nodeId)];
        if (currentNode?.transientStatus === transientStatus) {
          const cleanedNode = { ...currentNode };
          delete cleanedNode.transientStatus;
          publishNodeAction(cleanedNode, 'node-edit');
        }
      }
      if (nodeActionTokens.get(nodeId) === token) nodeActionTokens.delete(nodeId);
    }
  }

  async function handleNodeAction(
    action: NodeLifecycleAction | 'delete' | 'console' | 'recordings',
    targetId: string,
    consoleMode: 'primary' | 'second' = 'primary'
  ) {
    closeMenu();
    const decoded = decodeId(targetId);
    if (!decoded || decoded.type !== 'node') {
      return;
    }

    const node = $labState.nodes[String(decoded.id)];
    if (!node) {
      return;
    }

    if (action === 'console') {
      dispatch('console', { nodeId: decoded.id, node, mode: consoleMode });
      return;
    }

    if (action === 'recordings') {
      dispatch('recordings', { node });
      return;
    }

    if (action === 'delete') {
      const response = await apiRequest(`/labs/${labId}/nodes/${decoded.id}`, { method: 'DELETE' });
      adoptRevision(response);
      const nextNodes = { ...$labState.nodes };
      delete nextNodes[String(decoded.id)];
      labState.setNodes(nextNodes);
      labState.setTopology($labState.topology.filter(
        (link) => link.source !== `node${decoded.id}` && link.destination !== `node${decoded.id}`
      ));
      recalculateNetworks();
      publishFlowState();
      scheduleSave();
      dispatchCanvasChange('node-delete', {
        nodeId: decoded.id,
      });
      return;
    }

    await runNodeLifecycle(action, node);
  }

  function handleEditNode(targetId: string) {
    openEditNodeModal(targetId);
  }

  function handleNetworkInfo(targetId: string) {
    closeMenu();
    const decoded = decodeId(targetId);
    if (decoded?.type === 'network') {
      infoPanels.open('network', decoded.id);
    }
  }

  async function handleNetworkSharing(targetId: string, shared: boolean) {
    closeMenu();
    const decoded = decodeId(targetId);
    if (!decoded || decoded.type !== 'network') {
      return;
    }
    try {
      const response = await apiRequest(`/labs/${labId}/networks/${decoded.id}/sharing`, {
        method: 'PUT',
        body: { shared }
      });
      adoptRevision(response);
      const updated = (response as unknown as { network: NetworkData }).network;
      const current = $labState.networks[String(decoded.id)];
      if (current) {
        labState.setNetworks({
          ...$labState.networks,
          [String(decoded.id)]: { ...current, shared: updated.shared === true }
        });
        publishFlowState();
      }
      toastStore.push(response.message, 'success');
    } catch (_error) {
      // apiRequest already surfaced a toast; nothing else to do here.
    }
  }

  async function handleNetworkDelete(targetId: string) {
    closeMenu();
    const decoded = decodeId(targetId);
    if (!decoded || decoded.type !== 'network') {
      return;
    }

    const response = await apiRequest(`/labs/${labId}/networks/${decoded.id}`, { method: 'DELETE' });
    adoptRevision(response);
    const nextNetworks = { ...$labState.networks };
    delete nextNetworks[String(decoded.id)];
    labState.setNetworks(nextNetworks);
    labState.setTopology($labState.topology.filter((link) => link.network_id !== decoded.id));
    for (const node of Object.values($labState.nodes)) {
      for (const iface of node.interfaces) {
        if (iface.network_id === decoded.id) {
          iface.network_id = 0;
        }
      }
    }
    labState.setNodes({ ...$labState.nodes });
    publishFlowState();
    scheduleSave();
    dispatchCanvasChange('network-delete', {
    });
  }

  async function createNetworkAt(
    position: { x: number; y: number },
    options: { name: string; type: NetworkType; config?: NetworkCreateConfig }
  ) {
    const body: {
      name: string;
      type: NetworkType;
      left: number;
      top: number;
      config?: NetworkCreateConfig;
    } = {
      name: options.name,
      type: options.type,
      left: Math.round(position.x),
      top: Math.round(position.y)
    };
    if (options.config && Object.keys(options.config).length > 0) {
      body.config = options.config;
    }
    const response = await apiRequest<NetworkData>(`/labs/${labId}/networks`, {
      method: 'POST',
      body
    });
    // POST /networks (US-063/US-064) returns ``{...,"network": <record>}`` — the
    // single-record envelope chosen for write routes. List routes still use
    // ``data:``; ApiResponse<T>.data is therefore unset here.
    const created = (response as unknown as { network: NetworkData }).network;
    adoptRevision(response);
    labState.setNetworks({
      ...$labState.networks,
      [String(created.id)]: created
    });
    publishFlowState();
    dispatchCanvasChange('network-create', {
    });
  }

  function nextAvailableNetworkName(): string {
    // Lowest ``net-N`` not currently taken (case-insensitive, trimmed).
    // Earlier ``net-${count + 1}`` collided after deletes: removing
    // ``net-3`` from {net-1, net-2, net-3} dropped count to 2 → next
    // create suggested ``net-2`` which already exists, and the server
    // 409s. This walks the in-memory set to skip occupied slots.
    const used = new Set<string>();
    for (const id of Object.keys($labState.networks)) {
      const record = $labState.networks[id];
      if (record && typeof record.name === 'string') {
        const trimmed = record.name.trim().toLowerCase();
        if (trimmed) used.add(trimmed);
      }
    }
    let i = 1;
    while (used.has(`net-${i}`)) i += 1;
    return `net-${i}`;
  }

  function addPaletteItem(item: PaletteItem) {
    const viewport = typeof window !== 'undefined'
      ? screenToFlowPosition(
          { x: Math.round(window.innerWidth * 0.5), y: Math.round(window.innerHeight * 0.5) },
          { snapToGrid: true }
        )
      : { x: 200, y: 200 };

    if (item.kind === 'network') {
      pendingNetworkPosition = viewport;
      editingNetwork = null;
      networkSaveError = null;
      networkModalDefaultName = nextAvailableNetworkName();
      networkModalDefaultType = item.networkType;
      networkModalOpen = true;
      if (zenMode) zenDialog = 'network';
      closeAddMenu();
      return;
    }

    closeAddMenu();
  }

  async function onNetworkModalConfirm(
    event: CustomEvent<{ name: string; type: NetworkType; config?: NetworkCreateConfig }>
  ) {
    if (networkSaving) return;
    if (editingNetwork) {
      await saveNetworkEdit(editingNetwork, event.detail);
      return;
    }
    const position = pendingNetworkPosition ?? { x: 200, y: 200 };
    networkModalOpen = false;
    pendingNetworkPosition = null;
    try {
      await createNetworkAt(position, event.detail);
    } catch (_error) {
      // apiRequest already surfaced a toast; nothing else to do here.
    }
  }

  function handleNetworkConfigure(targetId: string) {
    closeMenu();
    const decoded = decodeId(targetId);
    if (!decoded || decoded.type !== 'network') return;
    const network = $labState.networks[String(decoded.id)];
    if (!network) return;
    editingNetwork = deepClone(network);
    networkSaveError = null;
    networkModalOpen = true;
    if (zenMode) zenDialog = 'network';
  }

  function networkSavedMessage(before: NetworkData, after: NetworkData): string {
    const wasOn = before.config?.dhcp !== false;
    const isOn = after.config?.dhcp !== false;
    if (wasOn && !isOn) return 'DHCP disabled.';
    const rangeChanged = ['dhcp_start', 'dhcp_end'].some((key) => before.config?.[key] !== after.config?.[key]);
    if (!isOn || (wasOn && !rangeChanged)) return 'Network settings saved.';
    // The host reports a pid only for a server it verified as running.
    const started = Number(after.runtime?.dhcp_pid ?? 0) > 0;
    if (!wasOn) return started ? 'DHCP enabled; server running.' : 'DHCP enabled.';
    return started ? 'DHCP range updated; server restarted.' : 'DHCP range updated.';
  }

  /** The modal stays open until the server accepts the change (#320). */
  async function saveNetworkEdit(
    network: NetworkData,
    detail: { name: string; config?: NetworkCreateConfig }
  ) {
    const body: Record<string, unknown> = {};
    if (detail.name !== network.name) body.name = detail.name;
    if (detail.config && Object.keys(detail.config).length > 0) body.config = detail.config;
    networkSaving = true;
    networkSaveError = null;
    try {
      const response = await apiRequest<NetworkData>(`/labs/${labId}/networks/${network.id}`, {
        method: 'PATCH',
        body,
        suppressToast: true
      });
      const updated = (response as unknown as { network: NetworkData }).network;
      adoptRevision(response);
      labState.setNetworks({ ...$labState.networks, [String(updated.id)]: updated });
      publishFlowState();
      toastStore.push(networkSavedMessage(network, updated), 'success');
      onNetworkModalCancel();
      dispatchCanvasChange('network-update', {
      });
    } catch (error) {
      networkSaveError = {
        message: error instanceof Error ? error.message : String(error),
        field: error instanceof ApiError ? error.field : undefined
      };
    } finally {
      networkSaving = false;
    }
  }

  function onNetworkModalCancel() {
    networkModalOpen = false;
    pendingNetworkPosition = null;
    editingNetwork = null;
    networkSaveError = null;
  }

  setContext('nova-ve:connection-point-context-menu', (
    target: ConnectionPointTarget,
    event: MouseEvent
  ) => {
    if (zenMode) return;
    const point = eventPoint(event);
    closeAddMenu();
    portMenuTarget = target;
    menu = {
      x: point.x,
      y: point.y,
        targetId:
          target.kind === 'node'
          ? `node-port:${target.nodeId}:${target.interfaceIndex}:${target.linkId ?? ''}:${target.endpointKey ?? ''}`
          : `network-port:${target.networkId}:${target.linkId}:${target.endpointKey ?? ''}`,
      targetType: 'port',
    };
  });

  // #258: findLinkEndpointForConnectionPoint removed with the manual
  // placement persistence it served.

  // #258: persistConnectionPointPosition / withEndpointPosition removed —
  // manual endpoint placements are no longer written or honored.

  function highlightedTargetForNode(candidateNodeId: number): number | null {
    const snap = getDragLinkSnapshot();
    const target = snap.target;
    if (target?.kind === 'interface' && target.nodeId === candidateNodeId) {
      return target.interfaceIndex;
    }
    return null;
  }

  function highlightedNewConnectionForNode(candidateNodeId: number): boolean {
    const snap = getDragLinkSnapshot();
    const target = snap.target;
    return target?.kind === 'node-slot' && target.nodeId === candidateNodeId;
  }

  function connectedInterfaceIndexesForNode(node: NodeData): number[] {
    const indexes = new Set<number>();
    for (const [index, iface] of node.interfaces.entries()) {
      if ((iface.network_id ?? 0) > 0) {
        indexes.add(iface.index ?? index);
      }
    }
    for (const link of $labState.links) {
      for (const endpoint of [link.from, link.to]) {
        if (endpoint?.node_id === node.id && typeof endpoint.interface_index === 'number') {
          indexes.add(endpoint.interface_index);
        }
      }
    }
    return [...indexes];
  }

  function handleWindowMouseMove(event: MouseEvent) {
    if (zenMode) return;
    const snap = getDragLinkSnapshot();
    if (snap.state === 'idle' || snap.state === 'confirming') return;
    dragLinkStore.move({ pointer: { x: event.clientX, y: event.clientY } });
  }

  function handleWindowMouseUp(event: MouseEvent) {
    if (zenMode) return;
    const snap = getDragLinkSnapshot();
    if (snap.state === 'idle' || snap.state === 'confirming') return;
    if (snap.state === 'near_target') {
      // Mouse-up over target is handled by the port's mouseup; if we got here
      // without a target, treat as a far-from-target cancel.
      if (!snap.target) {
        dragLinkStore.cancel();
      }
      return;
    }
    // dragging / port_pressed without entering a target hot zone → cancel.
    dragLinkStore.release({
      pointer: { x: event.clientX, y: event.clientY },
      targetReachable: false,
    });
  }

  function handleWindowKeyDown(event: KeyboardEvent) {
    if (zenMode || event.key !== 'Escape') return;
    const snap = getDragLinkSnapshot();
    if (snap.state !== 'idle') {
      dragLinkStore.cancel();
      dragLinkSourceAnchor = null;
    }
  }

  /** Whether the other half of an implicit bridge's pair is already in local state. */
  function hasPeerHalf(networkId: number, linkId: string): boolean {
    return $labState.links.some(
      (entry) =>
        entry.id !== linkId &&
        !String(entry.id).startsWith('tmp_') &&
        entry.to?.network_id === networkId
    );
  }

  async function handleLinkConfirm(
    event: CustomEvent<{
      styleOverride: LinkStyle;
      sourceInterfaceIndex?: number;
      targetInterfaceIndex?: number;
    }>
  ) {
    const snap = getDragLinkSnapshot();
    if (!snap.source || !snap.target || !snap.idempotencyKey) {
      dragLinkStore.cancel();
      return;
    }

    const idempotencyKey = snap.idempotencyKey;
    const styleOverride = event.detail.styleOverride;
    const tempId = `tmp_${idempotencyKey}`;

    function endpointForPost(
      endpoint: NonNullable<DragLinkSnapshot['source']>,
      selectedInterfaceIndex: number | undefined
    ) {
      if (endpoint.kind === 'network') {
        return { network_id: endpoint.networkId };
      }
      if (endpoint.kind === 'interface') {
        return { node_id: endpoint.nodeId, interface_index: endpoint.interfaceIndex };
      }
      if (typeof selectedInterfaceIndex !== 'number') {
        return null;
      }
      return { node_id: endpoint.nodeId, interface_index: selectedInterfaceIndex };
    }

    const fromEndpoint = endpointForPost(snap.source, event.detail.sourceInterfaceIndex);
    const toEndpoint = endpointForPost(snap.target, event.detail.targetInterfaceIndex);
    if (!fromEndpoint || !toEndpoint) {
      toastStore.push('Choose an unused interface for the new link.', 'error');
      return;
    }

    // The wire contract requires the node-side endpoint as ``from`` whenever
    // exactly one side is a node; flip the orientation if the user dragged
    // from a network onto an interface.
    let postFrom = fromEndpoint;
    let postTo = toEndpoint;
    if (
      snap.source.kind === 'network' &&
      (snap.target.kind === 'interface' || snap.target.kind === 'node-slot')
    ) {
      postFrom = toEndpoint;
      postTo = { network_id: snap.source.networkId };
    }

    // Optimistic insertion: append to $labState.links with a temporary id so the
    // edge renders immediately. The server response replaces the temp id.
    const optimisticLink: Link = {
      id: tempId,
      from: postFrom,
      to: postTo,
      style_override: styleOverride,
      label: '',
      color: '',
      width: '1',
    };

    labState.setLinks([...$labState.links, optimisticLink]);
    publishFlowState();
    dragLinkStore.cancel();
    dragLinkSourceAnchor = null;

    try {
      const response = await apiRequest<LinkCreateResponse>(`/labs/${labId}/links`, {
        method: 'POST',
        headers: { 'Idempotency-Key': idempotencyKey },
        body: {
          from: postFrom,
          to: postTo,
          style_override: styleOverride,
        },
        suppressToast: true,
      });
      const created = createdFromLinkResponse(response);
      adoptRevision(response);

      // The lab can only be patched in place from a complete result. A
      // node-to-node link is stored as an implicit bridge with two halves, but
      // the response carries just the half that was requested — the other
      // arrives over the WebSocket, usually before this response. Adopting
      // the bridge without both halves would paint it as a broken (orphaned)
      // bridge until the peer lands. So when the result is incomplete, let the
      // lab reload pick it up; the optimistic line stays on screen until the
      // snapshot replaces it, so the link never blinks out.
      const bridge = created.network;
      const incomplete =
        !created.link || (bridge?.implicit === true && !hasPeerHalf(bridge.id, created.link.id));
      if (incomplete || !created.link) {
        dispatchCanvasChange('link-create-reload', {
        });
        return;
      }

      labState.applyCreatedLink(created.link, created.network, tempId);
      publishFlowState();
      dispatchCanvasChange('topology', {
      });
    } catch (error) {
      labState.setLinks($labState.links.filter((entry) => entry.id !== tempId));
      publishFlowState();
      const message =
        error instanceof ApiError
          ? error.message
          : error instanceof Error
            ? error.message
            : 'Failed to create link';
      toastStore.push(message, 'error');
    }
  }

  function handleLinkConfirmCancel() {
    dragLinkStore.cancel();
    dragLinkSourceAnchor = null;
  }

  // ── lifecycle: WS hub flush hook & cleanup ───────────────────────────────
  let wsClient: WsClient | null = null;
  let labWsStores: LabWsStores | null = null;
  /** Unsubscribe handle for the unified linkReconciliation store (US-403/404). */
  let reconUnsub: (() => void) | null = null;
  /** #153: unsubscribe handle for the per-link mutation forwarder. */
  let linkChangeUnsub: (() => void) | null = null;

  onMount(() => {
    if (typeof window !== 'undefined') {
      window.addEventListener('mousemove', handleWindowMouseMove);
      window.addEventListener('mouseup', handleWindowMouseUp);
      window.addEventListener('keydown', handleWindowKeyDown);
      // Closing the tab inside the 500ms debounce window used to silently
      // lose the last drag. keepalive fetch survives page teardown.
      window.addEventListener('beforeunload', handleBeforeUnloadFlush);
    }

    if (typeof window !== 'undefined') {
      const client = createWsClient({ labId });
      wsClient = client;
      labWsStores = createLabWsStores(client);
      client.on('lab_topology', (_msg: WsMessage) => {
        void layoutDebouncer.flush();
        // lab_topology resets the reconciliation store (done inside labWs);
        // republish edges so overlays clear from the canvas immediately.
        publishFlowState();
      });
      // Subscribe to the unified reconciliation store so the canvas repaints
      // whenever a discovered_link or link_divergent WS event lands.
      // Skip the synchronous first emission (store fires on subscribe before
      // any WS events have arrived; publishFlowState already ran via $:).
      let primed = false;
      reconUnsub = labWsStores.linkReconciliation.subscribe(() => {
        if (!primed) {
          primed = true;
          return;
        }
        publishFlowState();
      });
      // #153: forward per-link mutation events up to the page so it can
      // splice links[] reactively. The page is the source of truth for
      // links[]; the canvas just relays.
      linkChangeUnsub = labWsStores.onLinkChange((event) => {
        dispatch('link-change', event);
      });
      if (import.meta.env.DEV) {
        (window as unknown as { __novaWsClient?: WsClient }).__novaWsClient = client;
      }
      client.connect();
    }

    return () => {
      if (typeof window !== 'undefined') {
        window.removeEventListener('mousemove', handleWindowMouseMove);
        window.removeEventListener('mouseup', handleWindowMouseUp);
        window.removeEventListener('keydown', handleWindowKeyDown);
        window.removeEventListener('beforeunload', handleBeforeUnloadFlush);
      }
      void layoutDebouncer.flush();
      if (reconUnsub) {
        reconUnsub();
        reconUnsub = null;
      }
      if (linkChangeUnsub) {
        linkChangeUnsub();
        linkChangeUnsub = null;
      }
      if (wsClient) {
        wsClient.close();
        if (typeof window !== 'undefined' && import.meta.env.DEV) {
          delete (window as unknown as { __novaWsClient?: WsClient }).__novaWsClient;
        }
        wsClient = null;
        labWsStores = null;
      }
    };
  });

  onDestroy(() => {
    canvasDestroyed = true;
    clearTopologyChangeBanner();
  });

  export async function flushPendingLayout(): Promise<void> {
    await layoutDebouncer.flush();
  }

  /**
   * Positions still queued/in-flight to PUT /layout. The page passes these
   * to labState.applySnapshot so a server snapshot racing a (possibly
   * failed-and-requeued) layout save cannot adopt pre-drag coordinates.
   */
  export function pendingLayoutIds(): { nodeIds: Set<number>; networkIds: Set<number> } {
    return layoutDebouncer.pendingPositionIds();
  }

  function handleBeforeUnloadFlush() {
    layoutDebouncer.flushKeepalive();
  }

  async function handleNodeModalSubmit(
    event: CustomEvent<
      | {
          mode: 'create';
          payload: {
            type: NodeData['type'];
            template: string;
            image: string;
            name_prefix: string;
            count: number;
            placement: 'grid' | 'row';
            icon: string;
            cpu: number;
            ram: number;
            ethernet: number;
            console: NodeData['console'];
            second_console: NonNullable<NodeData['secondConsole']> | null;
            delay: number;
            extras: Record<string, unknown>;
          };
        }
      | {
          mode: 'create-paired';
          payload: {
            template_key: string;
            name_overrides: Record<string, string>;
          };
        }
      | {
          mode: 'edit';
          nodeId: number;
          payload: {
            name: string;
            image: string;
            icon: string;
            cpu: number;
            ram: number;
            ethernet: number;
            console: NodeData['console'];
            second_console: NonNullable<NodeData['secondConsole']> | null;
            delay: number;
            extras: Record<string, unknown>;
          };
        }
    >
  ) {
    nodeModalSubmitting = true;
    try {
      if (event.detail.mode === 'create') {
        const response = await apiRequest<NodeBatchCreateResult>(`/labs/${labId}/nodes/batch`, {
          method: 'POST',
          body: {
            ...event.detail.payload,
            left: Math.round(nodeCreateAnchor.x),
            top: Math.round(nodeCreateAnchor.y),
          }
        });
        const nextNodes = { ...$labState.nodes };
        for (const node of response.data.nodes) {
          nextNodes[String(node.id)] = node;
        }
        labState.setNodes(nextNodes);
        adoptRevision(response);
        publishFlowState();
        dispatchCanvasChange('node-create', {
        });
      } else if (event.detail.mode === 'create-paired') {
        await apiRequest<{ nodes: NodeData[]; links: unknown[] }>(
          `/labs/${labId}/nodes/from-paired-template`,
          {
            method: 'POST',
            body: {
              template_key: event.detail.payload.template_key,
              name_overrides: event.detail.payload.name_overrides,
              base_left: Math.round(nodeCreateAnchor.x),
              base_top: Math.round(nodeCreateAnchor.y),
            }
          }
        );
        // Paired creation adds 2+ nodes plus an implicit bridge network and
        // the auto-link halves; route through the parent's full lab refresh
        // path so $labState.networks/$labState.topology stay consistent.
        dispatchCanvasChange('paired-create', {});
      } else {
        const response = await apiRequest<NodeData>(`/labs/${labId}/nodes/${event.detail.nodeId}`, {
          method: 'PUT',
          body: event.detail.payload,
        });
        labState.setNodes({
          ...$labState.nodes,
          [String(response.data.id)]: response.data,
        });
        adoptRevision(response);
        publishFlowState();
        dispatchCanvasChange('node-edit', {
          nodeId: response.data.id,
          node: deepClone(response.data),
        });
      }

      nodeModalOpen = false;
      nodeModalNode = null;
    } finally {
      nodeModalSubmitting = false;
    }
  }
</script>

<div class="relative h-full w-full flex-1" class:zen-mode={zenMode}>
  <SvelteFlow
    nodes={nodesStore}
    edges={edgesStore}
    {nodeTypes}
    {edgeTypes}
    fitView
    nodesConnectable={!canvasLocked && !zenMode}
    nodesDraggable={!canvasLocked}
    elementsSelectable={!canvasLocked}
    colorMode="dark"
    minZoom={0.1}
    maxZoom={2}
    panOnDrag={!canvasLocked}
    panOnScroll={!canvasLocked}
    panActivationKey={canvasLocked ? null : undefined}
    zoomOnScroll={!canvasLocked}
    zoomOnPinch={!canvasLocked}
    zoomOnDoubleClick={!canvasLocked}
    zoomActivationKey={canvasLocked ? null : undefined}
    selectionOnDrag={!canvasLocked}
    autoPanOnConnect={!canvasLocked}
    autoPanOnNodeDrag={!canvasLocked}
    defaultEdgeOptions={{ type: 'default' }}
    proOptions={{ hideAttribution: true }}
    on:nodedragstop={handleNodeDragStop}
    on:moveend={(event: CustomEvent<LabViewport>) => handleViewportEnd(event)}
    on:edgeclick={(event: CustomEvent<{ edge: Edge }>) => {
      selectedEdgeId = event.detail.edge.id;
      closeMenu();
      closeAddMenu();
    }}
    on:edgecontextmenu={(event: CustomEvent<{ edge: Edge; event: MouseEvent | TouchEvent }>) => {
      event.detail.event.preventDefault();
      if (zenMode) return;
      const point = eventPoint(event.detail.event);
      selectedEdgeId = event.detail.edge.id;
      closeAddMenu();
      menu = {
        x: point.x,
        y: point.y,
        targetId: event.detail.edge.id,
        targetType: 'edge'
      };
    }}
    on:nodecontextmenu={(event: CustomEvent<{ node: Node; event: MouseEvent | TouchEvent }>) => {
      event.detail.event.preventDefault();
      const point = eventPoint(event.detail.event);
      closeAddMenu();
      menu = {
        x: point.x,
        y: point.y,
        targetId: event.detail.node.id,
        targetType: event.detail.node.id.startsWith('network') ? 'network' : 'node'
      };
    }}
    on:nodemouseenter={(event: CustomEvent<{ node: Node }>) => {
      // #275: no hover focus while a wire drag is choosing a target —
      // the drag's own highlight owns the visual then.
      if (!dragLinkSourceAnchor) {
        setHoverFocus(event.detail.node.id);
      }
    }}
    on:nodemouseleave={() => {
      scheduleHoverClear();
    }}
    on:paneclick={() => {
      selectedEdgeId = null;
      closeMenu();
      closeAddMenu();
    }}
  >
    <Background patternColor="#374151" gap={20} />
    <!-- #273: the minimap starts collapsed to a small floating box (same
         style as the top-right legend). Expanding swaps the minimap INTO
         that corner spot — the box is replaced by the minimap itself,
         which carries its own minimize control. The choice persists per
         user. Both states sit close to the corner to cover less canvas. -->
    {#if $canvasPrefs.minimapExpanded}
      <div class="canvas-minimap-anchor" data-testid="minimap-frame">
        <MiniMap
          nodeColor={nodeColor}
          maskColor="rgba(17, 24, 39, 0.7)"
          class="canvas-minimap"
        />
        <button
          type="button"
          class="canvas-minimap-minimize"
          data-testid="minimap-minimize"
          aria-label="Hide minimap"
          title="Hide minimap"
          on:click={() => canvasPrefs.setMinimapExpanded(false)}
        >
          <Minus class="h-3 w-3" />
        </button>
      </div>
    {:else}
      <Panel position="bottom-right" class="minimap-chip-panel">
        <button
          type="button"
          class="inline-flex items-center gap-1.5 rounded-xl border border-gray-700 bg-gray-900/95 px-2.5 py-2 text-xs font-normal text-gray-300 shadow-lg shadow-black/20 backdrop-blur transition-colors hover:border-blue-500 hover:text-white"
          data-testid="minimap-toggle"
          aria-label="Show minimap"
          title="Show minimap"
          on:click={() => canvasPrefs.setMinimapExpanded(true)}
        >
          <MapIcon class="h-3.5 w-3.5 text-gray-400" />
          <span>Minimap</span>
          <span class="text-[9px] text-gray-500">▴</span>
        </button>
      </Panel>
    {/if}

    <Panel position="top-left">
      <div class="rounded-full border border-gray-800 bg-gray-900/95 px-3 py-1.5 text-[10px] uppercase tracking-[0.05em] text-gray-500 shadow-lg shadow-black/20">
        {#if saveState === 'saving'}
          Saving topology…
        {:else if saveState === 'saved'}
          Topology saved
        {:else}
          Topology editor
        {/if}
      </div>
    </Panel>

    <Panel position="top-right">
      <div class="flex flex-col items-end gap-2">
        <div class="space-y-1 rounded-xl border border-gray-700 bg-gray-900/95 px-2.5 py-2 text-xs shadow-lg shadow-black/20 backdrop-blur">
          <div class="flex items-center gap-1.5">
            <span class="h-2.5 w-2.5 rounded-full bg-emerald-500"></span>
            <span class="text-gray-200">Running</span>
          </div>
          <div class="flex items-center gap-1.5">
            <span class="h-2.5 w-2.5 rounded-full bg-gray-500"></span>
            <span class="text-gray-200">Stopped</span>
          </div>
          <div class="flex items-center gap-1.5">
            <span class="h-2.5 w-2.5 rounded-full bg-blue-500"></span>
            <span class="text-gray-200">Network</span>
          </div>
        </div>
        {#if consoleSelectorWindows.length}
          <div class="rounded-xl border border-gray-700 bg-gray-900/95 px-2.5 py-2 text-xs shadow-lg shadow-black/20 backdrop-blur">
            <div class="text-[9px] uppercase tracking-[0.18em] text-gray-500">Console selector</div>
            <div class="mt-1.5 flex max-w-[14rem] flex-col items-stretch gap-1">
              {#each consoleSelectorWindows as window (window.id)}
                <button
                  type="button"
                  class={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-left text-[10px] transition ${window.isFront && !window.isMinimized ? 'border-emerald-400/60 bg-emerald-500/15 text-emerald-100' : 'border-gray-700 bg-gray-950/80 text-gray-300 hover:border-blue-500 hover:text-white'}`}
                  aria-label={`Bring ${window.nodeName} console to front`}
                  title={window.isMinimized ? `Restore ${window.nodeName}` : `Bring ${window.nodeName} to front`}
                  on:click={() => dispatch('console-select', { tabId: window.id })}
                >
                  <span class={`inline-block h-1.5 w-1.5 rounded-full ${window.isMinimized ? 'bg-amber-300' : 'bg-emerald-400'}`}></span>
                  <span class="truncate">{window.nodeName}</span>
                </button>
              {/each}
            </div>
          </div>
        {/if}
        {#each consoleMinimizedWindows as window (window.id)}
          <div class="flex items-center gap-2.5 rounded-2xl border border-gray-700 bg-gray-900/95 px-3 py-1 shadow-lg shadow-black/20 backdrop-blur">
            <div class="flex items-center gap-2">
              <button
                type="button"
                class="h-3 w-3 rounded-full bg-red-400 transition hover:bg-red-300"
                aria-label={`Close ${window.nodeName} console`}
                on:click={() => dispatch('console-close', { tabId: window.id })}
              ></button>
              <button
                type="button"
                class="h-3 w-3 rounded-full bg-amber-300 transition hover:bg-amber-200"
                aria-label={`Restore ${window.nodeName} console`}
                on:click={() => dispatch('console-restore', { tabId: window.id })}
              ></button>
              <button
                type="button"
                class="h-3 w-3 rounded-full bg-emerald-400 transition hover:bg-emerald-300"
                aria-label={`Maximize ${window.nodeName} console`}
                on:click={() => dispatch('console-maximize', { tabId: window.id })}
              ></button>
            </div>
            <div class="flex items-baseline gap-2">
              <span class="text-[9px] uppercase tracking-[0.05em] text-gray-500">Console</span>
              <span class="whitespace-nowrap text-sm font-semibold leading-none text-gray-100">{window.nodeName}</span>
            </div>
          </div>
        {/each}
      </div>
    </Panel>

    <Panel position="center-left" class="lab-controls-panel">
      <div class="relative flex items-center">
        <div
          role="group"
          aria-label="Lab controls"
          aria-busy={labNodeAction !== null}
          class="flex flex-col gap-1 rounded-2xl border border-gray-700 bg-gray-900/95 p-1 shadow-2xl shadow-black/30 backdrop-blur"
        >
          <button
            type="button"
            class={labControlButtonClass}
            disabled={labNodeAction !== null || !canStartLabNodes}
            aria-label="Start all nodes"
            title="Start all nodes"
            on:click={() => runLabNodeAction('start')}
          >
            {#if labNodeAction?.action === 'start'}
              <LoaderCircle class="h-5 w-5 animate-spin" aria-hidden="true" />
            {:else}
              <Play class="h-5 w-5" aria-hidden="true" />
            {/if}
          </button>
          <button
            type="button"
            class={labControlButtonClass}
            disabled={labNodeAction !== null || !canStopLabNodes}
            aria-label="Stop all nodes"
            title="Stop all nodes"
            on:click={() => runLabNodeAction('stop')}
          >
            {#if labNodeAction?.action === 'stop'}
              <LoaderCircle class="h-5 w-5 animate-spin" aria-hidden="true" />
            {:else}
              <Square class="h-5 w-5" aria-hidden="true" />
            {/if}
          </button>
          <button
            type="button"
            class={labControlButtonClass}
            disabled
            aria-label="Restart all nodes"
            title="Restart all nodes (unavailable)"
          >
            <RotateCcw class="h-5 w-5" aria-hidden="true" />
          </button>
          <button
            type="button"
            class={labControlButtonClass}
            disabled
            aria-label="Open all consoles"
            title="Open all consoles (unavailable)"
          >
            <Monitor class="h-5 w-5" aria-hidden="true" />
          </button>
        </div>
        <div
          role="status"
          class="pointer-events-none absolute left-full ml-2 whitespace-nowrap"
        >
          {#if labNodeAction}
            <span class="rounded-xl border border-gray-700 bg-gray-900/95 px-2.5 py-2 text-xs text-gray-200 shadow-lg">
              {labNodeAction.action === 'start' ? 'Starting' : 'Stopping'} nodes
              ({labNodeAction.completed}/{labNodeAction.total})
            </span>
          {/if}
        </div>
      </div>
    </Panel>

    <Panel position="bottom-left">
      <div class="flex items-end gap-3">
        <div class="flex flex-col gap-1 rounded-2xl border border-gray-700 bg-gray-900/95 p-1 shadow-2xl shadow-black/30 backdrop-blur">
          <button
            type="button"
            class="inline-flex h-8 w-8 items-center justify-center rounded-lg border border-gray-800 bg-gray-950/80 text-gray-200 transition hover:border-blue-500/40 hover:text-white disabled:cursor-not-allowed disabled:opacity-40"
            on:click={handleZoomIn}
            title="zoom in"
            aria-label="zoom in"
            disabled={canvasLocked}
          >
            <ZoomIn class="h-5 w-5" />
          </button>
          <button
            type="button"
            class="inline-flex h-8 w-8 items-center justify-center rounded-lg border border-gray-800 bg-gray-950/80 text-gray-200 transition hover:border-blue-500/40 hover:text-white disabled:cursor-not-allowed disabled:opacity-40"
            on:click={handleZoomOut}
            title="zoom out"
            aria-label="zoom out"
            disabled={canvasLocked}
          >
            <ZoomOut class="h-5 w-5" />
          </button>
          <button
            type="button"
            class="inline-flex h-8 w-8 items-center justify-center rounded-lg border border-gray-800 bg-gray-950/80 text-gray-200 transition hover:border-blue-500/40 hover:text-white disabled:cursor-not-allowed disabled:opacity-40"
            on:click={handleFitView}
            title="center content"
            aria-label="center content"
            disabled={canvasLocked}
          >
            <LocateFixed class="h-5 w-5" />
          </button>
          <button
            type="button"
            class="inline-flex h-8 w-8 items-center justify-center rounded-lg border border-gray-800 bg-gray-950/80 text-gray-200 transition hover:border-blue-500/40 hover:text-white"
            on:click={toggleCanvasLock}
            title={canvasLocked ? 'unlock canvas' : 'lock canvas'}
            aria-label={canvasLocked ? 'unlock canvas' : 'lock canvas'}
          >
            {#if canvasLocked}
              <Lock class="h-5 w-5" />
            {:else}
              <LockOpen class="h-5 w-5" />
            {/if}
          </button>
        </div>

        {#if addMenuStep !== 'closed'}
          <div class="w-72 overflow-hidden rounded-2xl border border-gray-700 bg-gray-900/95 shadow-2xl shadow-black/30 backdrop-blur">
            <div class="flex items-center justify-between border-b border-gray-800 px-3 py-2">
              <div class="flex items-center gap-2">
                {#if addMenuStep === 'item'}
                  <button
                    type="button"
                    class="inline-flex h-7 w-7 items-center justify-center rounded-md border border-gray-800 bg-gray-950/80 text-gray-300 transition hover:border-blue-500/40 hover:text-white"
                    aria-label="Back to add categories"
                    on:click={() => {
                      addMenuStep = 'kind';
                      addMenuKind = null;
                    }}
                  >
                    <ChevronLeft class="h-4 w-4" />
                  </button>
                {/if}
                <div class="text-[10px] uppercase tracking-[0.05em] text-gray-500">
                  {#if addMenuStep === 'kind'}
                    Add element
                  {:else}
                    Choose network
                  {/if}
                </div>
              </div>
              <button
                type="button"
                class="inline-flex h-7 w-7 items-center justify-center rounded-md border border-gray-800 bg-gray-950/80 text-gray-300 transition hover:border-blue-500/40 hover:text-white"
                aria-label="Close add menu"
                on:click={closeAddMenu}
              >
                ×
              </button>
            </div>

            <div class="max-h-80 space-y-2 overflow-y-auto p-2">
              {#if addMenuStep === 'kind'}
                <button
                  type="button"
                  class="block w-full rounded-xl border border-gray-800 bg-gray-950/80 px-3 py-2.5 text-left transition hover:border-blue-500/40 hover:bg-gray-900"
                  on:click={() => openAddKind('node')}
                >
                  <div class="text-[11px] font-medium text-gray-100">Node</div>
                  <div class="mt-1 text-[10px] uppercase tracking-[0.05em] text-gray-500">
                    Choose a template and image
                  </div>
                </button>
                <button
                  type="button"
                  class="block w-full rounded-xl border border-gray-800 bg-gray-950/80 px-3 py-2.5 text-left transition hover:border-blue-500/40 hover:bg-gray-900"
                  on:click={() => openAddKind('network')}
                >
                  <div class="text-[11px] font-medium text-gray-100">Network</div>
                  <div class="mt-1 text-[10px] uppercase tracking-[0.05em] text-gray-500">
                    Choose a network container type
                  </div>
                </button>
              {:else}
                {#if networkPaletteItems.length === 0}
                  <div class="rounded-xl border border-gray-800 bg-gray-950/80 px-3 py-2 text-[11px] text-gray-500">
                    No network types are available.
                  </div>
                {:else}
                  {#each networkPaletteItems as item}
                    <button
                      type="button"
                      class="block w-full rounded-xl border border-gray-800 bg-gray-950/80 px-3 py-2.5 text-left transition hover:border-blue-500/40 hover:bg-gray-900"
                      on:click={() => addPaletteItem(item)}
                    >
                      <div class="text-[11px] font-medium text-gray-100">{item.title}</div>
                      <div class="mt-1 text-[10px] uppercase tracking-[0.05em] text-gray-500">{item.subtitle}</div>
                    </button>
                  {/each}
                {/if}
              {/if}
            </div>
          </div>
        {:else}
          <button
            type="button"
            class="inline-flex h-12 w-12 items-center justify-center rounded-full border border-blue-500/40 bg-blue-500/15 text-blue-100 shadow-2xl shadow-black/30 transition hover:bg-blue-500/25"
            aria-label="Open add element menu"
            aria-expanded="false"
            on:click={toggleAddMenu}
          >
            <Plus class="h-5 w-5" />
          </button>
        {/if}
      </div>
    </Panel>
  </SvelteFlow>

  {#if !zenMode && selectedEdgeId}
    <div class="absolute bottom-4 left-4 z-20 rounded-xl border border-gray-700 bg-gray-900/95 p-3 text-xs text-gray-200 shadow-lg shadow-black/20 backdrop-blur">
      <div class="text-[10px] uppercase tracking-[0.05em] text-gray-500">Selected Link</div>
      <button
        type="button"
        class="mt-3 rounded-md border border-red-400/40 px-3 py-2 uppercase tracking-[0.05em] text-red-100 hover:bg-red-500/10"
        on:click={() => {
          if (selectedEdgeId) {
            deleteLink(selectedEdgeId);
          }
          selectedEdgeId = null;
        }}
      >
        Delete Link
      </button>
    </div>
  {/if}

  {#if menu && (!zenMode || menu.targetType === 'node' || menu.targetType === 'network')}
    <div
      role="menu"
      tabindex="-1"
      class="fixed z-30 min-w-44 rounded-lg border border-gray-700 bg-gray-900/95 p-1 shadow-xl shadow-black/30 backdrop-blur"
      use:anchoredMenu={{ x: menu.x, y: menu.y }}
      on:keydown|stopPropagation={() => {}}
      on:mousedown|stopPropagation
      on:mouseup|stopPropagation
      on:click|stopPropagation
    >
      <div class="px-1.5 py-0.5 text-[9px] uppercase tracking-[0.22em] text-gray-500">
        {menu.targetType === 'node' ? 'Node actions' : menu.targetType === 'network' ? 'Network actions' : menu.targetType === 'port' ? 'Connection point' : 'Link actions'}
      </div>
      {#if menu?.targetType === 'node'}
        <button type="button" class="menu-item" on:click|preventDefault|stopPropagation={() => handleNodeAction('start', menu?.targetId ?? '')}>
          <span>Start</span><Play class="h-3.5 w-3.5 text-gray-400" />
        </button>
        <button type="button" class="menu-item" on:click|preventDefault|stopPropagation={() => handleNodeAction('stop', menu?.targetId ?? '')}>
          <span>Stop</span><Square class="h-3.5 w-3.5 text-gray-400" />
        </button>
        <button type="button" class="menu-item" on:click|preventDefault|stopPropagation={() => handleNodeAction('wipe', menu?.targetId ?? '')}>
          <span>Wipe</span><Eraser class="h-3.5 w-3.5 text-gray-400" />
        </button>
        <!-- #268: one clickable row per console session — the primary, plus
             a "2nd" row when the node declares a secondary console. The
             console type rides as small right-aligned text next to the
             icon (fits the menu width; the old pill buttons were too wide
             and left the row itself unclickable). -->
        <button
          type="button"
          class="menu-item"
          data-testid="node-menu-console-primary"
          on:click|preventDefault|stopPropagation={() => handleNodeAction('console', menu?.targetId ?? '', 'primary')}
        >
          <span>Console</span>
          <span class="flex items-center gap-1.5">
            {#if menuNode}
              <span class="text-[9px] uppercase tracking-wide text-gray-500">{consolePillLabel(menuNode.console)}</span>
            {/if}
            <Monitor class="h-3.5 w-3.5 text-gray-400" />
          </span>
        </button>
        {#if menuNode?.secondConsole}
          <button
            type="button"
            class="menu-item"
            data-testid="node-menu-console-secondary"
            on:click|preventDefault|stopPropagation={() => handleNodeAction('console', menu?.targetId ?? '', 'second')}
          >
            <span>2nd Console</span>
            <span class="flex items-center gap-1.5">
              <span class="text-[9px] uppercase tracking-wide text-gray-500">{consolePillLabel(menuNode.secondConsole)}</span>
              <Monitor class="h-3.5 w-3.5 text-gray-400" />
            </span>
          </button>
        {/if}
        <button
          type="button"
          class="menu-item"
          data-testid="node-menu-recordings"
          on:click|preventDefault|stopPropagation={() => handleNodeAction('recordings', menu?.targetId ?? '')}
        >
          <span>Recordings</span><ListVideo class="h-3.5 w-3.5 text-gray-400" aria-hidden="true" />
        </button>
        <button type="button" class="menu-item" on:click|preventDefault|stopPropagation={() => handleEditNode(menu?.targetId ?? '')}>
          <span>Edit Node</span><Pencil class="h-3.5 w-3.5 text-gray-400" />
        </button>
        <button type="button" class="menu-item text-red-200" on:click|preventDefault|stopPropagation={() => handleNodeAction('delete', menu?.targetId ?? '')}>
          <span>Delete Node</span><Trash2 class="h-3.5 w-3.5 text-red-200/80" />
        </button>
      {:else if menu?.targetType === 'network'}
        <button
          type="button"
          class="menu-item"
          data-testid="network-menu-info"
          on:click|preventDefault|stopPropagation={() => handleNetworkInfo(menu?.targetId ?? '')}
        >
          <span>Network Info</span><Info class="h-3.5 w-3.5 text-gray-400" aria-hidden="true" />
        </button>
        {#if menuNetwork?.type === 'nat_cloud'}
          <button
            type="button"
            class="menu-item disabled:cursor-not-allowed disabled:opacity-50"
            data-testid="network-menu-configure"
            disabled={menuSharing?.role === 'member'}
            title={menuSharing?.role === 'member'
              ? 'Settings are managed by the lab that owns this network.'
              : 'DHCP and name settings'}
            on:click|preventDefault|stopPropagation={() => handleNetworkConfigure(menu?.targetId ?? '')}
          >
            <span>Configure…</span>
            <span class="flex items-center gap-1.5">
              {#if menuSharing?.role !== 'member'}
                <span class="text-[9px] uppercase tracking-wide text-gray-500">
                  {menuNetwork.config?.dhcp === false ? 'DHCP off' : 'DHCP on'}
                </span>
              {/if}
              <Settings2 class="h-3.5 w-3.5 text-gray-400" aria-hidden="true" />
            </span>
          </button>
        {/if}
        {#if menuSharing}
          <button
            type="button"
            class="menu-item disabled:cursor-not-allowed disabled:opacity-50"
            role="menuitemcheckbox"
            aria-checked={menuSharing.role !== null}
            data-testid="network-menu-sharing"
            disabled={!menuSharing.canToggle}
            title={menuSharing.hint}
            on:click|preventDefault|stopPropagation={() =>
              handleNetworkSharing(menu?.targetId ?? '', menuSharing?.role !== 'owner')}
          >
            <span>{menuSharing.role === 'member' ? 'Shared from another lab' : 'Make shared'}</span>
            {#if menuSharing.role}
              <Check class="h-3.5 w-3.5 text-sky-300" aria-hidden="true" />
            {:else}
              <Share2 class="h-3.5 w-3.5 text-gray-400" aria-hidden="true" />
            {/if}
          </button>
        {/if}
        <button type="button" class="menu-item text-red-200" on:click|preventDefault|stopPropagation={() => handleNetworkDelete(menu?.targetId ?? '')}>
          <span>Delete Network</span><Trash2 class="h-3.5 w-3.5 text-red-200/80" />
        </button>
      {:else if menu?.targetType === 'port'}
        <!-- #258: connection points are auto-positioned; no port menu actions.
             This branch stays so right-clicking a port shows an empty menu
             instead of falling through to the edge line-style menu. -->
        <div class="px-2 pt-1 pb-0.5 text-[10px] uppercase tracking-widest text-gray-500">
          Auto-positioned
        </div>
      {:else}
        {#if menu && isDiscoveredEdgeId(menu.targetId)}
          <button
            type="button"
            class="menu-item"
            on:click|preventDefault|stopPropagation={() => {
              if (menu?.targetId) {
                void promoteDiscoveredLink(menu.targetId);
              }
              selectedEdgeId = null;
              closeMenu();
            }}
          >
            <span>Promote to declared link</span><Plus class="h-3.5 w-3.5 text-amber-300/80" />
          </button>
        {:else}
          <div class="px-2 pt-1 pb-0.5 text-[10px] uppercase tracking-widest text-gray-500">Line style</div>
          {#each ([['straight', 'Straight'], ['bezier', 'Curved'], ['orthogonal', 'Orthogonal']] as const) as [styleVal, styleLabel]}
            <button
              type="button"
              class="menu-item"
              on:click|preventDefault|stopPropagation={() => {
                if (menu?.targetId) setLinkStyle(menu.targetId, styleVal);
                closeMenu();
              }}
            >
              <span>{styleLabel}</span>
            </button>
          {/each}
          <div class="mx-2 my-1 border-t border-gray-700"></div>
          <button
            type="button"
            class="menu-item text-red-200"
            on:click|preventDefault|stopPropagation={() => {
              if (menu?.targetId) {
                deleteLink(menu.targetId);
              }
              selectedEdgeId = null;
              closeMenu();
            }}
          >
            <span>Delete Link</span><Trash2 class="h-3.5 w-3.5 text-red-200/80" />
          </button>
        {/if}
      {/if}
    </div>
  {/if}

  {#if !zenMode && topologyChangeBanner}
    <div class="pointer-events-none absolute inset-x-4 top-4 z-40 flex justify-center" data-testid="topology-hotplug-banner">
      <div class="pointer-events-auto flex w-full max-w-xl items-start gap-3 rounded-2xl border border-amber-700/60 bg-amber-950/90 px-4 py-3 text-sm text-amber-100 shadow-2xl shadow-black/30 backdrop-blur">
        <div class="min-w-0 flex-1">
          <div class="text-[10px] uppercase tracking-[0.05em] text-amber-300/80">Topology change blocked</div>
          <div class="mt-1 font-medium">{topologyChangeBanner.message}</div>
        </div>
        <button
          type="button"
          class="inline-flex h-8 w-8 items-center justify-center rounded-full border border-amber-600/40 bg-amber-950/80 text-amber-100 transition hover:border-amber-400/70 hover:text-white"
          aria-label="Dismiss topology change banner"
          data-testid="topology-hotplug-banner-close"
          on:click={clearTopologyChangeBanner}
        >
          ×
        </button>
      </div>
    </div>
  {/if}

  <div hidden={zenMode} inert={zenMode}>
    <LinkPreview sourceAnchor={dragLinkSourceAnchor} />
    <LinkConfirmModal
      suspended={zenMode}
      nodes={$labState.nodes}
      links={$labState.links}
      on:confirm={handleLinkConfirm}
      on:cancel={handleLinkConfirmCancel}
    />
  </div>
  <div hidden={hideNetworkModal} inert={hideNetworkModal}>
    <NetworkConfigModal
      {labId}
      suspended={hideNetworkModal}
      open={networkModalOpen}
      defaultName={networkModalDefaultName}
      defaultType={networkModalDefaultType}
      mode={editingNetwork ? 'edit' : 'create'}
      network={editingNetwork}
      saving={networkSaving}
      saveError={networkSaveError}
      on:confirm={onNetworkModalConfirm}
      on:cancel={onNetworkModalCancel}
    />
  </div>

  {#if !zenMode && selectedPortInfo}
    {#if selectedPortInfo.kind === 'interface'}
      <PortInfoPopover
        kind="interface"
        interfaceName={selectedPortInfo.interfaceName}
        plannedMac={selectedPortInfo.plannedMac}
        liveMac={getLiveMacFor(selectedPortInfo.nodeId, selectedPortInfo.interfaceIndex)}
        peerLabel={resolveInterfacePeerLabel(
          selectedPortInfo.nodeId,
          selectedPortInfo.interfaceIndex,
        )}
        status="unknown"
        speed={null}
        mtu={null}
        duplex={null}
        anchorRect={selectedPortInfo.anchorRect}
        placement={($labState.nodes[String(selectedPortInfo.nodeId)]?.interfaces?.[selectedPortInfo.interfaceIndex]
          ?.port_position?.side ?? 'right') as 'top' | 'right' | 'bottom' | 'left'}
        onClose={closePortInfo}
      />
    {:else}
      <PortInfoPopover
        kind="network"
        interfaceName={selectedPortInfo.networkName ?? `network ${selectedPortInfo.networkId}`}
        plannedMac={null}
        liveMac={null}
        peerLabel={resolveNetworkPeerLabel(selectedPortInfo.networkId)}
        status="unknown"
        speed={null}
        mtu={null}
        duplex={null}
        anchorRect={selectedPortInfo.anchorRect}
        placement={selectedPortInfo.side}
        onClose={closePortInfo}
      />
    {/if}
  {/if}

  <div hidden={hideNodeModal} inert={hideNodeModal}>
    <NodeConfigModal
      open={nodeModalOpen}
      mode={nodeModalMode}
      catalog={nodeCatalog}
      node={nodeModalNode}
      submitting={nodeModalSubmitting}
      labPath={labId}
      on:cancel={() => {
        nodeModalOpen = false;
        nodeModalNode = null;
      }}
      on:submit={handleNodeModalSubmit}
    />
  </div>
</div>

<style>
  .zen-mode :global([data-port-node-id]),
  .zen-mode :global([data-network-port-id]) {
    pointer-events: none;
  }

  @media (max-height: 560px) {
    :global(.svelte-flow__panel.lab-controls-panel) {
      margin-left: 3.75rem;
    }
  }

  :global(.canvas-minimap) {
    overflow: hidden;
    border-radius: 0.75rem;
    border: 1px solid rgb(55 65 81 / 1);
    background: rgb(17 24 39 / 0.92);
    box-shadow: 0 20px 40px -20px rgb(0 0 0 / 0.65);
  }

  /* #273: expanded, the minimap takes the corner spot itself (the toggle
     box is swapped out) with its own minimize control; both states hug
     the corner closer than the library default 15px so less canvas is
     covered. The anchor is the positioned ancestor the minimap's own
     absolute panel attaches to. */
  :global(.minimap-chip-panel) {
    margin: 8px;
  }

  :global(.canvas-minimap-anchor) {
    position: absolute;
    right: 8px;
    bottom: 8px;
    z-index: 5;
  }

  :global(.canvas-minimap-anchor .svelte-flow__panel) {
    position: static;
    margin: 0;
  }

  :global(.canvas-minimap-minimize) {
    position: absolute;
    top: 4px;
    right: 4px;
    z-index: 6;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    height: 1.15rem;
    width: 1.15rem;
    border-radius: 0.375rem;
    border: 1px solid rgb(55 65 81 / 1);
    background: rgb(17 24 39 / 0.92);
    color: rgb(209 213 219 / 1);
    transition:
      color 120ms,
      border-color 120ms;
  }

  :global(.canvas-minimap-minimize:hover) {
    color: rgb(255 255 255 / 1);
    border-color: rgb(96 165 250 / 1);
  }

  /* #275 (revision): hover focus pulses only the related set's outer card
     outlines (#c0c0c0 <-> #606060). Outline — not border or box-shadow — so
     the #274 run-state ring and the card's layout/hit area stay untouched. */
  :global {
    @keyframes nv-focus-pulse {
      0%,
      100% {
        outline-color: #c0c0c0;
      }
      50% {
        outline-color: #606060;
      }
    }
  }

  :global(.svelte-flow__node.nv-focus-pulse) :global(.nb-card) {
    outline: 2px solid #c0c0c0;
    outline-offset: 2px;
    animation: nv-focus-pulse 2s ease-in-out infinite;
  }

  :global(.canvas-minimap svg) {
    background: transparent;
  }

  .menu-item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem;
    width: 100%;
    border-radius: 0.375rem;
    padding: 0.35rem 0.55rem;
    text-align: left;
    font-size: 0.7rem;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    color: rgb(229 231 235);
  }

  .menu-item:hover {
    background: rgba(59, 130, 246, 0.12);
  }
</style>
