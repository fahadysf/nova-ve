<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  import { createEventDispatcher, onDestroy, setContext } from 'svelte';
  import { onMount } from 'svelte';
  import { writable, get } from 'svelte/store';
  import {
    ChevronLeft,
    Eraser,
    LocateFixed,
    Lock,
    LockOpen,
    Monitor,
    Pencil,
    Play,
    Plus,
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
  import CustomNode from '$lib/components/canvas/CustomNode.svelte';
  import NodeConfigModal from '$lib/components/canvas/NodeConfigModal.svelte';
  import NetworkNode from '$lib/components/canvas/NetworkNode.svelte';
  import LinkEdge from '$lib/components/canvas/LinkEdge.svelte';
  import LinkPreview from '$lib/components/canvas/LinkPreview.svelte';
  import LinkConfirmModal from '$lib/components/canvas/LinkConfirmModal.svelte';
  import PortInfoPopover from '$lib/components/canvas/PortInfoPopover.svelte';
  import { selectedPortInfoStore, type SelectedPortInfo } from '$lib/stores/portInfo';
  import { toastStore } from '$lib/stores/toasts';
  import { createLayoutDebouncer, type LayoutDebouncer } from '$lib/services/labApi';
  import { createWsClient, type WsClient, type WsMessage } from '$lib/services/wsClient';
  import { createLabWsStores, type LabWsStores } from '$lib/stores/labWs';
  import { deriveEdges, parseIfaceInterfaceIndex, type DiscoveredLink } from '$lib/services/canvasEdges';
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
    LinkReconciliation,
    LinkStyle,
    LiveMacState,
    NetworkData,
    NodeBatchCreateResult,
    NodeCatalog,
    NodeData,
    PortPosition,
    TemplateCapabilities,
    TopologyLink
  } from '$lib/types';

  export let labId: string;
  export let nodes: Record<string, NodeData> = {};
  export let networks: Record<string, NetworkData> = {};
  export let topology: TopologyLink[] = [];
  /** v2 links[] — the source of truth for edges (US-082). */
  export let links: Link[] = [];
  /** v2 lab defaults (notably ``link_style`` for edge routing). */
  export let defaults: LabDefaults = { link_style: 'orthogonal' };
  export let consoleSelectorWindows: ConsoleSelectorWindow[] = [];
  export let consoleMinimizedWindows: ConsoleMinimizedBar[] = [];

  type PaletteNetworkItem = {
    kind: 'network';
    title: string;
    subtitle: string;
    networkType: string;
  };
  type PaletteItem = PaletteNetworkItem;
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

  const dispatch = createEventDispatcher<{
    console: { nodeId: number; node: NodeData };
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
  }>();

  const nodeTypes = {
    device: CustomNode,
    network: NetworkNode
  } as unknown as Record<string, never>;
  const edgeTypes = {
    link: LinkEdge
  } as unknown as Record<string, never>;
  const { fitView, screenToFlowPosition, zoomIn, zoomOut } = useSvelteFlow();
  const layoutDebouncer: LayoutDebouncer = createLayoutDebouncer(labId, {
    delayMs: 500,
    onError: (err) => {
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
  let topologyChangeBanner:
    | {
        message: string;
        nodeNames: string[];
      }
    | null = null;
  let topologyChangeBannerTimer: ReturnType<typeof setTimeout> | null = null;

  $: selectedPortInfo = $selectedPortInfoStore;

  function resolveInterfacePeerLabel(nodeId: number, interfaceIndex: number): string | null {
    for (const link of localLinks) {
      const fromMatches =
        link.from?.node_id === nodeId && link.from?.interface_index === interfaceIndex;
      const toMatches =
        link.to?.node_id === nodeId && link.to?.interface_index === interfaceIndex;
      if (!fromMatches && !toMatches) continue;
      const other = fromMatches ? link.to : link.from;
      if (!other) continue;
      if (typeof other.network_id === 'number') {
        const network = localNetworks[String(other.network_id)];
        return network?.name ?? `network ${other.network_id}`;
      }
      if (typeof other.node_id === 'number') {
        const peerNode = localNodes[String(other.node_id)];
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
    for (const link of localLinks) {
      const fromMatches = link.from?.network_id === networkId;
      const toMatches = link.to?.network_id === networkId;
      if (!fromMatches && !toMatches) continue;
      const other = fromMatches ? link.to : link.from;
      if (!other) continue;
      if (typeof other.node_id === 'number') {
        const peerNode = localNodes[String(other.node_id)];
        const nodeLabel = peerNode?.name ?? `node ${other.node_id}`;
        const peerIface =
          peerNode && typeof other.interface_index === 'number'
            ? peerNode.interfaces[other.interface_index]
            : undefined;
        peers.push(peerIface ? `${nodeLabel} · ${peerIface.name}` : nodeLabel);
      } else if (typeof other.network_id === 'number') {
        const network = localNetworks[String(other.network_id)];
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

  let localNodes: Record<string, NodeData> = {};
  let localNetworks: Record<string, NetworkData> = {};
  let localTopology: TopologyLink[] = [];
  let localLinks: Link[] = [];
  let localDefaults: LabDefaults = { link_style: 'orthogonal' };
  let lastNodesRef = nodes;
  let lastNetworksRef = networks;
  let lastTopologyRef = topology;
  let lastLinksRef = links;
  let lastDefaultsRef = defaults;
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
  let nodeActionSequence = 0;
  const nodeActionTokens = new Map<number, number>();
  const nodeActionPollSchedule = [0, 250, 500, 1000, 2000];
  let menu:
    | {
        x: number;
        y: number;
        targetId: string;
        targetType: 'node' | 'network' | 'edge';
      }
    | null = null;

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
    if (node.capabilities) {
      return node.capabilities;
    }

    const templateCapabilities = nodeCatalog?.templates.find(
      (template) => template.key === node.template && template.type === node.type
    )?.capabilities;
    return templateCapabilities ?? defaultTemplateCapabilities;
  }

  function resolveEndpointNode(endpoint: DragEndpoint | null): NodeData | null {
    if (endpoint?.kind !== 'interface') {
      return null;
    }
    return localNodes[String(endpoint.nodeId)] ?? null;
  }

  function blockedTopologyChangeNodes(snapshot: DragLinkSnapshot): NodeData[] {
    const seenNodeIds = new Set<number>();
    const candidateNodes = [resolveEndpointNode(snapshot.source), resolveEndpointNode(snapshot.target)];

    return candidateNodes.filter((node): node is NodeData => {
      if (!node || seenNodeIds.has(node.id)) {
        return false;
      }
      seenNodeIds.add(node.id);
      const capabilities = resolveNodeCapabilities(node);
      return node.status === 2 && capabilities.hotplug === false;
    });
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
    token: number,
    action: 'start' | 'stop' | 'wipe',
    targetStatus: 0 | 2,
    transientStatus: 'starting' | 'stopping'
  ) {
    for (const delayMs of nodeActionPollSchedule) {
      if (nodeActionTokens.get(nodeId) !== token) {
        return false;
      }

      if (delayMs > 0) {
        await pause(delayMs);
      }

      const response = await apiRequest<Record<string, NodeData>>(`/labs/${labId}/nodes`, {
        suppressToast: true,
      });
      const refreshedNode = response.data?.[String(nodeId)];
      if (!refreshedNode) {
        return false;
      }

      const nextNode =
        refreshedNode.status === targetStatus
          ? refreshedNode
          : { ...refreshedNode, transientStatus };

      localNodes = {
        ...localNodes,
        [String(nodeId)]: nextNode,
      };
      publishFlowState();
      dispatchCanvasChange(
        refreshedNode.status === targetStatus ? `node-${action}` : `node-${action}-pending`,
        {
          nodeId,
          node: deepClone(nextNode),
          nodes: deepClone(localNodes),
        }
      );

      if (refreshedNode.status === targetStatus) {
        return true;
      }
    }

    return false;
  }

  function syncLocalState() {
    localNodes = deepClone(nodes);
    localNetworks = deepClone(networks);
    localTopology = deepClone(topology);
    localLinks = deepClone(links);
    localDefaults = deepClone(defaults);
  }

  function publishFlowState() {
    nodesStore.set(buildFlowNodes());
    edgesStore.set(buildFlowEdges());
  }

  $: if (
    nodes !== lastNodesRef ||
    networks !== lastNetworksRef ||
    topology !== lastTopologyRef ||
    links !== lastLinksRef ||
    defaults !== lastDefaultsRef
  ) {
    lastNodesRef = nodes;
    lastNetworksRef = networks;
    lastTopologyRef = topology;
    lastLinksRef = links;
    lastDefaultsRef = defaults;
    syncLocalState();
    publishFlowState();
  }

  $: if (
    !Object.keys(localNodes).length &&
    !Object.keys(localNetworks).length &&
    !localTopology.length &&
    !localLinks.length
  ) {
    syncLocalState();
  }

  $: networkPaletteItems = paletteItems.filter(
    (item): item is PaletteNetworkItem => item.kind === 'network'
  );

  function buildFlowNodes(): Node[] {
    const flowNodes: Node[] = [];

    for (const node of Object.values(localNodes)) {
      flowNodes.push({
        id: `node${node.id}`,
        type: 'device',
        position: { x: node.left, y: node.top },
        data: {
          label: node.name,
          icon: node.icon,
          status: node.status,
          transientStatus: node.transientStatus,
          type: node.type,
          template: node.template,
          console: node.console,
          nodeId: node.id,
          interfaces: node.interfaces ?? [],
          highlightedInterfaceIndex: highlightedTargetForNode(node.id)
        },
        style: `width: ${node.width ? parseInt(node.width, 10) + 60 : 104}px;`
      });
    }

    for (const network of Object.values(localNetworks)) {
      if (!network.visibility) continue;
      flowNodes.push({
        id: `network${network.id}`,
        type: 'network',
        position: { x: network.left || 100, y: network.top || 100 },
        data: {
          label: network.name,
          icon: network.icon,
          type: 'network',
          count: network.count ?? 0
        },
        style: `width: ${network.width ? network.width + 40 : 110}px;`
      });
    }

    return flowNodes;
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

    return deriveEdges(localLinks, localDefaults, discoveredList, divergentByLinkId);
  }

  $: {
    publishFlowState();
  }

  function closeMenu() {
    menu = null;
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
  }

  function openEditNodeModal(targetId: string) {
    const decoded = decodeId(targetId);
    if (!decoded || decoded.type !== 'node') {
      return;
    }

    const node = localNodes[String(decoded.id)];
    if (!node) {
      return;
    }

    closeMenu();
    nodeModalMode = 'edit';
    nodeModalNode = deepClone(node);
    nodeModalOpen = true;
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
      paletteItems = [
        { kind: 'network', title: 'Bridge Network', subtitle: 'bridge', networkType: 'bridge' }
      ];
    } catch (_error) {
      paletteItems = [{ kind: 'network', title: 'Bridge Network', subtitle: 'bridge', networkType: 'bridge' }];
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
        await apiRequest(`/labs/${labId}/topology`, {
          method: 'PUT',
          body: {
            topology: localTopology,
            nodes: localNodes,
            networks: localNetworks
          }
        });
        saveState = 'saved';
      } catch (_error) {
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
    const nextNetworks = { ...localNetworks };

    for (const node of Object.values(localNodes)) {
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

    localNetworks = nextNetworks;
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

    if (decoded.type === 'node' && localNodes[String(decoded.id)]) {
      localNodes[String(decoded.id)].left = left;
      localNodes[String(decoded.id)].top = top;
      localNodes = { ...localNodes };
      layoutDebouncer.pushNodePosition(decoded.id, left, top);
    }

    if (decoded.type === 'network' && localNetworks[String(decoded.id)]) {
      localNetworks[String(decoded.id)].left = left;
      localNetworks[String(decoded.id)].top = top;
      localNetworks = { ...localNetworks };
      layoutDebouncer.pushNetworkPosition(decoded.id, left, top);
    }
  }

  function handleViewportEnd(event: CustomEvent<LabViewport>) {
    const viewport = event.detail;
    if (!viewport) return;
    layoutDebouncer.pushViewport(viewport);
  }

  function getEdgeIndex(edgeId: string): number {
    const prefix = 'link:';
    if (edgeId.startsWith(prefix)) {
      const linkId = edgeId.slice(prefix.length);
      return localLinks.findIndex((entry) => entry.id === linkId);
    }
    return buildFlowEdges().findIndex((edge) => edge.id === edgeId);
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
      const response = await apiRequest<Link>(`/labs/${labId}/links`, {
        method: 'POST',
        body: { from: fromEndpoint, to: toEndpoint },
        suppressToast: true,
      });
      const serverLink = response.data;
      if (serverLink && serverLink.id) {
        localLinks = [...localLinks, serverLink];
      }
      // Optimistically remove the discovered overlay immediately (HIGH-1).
      // The next discovery cycle would also clear it, but removing it now
      // avoids a duplicate amber+gray edge flash while the cycle completes.
      if (labWsStores) {
        labWsStores.deleteReconciliation(`iface:${iface}`);
      }
      publishFlowState();
      dispatchCanvasChange('topology', {
        nodes: deepClone(localNodes),
        networks: deepClone(localNetworks),
        topology: deepClone(localTopology),
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

  function deleteLink(edgeId: string) {
    const index = getEdgeIndex(edgeId);
    if (index < 0) {
      return;
    }

    const v2Link = localLinks[index];
    if (v2Link) {
      if (
        typeof v2Link.from?.node_id === 'number' &&
        typeof v2Link.from.interface_index === 'number'
      ) {
        const node = localNodes[String(v2Link.from.node_id)];
        const iface = node?.interfaces[v2Link.from.interface_index];
        if (iface) iface.network_id = 0;
      }
      if (
        typeof v2Link.to?.node_id === 'number' &&
        typeof v2Link.to.interface_index === 'number'
      ) {
        const node = localNodes[String(v2Link.to.node_id)];
        const iface = node?.interfaces[v2Link.to.interface_index];
        if (iface) iface.network_id = 0;
      }
    }

    localNodes = { ...localNodes };
    if (index < localTopology.length) {
      localTopology = localTopology.filter((_, currentIndex) => currentIndex !== index);
    }
    localLinks = localLinks.filter((_, currentIndex) => currentIndex !== index);
    recalculateNetworks();
    publishFlowState();
    scheduleSave();
    dispatchCanvasChange('topology', {
      nodes: deepClone(localNodes),
      networks: deepClone(localNetworks),
      topology: deepClone(localTopology),
    });
  }

  function setLinkStyle(edgeId: string, style: LinkStyle | null) {
    const index = getEdgeIndex(edgeId);
    if (index < 0) return;
    localLinks = localLinks.map((link, i) =>
      i === index ? { ...link, style_override: style } : link
    );
    if (index < localTopology.length) {
      localTopology = localTopology.map((tl, i) =>
        i === index ? { ...tl, style_override: style } : tl
      );
    }
    publishFlowState();
    scheduleSave();
    dispatchCanvasChange('topology', {
      nodes: deepClone(localNodes),
      networks: deepClone(localNetworks),
      topology: deepClone(localTopology),
    });
  }

  async function handleNodeAction(action: 'start' | 'stop' | 'wipe' | 'delete' | 'console', targetId: string) {
    closeMenu();
    const decoded = decodeId(targetId);
    if (!decoded || decoded.type !== 'node') {
      return;
    }

    const node = localNodes[String(decoded.id)];
    if (!node) {
      return;
    }

    if (action === 'console') {
      dispatch('console', { nodeId: decoded.id, node });
      return;
    }

    if (action === 'delete') {
      await apiRequest(`/labs/${labId}/nodes/${decoded.id}`, { method: 'DELETE' });
      const nextNodes = { ...localNodes };
      delete nextNodes[String(decoded.id)];
      localNodes = nextNodes;
      localTopology = localTopology.filter(
        (link) => link.source !== `node${decoded.id}` && link.destination !== `node${decoded.id}`
      );
      recalculateNetworks();
      publishFlowState();
      scheduleSave();
      dispatchCanvasChange('node-delete', {
        nodeId: decoded.id,
        nodes: deepClone(localNodes),
        networks: deepClone(localNetworks),
        topology: deepClone(localTopology),
      });
      return;
    }

    if (action === 'start') {
      node.transientStatus = 'starting';
    }
    if (action === 'stop' || action === 'wipe') {
      node.transientStatus = 'stopping';
    }
    const token = ++nodeActionSequence;
    nodeActionTokens.set(decoded.id, token);
    localNodes = { ...localNodes };
    publishFlowState();
    dispatchCanvasChange(`node-${action}-pending`, {
      nodeId: decoded.id,
      node: deepClone(node),
      nodes: deepClone(localNodes),
    });

    try {
      await apiRequest(`/labs/${labId}/nodes/${decoded.id}/${action}`);
      const targetStatus = action === 'start' ? 2 : 0;
      const settled = await settleNodeAction(
        decoded.id,
        token,
        action,
        targetStatus,
        action === 'start' ? 'starting' : 'stopping'
      );
      if (settled) {
        nodeActionTokens.delete(decoded.id);
        return;
      }

      delete node.transientStatus;
      localNodes = { ...localNodes };
      publishFlowState();
      dispatchCanvasChange('node-edit', {
        nodeId: decoded.id,
        node: deepClone(node),
        nodes: deepClone(localNodes),
      });
      nodeActionTokens.delete(decoded.id);
    } catch (error) {
      delete node.transientStatus;
      localNodes = { ...localNodes };
      publishFlowState();
      dispatchCanvasChange('node-edit', {
        nodeId: decoded.id,
        node: deepClone(node),
        nodes: deepClone(localNodes),
      });
      nodeActionTokens.delete(decoded.id);
      throw error;
    }
  }

  function handleEditNode(targetId: string) {
    openEditNodeModal(targetId);
  }

  async function handleNetworkDelete(targetId: string) {
    closeMenu();
    const decoded = decodeId(targetId);
    if (!decoded || decoded.type !== 'network') {
      return;
    }

    await apiRequest(`/labs/${labId}/networks/${decoded.id}`, { method: 'DELETE' });
    const nextNetworks = { ...localNetworks };
    delete nextNetworks[String(decoded.id)];
    localNetworks = nextNetworks;
    localTopology = localTopology.filter((link) => link.network_id !== decoded.id);
    for (const node of Object.values(localNodes)) {
      for (const iface of node.interfaces) {
        if (iface.network_id === decoded.id) {
          iface.network_id = 0;
        }
      }
    }
    localNodes = { ...localNodes };
    publishFlowState();
    scheduleSave();
    dispatchCanvasChange('network-delete', {
      nodes: deepClone(localNodes),
      networks: deepClone(localNetworks),
      topology: deepClone(localTopology),
    });
  }

  async function createNetworkAt(position: { x: number; y: number }, networkType = 'bridge') {
    const response = await apiRequest<NetworkData>(`/labs/${labId}/networks`, {
      method: 'POST',
      body: {
        name: `net-${Object.keys(localNetworks).length + 1}`,
        type: networkType,
        left: Math.round(position.x),
        top: Math.round(position.y)
      }
    });
    localNetworks = {
      ...localNetworks,
      [String(response.data.id)]: response.data
    };
    publishFlowState();
    dispatchCanvasChange('network-create', {
      networks: deepClone(localNetworks),
    });
  }

  async function addPaletteItem(item: PaletteItem) {
    const viewport = typeof window !== 'undefined'
      ? screenToFlowPosition(
          { x: Math.round(window.innerWidth * 0.5), y: Math.round(window.innerHeight * 0.5) },
          { snapToGrid: true }
        )
      : { x: 200, y: 200 };

    if (item.kind === 'network') {
      await createNetworkAt(viewport, item.networkType);
      return;
    }

    closeAddMenu();
  }

  setContext('nova-ve:port-position-persist', (
    nodeId: number,
    interfaceIndex: number,
    port: PortPosition
  ) => {
    void persistPortPosition(nodeId, interfaceIndex, port);
  });

  async function persistPortPosition(
    nodeId: number,
    interfaceIndex: number,
    port: PortPosition
  ) {
    const node = localNodes[String(nodeId)];
    if (!node) return;
    const iface = node.interfaces[interfaceIndex];
    if (!iface) return;

    const previous = iface.port_position ?? null;
    iface.port_position = port;
    localNodes = { ...localNodes };
    publishFlowState();

    try {
      await apiRequest(`/labs/${labId}/nodes/${nodeId}/interfaces/${interfaceIndex}`, {
        method: 'PATCH',
        body: { port_position: port },
        suppressToast: true,
      });
    } catch (error) {
      iface.port_position = previous;
      localNodes = { ...localNodes };
      publishFlowState();
      const message = error instanceof Error ? error.message : 'Failed to persist port position';
      toastStore.push(message, 'error');
    }
  }

  function highlightedTargetForNode(candidateNodeId: number): number | null {
    const snap = getDragLinkSnapshot();
    const target = snap.target;
    if (target?.kind === 'interface' && target.nodeId === candidateNodeId) {
      return target.interfaceIndex;
    }
    return null;
  }

  function handleWindowMouseMove(event: MouseEvent) {
    const snap = getDragLinkSnapshot();
    if (snap.state === 'idle' || snap.state === 'confirming') return;
    dragLinkStore.move({ pointer: { x: event.clientX, y: event.clientY } });
  }

  function handleWindowMouseUp(event: MouseEvent) {
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
    if (event.key !== 'Escape') return;
    const snap = getDragLinkSnapshot();
    if (snap.state !== 'idle') {
      dragLinkStore.cancel();
      dragLinkSourceAnchor = null;
    }
  }

  async function handleLinkConfirm(event: CustomEvent<{ styleOverride: LinkStyle }>) {
    const snap = getDragLinkSnapshot();
    if (!snap.source || !snap.target || !snap.idempotencyKey) {
      dragLinkStore.cancel();
      return;
    }

    const idempotencyKey = snap.idempotencyKey;
    const styleOverride = event.detail.styleOverride;
    const tempId = `tmp_${idempotencyKey}`;

    const fromEndpoint =
      snap.source.kind === 'interface'
        ? { node_id: snap.source.nodeId, interface_index: snap.source.interfaceIndex }
        : { network_id: snap.source.networkId };
    const toEndpoint =
      snap.target.kind === 'interface'
        ? { node_id: snap.target.nodeId, interface_index: snap.target.interfaceIndex }
        : { network_id: snap.target.networkId };

    // The wire contract requires the node-side endpoint as ``from`` whenever
    // exactly one side is a node; flip the orientation if the user dragged
    // from a network onto an interface.
    let postFrom = fromEndpoint;
    let postTo = toEndpoint;
    if (
      snap.source.kind === 'network' &&
      snap.target.kind === 'interface'
    ) {
      postFrom = { node_id: snap.target.nodeId, interface_index: snap.target.interfaceIndex };
      postTo = { network_id: snap.source.networkId };
    }

    // Optimistic insertion: append to localLinks with a temporary id so the
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

    localLinks = [...localLinks, optimisticLink];
    publishFlowState();
    dragLinkStore.cancel();
    dragLinkSourceAnchor = null;

    try {
      const response = await apiRequest<Link>(`/labs/${labId}/links`, {
        method: 'POST',
        headers: { 'Idempotency-Key': idempotencyKey },
        body: {
          from: postFrom,
          to: postTo,
          style_override: styleOverride,
        },
        suppressToast: true,
      });
      const serverLink = response.data;
      if (serverLink && serverLink.id) {
        localLinks = localLinks.map((entry) => (entry.id === tempId ? serverLink : entry));
      }
      publishFlowState();
      dispatchCanvasChange('topology', {
        nodes: deepClone(localNodes),
        networks: deepClone(localNetworks),
        topology: deepClone(localTopology),
      });
    } catch (error) {
      localLinks = localLinks.filter((entry) => entry.id !== tempId);
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

  onMount(() => {
    if (typeof window !== 'undefined') {
      window.addEventListener('mousemove', handleWindowMouseMove);
      window.addEventListener('mouseup', handleWindowMouseUp);
      window.addEventListener('keydown', handleWindowKeyDown);
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
      }
      void layoutDebouncer.flush();
      if (reconUnsub) {
        reconUnsub();
        reconUnsub = null;
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
    clearTopologyChangeBanner();
  });

  export async function flushPendingLayout(): Promise<void> {
    await layoutDebouncer.flush();
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
            delay: number;
            extras: Record<string, unknown>;
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
        const nextNodes = { ...localNodes };
        for (const node of response.data.nodes) {
          nextNodes[String(node.id)] = node;
        }
        localNodes = nextNodes;
        publishFlowState();
        dispatchCanvasChange('node-create', {
          nodes: deepClone(localNodes),
        });
      } else {
        const response = await apiRequest<NodeData>(`/labs/${labId}/nodes/${event.detail.nodeId}`, {
          method: 'PUT',
          body: event.detail.payload,
        });
        localNodes = {
          ...localNodes,
          [String(response.data.id)]: response.data,
        };
        publishFlowState();
        dispatchCanvasChange('node-edit', {
          nodeId: response.data.id,
          node: deepClone(response.data),
          nodes: deepClone(localNodes),
        });
      }

      nodeModalOpen = false;
      nodeModalNode = null;
    } finally {
      nodeModalSubmitting = false;
    }
  }
</script>

<div class="relative h-full w-full flex-1">
  <SvelteFlow
    nodes={nodesStore}
    edges={edgesStore}
    {nodeTypes}
    {edgeTypes}
    fitView
    nodesConnectable={!canvasLocked}
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
    on:nodedragstop={handleNodeDragStop}
    on:moveend={(event: CustomEvent<LabViewport>) => handleViewportEnd(event)}
    on:edgeclick={(event: CustomEvent<{ edge: Edge }>) => {
      selectedEdgeId = event.detail.edge.id;
      closeMenu();
      closeAddMenu();
    }}
    on:edgecontextmenu={(event: CustomEvent<{ edge: Edge; event: MouseEvent | TouchEvent }>) => {
      event.detail.event.preventDefault();
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
    on:paneclick={() => {
      selectedEdgeId = null;
      closeMenu();
      closeAddMenu();
    }}
  >
    <Background patternColor="#374151" gap={20} />
    <MiniMap nodeColor={nodeColor} maskColor="rgba(17, 24, 39, 0.7)" class="canvas-minimap" />

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

  {#if selectedEdgeId}
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

  {#if menu}
    <div
      role="menu"
      tabindex="-1"
      class="fixed z-30 min-w-44 rounded-lg border border-gray-700 bg-gray-900/95 p-1 shadow-xl shadow-black/30 backdrop-blur"
      style={`left: ${menu.x}px; top: ${menu.y}px;`}
      on:keydown|stopPropagation={() => {}}
      on:mousedown|stopPropagation
      on:mouseup|stopPropagation
      on:click|stopPropagation
    >
      <div class="px-1.5 py-0.5 text-[9px] uppercase tracking-[0.22em] text-gray-500">
        {menu.targetType === 'node' ? 'Node actions' : menu.targetType === 'network' ? 'Network actions' : 'Link actions'}
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
        <button type="button" class="menu-item" on:click|preventDefault|stopPropagation={() => handleNodeAction('console', menu?.targetId ?? '')}>
          <span>Console</span><Monitor class="h-3.5 w-3.5 text-gray-400" />
        </button>
        <button type="button" class="menu-item" on:click|preventDefault|stopPropagation={() => handleEditNode(menu?.targetId ?? '')}>
          <span>Edit Node</span><Pencil class="h-3.5 w-3.5 text-gray-400" />
        </button>
        <button type="button" class="menu-item text-red-200" on:click|preventDefault|stopPropagation={() => handleNodeAction('delete', menu?.targetId ?? '')}>
          <span>Delete Node</span><Trash2 class="h-3.5 w-3.5 text-red-200/80" />
        </button>
      {:else if menu?.targetType === 'network'}
        <button type="button" class="menu-item text-red-200" on:click|preventDefault|stopPropagation={() => handleNetworkDelete(menu?.targetId ?? '')}>
          <span>Delete Network</span><Trash2 class="h-3.5 w-3.5 text-red-200/80" />
        </button>
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

  {#if topologyChangeBanner}
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

  <LinkPreview sourceAnchor={dragLinkSourceAnchor} />
  <LinkConfirmModal on:confirm={handleLinkConfirm} on:cancel={handleLinkConfirmCancel} />

  {#if selectedPortInfo}
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
        placement={(localNodes[String(selectedPortInfo.nodeId)]?.interfaces?.[selectedPortInfo.interfaceIndex]
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

<style>
  :global(.canvas-minimap) {
    overflow: hidden;
    border-radius: 0.75rem;
    border: 1px solid rgb(55 65 81 / 1);
    background: rgb(17 24 39 / 0.92);
    box-shadow: 0 20px 40px -20px rgb(0 0 0 / 0.65);
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
