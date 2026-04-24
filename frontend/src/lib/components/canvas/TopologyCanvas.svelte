<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { onMount } from 'svelte';
  import { writable } from 'svelte/store';
  import {
    Background,
    Controls,
    MiniMap,
    Panel,
    SvelteFlow,
    useSvelteFlow,
    type Connection,
    type Edge,
    type Node
  } from '@xyflow/svelte';
  import '@xyflow/svelte/dist/style.css';
  import { apiRequest } from '$lib/api';
  import CustomNode from '$lib/components/canvas/CustomNode.svelte';
  import NetworkNode from '$lib/components/canvas/NetworkNode.svelte';
  import { toastStore } from '$lib/stores/toasts';
  import type { NetworkData, NodeData, NodeInterface, TopologyLink } from '$lib/types';

  export let labId: string;
  export let nodes: Record<string, NodeData> = {};
  export let networks: Record<string, NetworkData> = {};
  export let topology: TopologyLink[] = [];

  const dispatch = createEventDispatcher<{
    console: { nodeId: number; node: NodeData };
  }>();

  const nodeTypes = {
    device: CustomNode,
    network: NetworkNode
  } as unknown as Record<string, never>;
  const { screenToFlowPosition } = useSvelteFlow();

  const nodesStore = writable<Node[]>([]);
  const edgesStore = writable<Edge[]>([]);

  let localNodes: Record<string, NodeData> = {};
  let localNetworks: Record<string, NetworkData> = {};
  let localTopology: TopologyLink[] = [];
  let lastNodesRef = nodes;
  let lastNetworksRef = networks;
  let lastTopologyRef = topology;
  let saveTimer: ReturnType<typeof setTimeout> | null = null;
  let saveState: 'idle' | 'saving' | 'saved' = 'idle';
  let selectedEdgeId: string | null = null;
  let paletteLoading = true;
  let paletteItems: Array<
    | { kind: 'node'; title: string; subtitle: string; type: string; template: string; image: string }
    | { kind: 'network'; title: string; subtitle: string; networkType: string }
  > = [];
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
    return '#6b7280';
  };

  function deepClone<T>(value: T): T {
    return JSON.parse(JSON.stringify(value)) as T;
  }

  function syncLocalState() {
    localNodes = deepClone(nodes);
    localNetworks = deepClone(networks);
    localTopology = deepClone(topology);
  }

  $: if (nodes !== lastNodesRef || networks !== lastNetworksRef || topology !== lastTopologyRef) {
    lastNodesRef = nodes;
    lastNetworksRef = networks;
    lastTopologyRef = topology;
    syncLocalState();
  }

  $: if (!Object.keys(localNodes).length && !Object.keys(localNetworks).length && !localTopology.length) {
    syncLocalState();
  }

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
          type: node.type,
          template: node.template
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
          type: 'network'
        },
        style: `width: ${network.width ? network.width + 40 : 110}px;`
      });
    }

    return flowNodes;
  }

  function edgeIdFor(link: TopologyLink, idx: number): string {
    return `edge:${link.source}:${link.destination}:${link.network_id}:${idx}`;
  }

  function buildFlowEdges(): Edge[] {
    return localTopology.map((link, idx) => ({
      id: edgeIdFor(link, idx),
      source: link.source,
      target: link.destination,
      label: link.source_label ? `${link.source_label}` : undefined,
      type: 'default',
      animated: false,
      style: `stroke: ${link.color || '#9ca3af'}; stroke-width: ${parseInt(link.width || '1', 10)}px;`,
      labelStyle: 'fill: #d1d5db; font-size: 10px;'
    }));
  }

  $: {
    nodesStore.set(buildFlowNodes());
    edgesStore.set(buildFlowEdges());
  }

  function closeMenu() {
    menu = null;
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
      const nodeItems: Array<(typeof paletteItems)[number]> = [];
      for (const templateType of ['qemu', 'docker']) {
        const templatesResponse = await apiRequest<Record<string, { name: string; template: string }>>(
          `/list/templates/${templateType}`,
          { suppressToast: true }
        );
        for (const template of Object.values(templatesResponse.data ?? {})) {
          const imagesResponse = await apiRequest<Record<string, { image: string }>>(
            `/list/images/${templateType}/${template.template}`,
            { suppressToast: true }
          );
          const firstImage = Object.values(imagesResponse.data ?? {})[0];
          if (!firstImage) {
            continue;
          }
          nodeItems.push({
            kind: 'node',
            title: template.name,
            subtitle: `${templateType} · ${firstImage.image}`,
            type: templateType,
            template: template.template,
            image: firstImage.image
          });
        }
      }
      paletteItems = [
        ...nodeItems,
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

  function nextNetworkId(): number {
    return Math.max(0, ...Object.keys(localNetworks).map((key) => Number(key))) + 1;
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

  function nextFreeInterface(node: NodeData): { index: number; interface: NodeInterface } | null {
    for (let index = 0; index < node.interfaces.length; index += 1) {
      if ((node.interfaces[index]?.network_id ?? 0) === 0) {
        return { index, interface: node.interfaces[index] };
      }
    }
    return null;
  }

  function recalculateNetworks() {
    const usage = new Map<number, number>();

    for (const node of Object.values(localNodes)) {
      for (const iface of node.interfaces ?? []) {
        if (iface.network_id > 0) {
          usage.set(iface.network_id, (usage.get(iface.network_id) ?? 0) + 1);
        }
      }
    }

    for (const [networkKey, network] of Object.entries(localNetworks)) {
      const count = usage.get(Number(networkKey)) ?? 0;
      network.count = count;
      if (!network.visibility && count === 0) {
        delete localNetworks[networkKey];
      }
    }
  }

  function pushLink(link: TopologyLink) {
    localTopology = [...localTopology, link];
    recalculateNetworks();
    scheduleSave();
  }

  function createNodeToNodeLink(sourceId: number, targetId: number) {
    const sourceNode = localNodes[String(sourceId)];
    const targetNode = localNodes[String(targetId)];
    if (!sourceNode || !targetNode) {
      return;
    }

    const sourceInterface = nextFreeInterface(sourceNode);
    const targetInterface = nextFreeInterface(targetNode);
    if (!sourceInterface || !targetInterface) {
      toastStore.push('No free ethernet interface is available on one of the selected nodes.');
      return;
    }

    const networkId = nextNetworkId();
    sourceNode.interfaces[sourceInterface.index].network_id = networkId;
    targetNode.interfaces[targetInterface.index].network_id = networkId;
    localNetworks[String(networkId)] = {
      id: networkId,
      name: `Net-${sourceNode.name}-${targetNode.name}`,
      type: 'bridge',
      left: 0,
      top: 0,
      icon: 'lan.png',
      visibility: 0,
      width: 0,
      style: 'Solid',
      linkstyle: 'Straight',
      color: '',
      label: '',
      smart: 0,
      count: 2
    };

    pushLink({
      type: 'ethernet',
      source: `node${sourceId}`,
      source_type: 'node',
      source_node_name: sourceNode.name,
      source_label: sourceInterface.interface.name,
      source_interfaceId: sourceInterface.index,
      source_suspend: 0,
      destination: `node${targetId}`,
      destination_type: 'node',
      destination_node_name: targetNode.name,
      destination_label: targetInterface.interface.name,
      destination_interfaceId: targetInterface.index,
      destination_suspend: 0,
      network_id: networkId,
      style: '',
      linkstyle: '',
      label: '',
      labelpos: '0.5',
      color: '',
      stub: '0',
      width: '1',
      curviness: '10',
      beziercurviness: '150',
      round: '0',
      midpoint: '0.5',
      srcpos: '0.15',
      dstpos: '0.85',
      source_delay: 0,
      source_loss: 0,
      source_bandwidth: 0,
      source_jitter: 0,
      destination_delay: 0,
      destination_loss: 0,
      destination_bandwidth: 0,
      destination_jitter: 0
    });
  }

  function createNodeToNetworkLink(nodeId: number, networkId: number, networkFirst = false) {
    const node = localNodes[String(nodeId)];
    const network = localNetworks[String(networkId)];
    if (!node || !network) {
      return;
    }

    const interfaceInfo = nextFreeInterface(node);
    if (!interfaceInfo) {
      toastStore.push(`No free ethernet interface is available on ${node.name}.`);
      return;
    }

    node.interfaces[interfaceInfo.index].network_id = networkId;

    pushLink({
      type: 'ethernet',
      source: networkFirst ? `network${networkId}` : `node${nodeId}`,
      source_type: networkFirst ? 'network' : 'node',
      source_node_name: networkFirst ? 'network' : node.name,
      source_label: networkFirst ? '' : interfaceInfo.interface.name,
      source_interfaceId: networkFirst ? 0 : interfaceInfo.index,
      source_suspend: 0,
      destination: networkFirst ? `node${nodeId}` : `network${networkId}`,
      destination_type: networkFirst ? 'node' : 'network',
      destination_node_name: networkFirst ? node.name : 'network',
      destination_label: networkFirst ? interfaceInfo.interface.name : '',
      destination_interfaceId: networkFirst ? interfaceInfo.index : 'network',
      destination_suspend: 0,
      network_id: networkId,
      style: '',
      linkstyle: '',
      label: '',
      labelpos: '0.5',
      color: '',
      stub: '0',
      width: '1',
      curviness: '10',
      beziercurviness: '150',
      round: '0',
      midpoint: '0.5',
      srcpos: '0.15',
      dstpos: '0.85',
      source_delay: 0,
      source_loss: 0,
      source_bandwidth: 0,
      source_jitter: 0,
      destination_delay: 0,
      destination_loss: 0,
      destination_bandwidth: 0,
      destination_jitter: 0
    });
  }

  function onConnect(connection: Connection) {
    const source = connection.source ? decodeId(connection.source) : null;
    const target = connection.target ? decodeId(connection.target) : null;

    if (!source || !target) {
      return;
    }

    if (source.type === 'network' && target.type === 'network') {
      toastStore.push('Direct network-to-network links are not supported.');
      return;
    }

    if (source.type === 'node' && target.type === 'node') {
      createNodeToNodeLink(source.id, target.id);
      return;
    }

    if (source.type === 'node' && target.type === 'network') {
      createNodeToNetworkLink(source.id, target.id, false);
      return;
    }

    createNodeToNetworkLink(target.id, source.id, true);
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

    if (decoded.type === 'node' && localNodes[String(decoded.id)]) {
      localNodes[String(decoded.id)].left = Math.round(movedNode.position.x);
      localNodes[String(decoded.id)].top = Math.round(movedNode.position.y);
    }

    if (decoded.type === 'network' && localNetworks[String(decoded.id)]) {
      localNetworks[String(decoded.id)].left = Math.round(movedNode.position.x);
      localNetworks[String(decoded.id)].top = Math.round(movedNode.position.y);
    }

    scheduleSave();
  }

  function getEdgeIndex(edgeId: string): number {
    return buildFlowEdges().findIndex((edge) => edge.id === edgeId);
  }

  function deleteLink(edgeId: string) {
    const index = getEdgeIndex(edgeId);
    if (index < 0) {
      return;
    }

    const link = localTopology[index];
    if (link.source_type === 'node') {
      const sourceNodeId = decodeId(link.source)?.id;
      if (sourceNodeId != null && localNodes[String(sourceNodeId)]?.interfaces[link.source_interfaceId]) {
        localNodes[String(sourceNodeId)].interfaces[link.source_interfaceId].network_id = 0;
      }
    }

    if (link.destination_type === 'node') {
      const destinationNodeId = decodeId(link.destination)?.id;
      const interfaceId = Number(link.destination_interfaceId);
      if (destinationNodeId != null && localNodes[String(destinationNodeId)]?.interfaces[interfaceId]) {
        localNodes[String(destinationNodeId)].interfaces[interfaceId].network_id = 0;
      }
    }

    localTopology = localTopology.filter((_, currentIndex) => currentIndex !== index);
    recalculateNetworks();
    scheduleSave();
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
      delete localNodes[String(decoded.id)];
      localTopology = localTopology.filter(
        (link) => link.source !== `node${decoded.id}` && link.destination !== `node${decoded.id}`
      );
      recalculateNetworks();
      return;
    }

    await apiRequest(`/labs/${labId}/nodes/${decoded.id}/${action}`);
    if (action === 'start') {
      node.status = 2;
    }
    if (action === 'stop' || action === 'wipe') {
      node.status = 0;
    }
  }

  async function handleNetworkDelete(targetId: string) {
    closeMenu();
    const decoded = decodeId(targetId);
    if (!decoded || decoded.type !== 'network') {
      return;
    }

    await apiRequest(`/labs/${labId}/networks/${decoded.id}`, { method: 'DELETE' });
    delete localNetworks[String(decoded.id)];
    localTopology = localTopology.filter((link) => link.network_id !== decoded.id);
    for (const node of Object.values(localNodes)) {
      for (const iface of node.interfaces) {
        if (iface.network_id === decoded.id) {
          iface.network_id = 0;
        }
      }
    }
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
    localNetworks[String(response.data.id)] = response.data;
  }

  async function createNodeAt(
    position: { x: number; y: number },
    payload: { type: string; template: string; image: string; title: string }
  ) {
    const response = await apiRequest<NodeData>(`/labs/${labId}/nodes`, {
      method: 'POST',
      body: {
        name: `${payload.title}-${Object.keys(localNodes).length + 1}`,
        type: payload.type,
        template: payload.template,
        image: payload.image,
        left: Math.round(position.x),
        top: Math.round(position.y)
      }
    });
    localNodes[String(response.data.id)] = response.data;
  }

  function paletteDragData(item: (typeof paletteItems)[number]): string {
    return JSON.stringify(item);
  }

  async function handleCanvasDrop(event: DragEvent) {
    event.preventDefault();
    const raw = event.dataTransfer?.getData('application/nova-ve-palette');
    if (!raw) {
      return;
    }

    const payload = JSON.parse(raw) as (typeof paletteItems)[number];
    const position = screenToFlowPosition({ x: event.clientX, y: event.clientY }, { snapToGrid: true });

    if (payload.kind === 'network') {
      await createNetworkAt(position, payload.networkType);
      return;
    }

    await createNodeAt(position, payload);
  }
</script>

<div class="relative h-full w-full flex-1">
  <SvelteFlow
    nodes={nodesStore}
    edges={edgesStore}
    {nodeTypes}
    fitView
    nodesConnectable
    onconnect={onConnect}
    colorMode="dark"
    minZoom={0.1}
    maxZoom={2}
    defaultEdgeOptions={{ type: 'default' }}
    on:nodedragstop={handleNodeDragStop}
    on:edgeclick={(event: CustomEvent<{ edge: Edge }>) => {
      selectedEdgeId = event.detail.edge.id;
      closeMenu();
    }}
    on:edgecontextmenu={(event: CustomEvent<{ edge: Edge; event: MouseEvent | TouchEvent }>) => {
      event.detail.event.preventDefault();
      const point = eventPoint(event.detail.event);
      selectedEdgeId = event.detail.edge.id;
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
    }}
    on:dragover={(event) => {
      event.preventDefault();
      if (event.dataTransfer) {
        event.dataTransfer.dropEffect = 'move';
      }
    }}
    on:drop={handleCanvasDrop}
  >
    <Background patternColor="#374151" gap={20} />
    <Controls />
    <MiniMap nodeColor={nodeColor} maskColor="rgba(17, 24, 39, 0.7)" />

    <Panel position="top-left">
      <div class="space-y-3 rounded-lg border border-gray-700 bg-gray-900/95 p-3 text-xs text-gray-300">
        <div class="uppercase tracking-[0.24em] text-gray-500">
          {#if saveState === 'saving'}
            Saving topology…
          {:else if saveState === 'saved'}
            Topology saved
          {:else}
            Topology editor
          {/if}
        </div>
        <div class="space-y-2 border-t border-gray-800 pt-3">
          <div class="text-[10px] uppercase tracking-[0.24em] text-gray-500">Palette</div>
          {#if paletteLoading}
            <div class="rounded-md border border-gray-800 bg-gray-950/80 px-3 py-2 text-[11px] text-gray-500">
              Loading templates…
            </div>
          {:else}
            {#each paletteItems as item}
              <button
                type="button"
                draggable="true"
                class="block w-56 rounded-md border border-gray-800 bg-gray-950/80 px-3 py-2 text-left hover:border-blue-500/40 hover:bg-gray-900"
                on:dragstart={(event) => {
                  event.dataTransfer?.setData('application/nova-ve-palette', paletteDragData(item));
                  if (event.dataTransfer) {
                    event.dataTransfer.effectAllowed = 'move';
                  }
                }}
              >
                <div class="font-medium text-gray-100">{item.title}</div>
                <div class="mt-1 text-[11px] uppercase tracking-[0.2em] text-gray-500">{item.subtitle}</div>
              </button>
            {/each}
          {/if}
        </div>
      </div>
    </Panel>

    <Panel position="top-right">
      <div class="space-y-1 rounded-lg border border-gray-700 bg-gray-800 p-3 text-xs">
        <div class="flex items-center gap-2">
          <span class="h-3 w-3 rounded-full bg-emerald-500"></span>
          <span>Running</span>
        </div>
        <div class="flex items-center gap-2">
          <span class="h-3 w-3 rounded-full bg-gray-500"></span>
          <span>Stopped</span>
        </div>
        <div class="flex items-center gap-2">
          <span class="h-3 w-3 rounded-full bg-blue-500"></span>
          <span>Network</span>
        </div>
        <div class="pt-2 text-[10px] uppercase tracking-[0.24em] text-gray-500">
          Drag nodes, connect handles, right-click for actions
        </div>
      </div>
    </Panel>
  </SvelteFlow>

  {#if selectedEdgeId}
    <div class="absolute bottom-4 left-4 z-20 rounded-xl border border-gray-700 bg-gray-900/95 p-3 text-xs text-gray-200 shadow-lg">
      <div class="text-[10px] uppercase tracking-[0.24em] text-gray-500">Selected Link</div>
      <button
        type="button"
        class="mt-3 rounded-md border border-red-400/40 px-3 py-2 uppercase tracking-[0.2em] text-red-100 hover:bg-red-500/10"
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
      class="fixed z-30 min-w-44 rounded-xl border border-gray-700 bg-gray-900/95 p-2 shadow-xl"
      style={`left: ${menu.x}px; top: ${menu.y}px;`}
    >
      {#if menu?.targetType === 'node'}
        <button class="menu-item" on:click={() => handleNodeAction('start', menu?.targetId ?? '')}>Start</button>
        <button class="menu-item" on:click={() => handleNodeAction('stop', menu?.targetId ?? '')}>Stop</button>
        <button class="menu-item" on:click={() => handleNodeAction('wipe', menu?.targetId ?? '')}>Wipe</button>
        <button class="menu-item" on:click={() => handleNodeAction('console', menu?.targetId ?? '')}>Console</button>
        <button class="menu-item text-red-200" on:click={() => handleNodeAction('delete', menu?.targetId ?? '')}>Delete Node</button>
      {:else if menu?.targetType === 'network'}
        <button class="menu-item text-red-200" on:click={() => handleNetworkDelete(menu?.targetId ?? '')}>Delete Network</button>
      {:else}
        <button
          class="menu-item text-red-200"
          on:click={() => {
            if (menu?.targetId) {
              deleteLink(menu.targetId);
            }
            selectedEdgeId = null;
            closeMenu();
          }}
        >
          Delete Link
        </button>
      {/if}
    </div>
  {/if}
</div>

<style>
  .menu-item {
    display: block;
    width: 100%;
    border-radius: 0.5rem;
    padding: 0.6rem 0.8rem;
    text-align: left;
    font-size: 0.75rem;
    color: rgb(229 231 235);
  }

  .menu-item:hover {
    background: rgba(59, 130, 246, 0.12);
  }
</style>
