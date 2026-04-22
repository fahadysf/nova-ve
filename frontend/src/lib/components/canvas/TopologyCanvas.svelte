<script lang="ts">
  import { writable } from 'svelte/store';
  import {
    SvelteFlow,
    Background,
    Controls,
    MiniMap,
    Position,
    Panel,
    useSvelteFlow,
    type Node,
    type Edge,
    type Connection,
  } from '@xyflow/svelte';
  import '@xyflow/svelte/dist/style.css';
  import type { NodeData, NetworkData, TopologyLink } from '$lib/types';

  export let nodes: Record<string, NodeData> = {};
  export let networks: Record<string, NetworkData> = {};
  export let topology: TopologyLink[] = [];

  const nodeColor = (n: Node) => {
    if (n.type === 'network') return '#3b82f6';
    if (n.data?.status === 2) return '#10b981';
    return '#6b7280';
  };

  function buildFlowNodes(): Node[] {
    const result: Node[] = [];

    // Add nodes
    for (const [key, n] of Object.entries(nodes)) {
      result.push({
        id: `node${n.id}`,
        type: 'default',
        position: { x: n.left, y: n.top },
        data: {
          label: n.name,
          icon: n.icon,
          status: n.status,
          type: n.type,
          template: n.template,
        },
        style: `width: ${n.width ? parseInt(n.width) + 60 : 80}px; height: 40px; background: ${n.status === 2 ? '#1f2937' : '#374151'}; border: ${n.status === 2 ? '1px solid #10b981' : '1px solid #6b7280'}; border-radius: 6px; color: #f3f4f6; font-size: 12px;`,
      });
    }

    // Add networks
    for (const [key, n] of Object.entries(networks)) {
      if (!n.visibility) continue;
      result.push({
        id: `network${n.id}`,
        type: 'default',
        position: { x: n.left || 100, y: n.top || 100 },
        data: {
          label: n.name,
          icon: n.icon,
          type: 'network',
        },
        style: `width: ${n.width ? n.width + 40 : 80}px; height: 40px; background: #1e3a5f; border: 1px solid #3b82f6; border-radius: 20px; color: #bfdbfe; font-size: 11px;`,
      });
    }

    return result;
  }

  function buildFlowEdges(): Edge[] {
    return topology.map((link, idx) => ({
      id: `e${idx}`,
      source: link.source,
      target: link.destination,
      label: link.source_label ? `${link.source_label}` : undefined,
      type: 'default',
      animated: false,
      style: `stroke: ${link.color || '#9ca3af'}; stroke-width: ${parseInt(link.width || '1')}px;`,
      labelStyle: 'fill: #d1d5db; font-size: 10px;',
    }));
  }

  const nodesStore = writable<Node[]>([]);
  const edgesStore = writable<Edge[]>([]);

  $: {
    nodesStore.set(buildFlowNodes());
    edgesStore.set(buildFlowEdges());
  }

  function onConnect(conn: Connection) {
    // TODO: create new link via API
    console.log('connect', conn);
  }
</script>

<div class="flex-1 w-full h-full relative">
  <SvelteFlow
    nodes={nodesStore}
    edges={edgesStore}
    fitView
    onconnect={onConnect}
    colorMode="dark"
    minZoom={0.1}
    maxZoom={2}
    defaultEdgeOptions={{ type: 'default' }}
  >
    <Background patternColor="#374151" gap={20} />
    <Controls />
    <MiniMap nodeColor={nodeColor} maskColor="rgba(17, 24, 39, 0.7)" />

    <Panel position="top-right">
      <div class="bg-gray-800 border border-gray-700 rounded-lg p-3 text-xs space-y-1">
        <div class="flex items-center gap-2">
          <span class="w-3 h-3 rounded-full bg-emerald-500"></span>
          <span>Running</span>
        </div>
        <div class="flex items-center gap-2">
          <span class="w-3 h-3 rounded-full bg-gray-500"></span>
          <span>Stopped</span>
        </div>
        <div class="flex items-center gap-2">
          <span class="w-3 h-3 rounded-full bg-blue-500"></span>
          <span>Network</span>
        </div>
      </div>
    </Panel>
  </SvelteFlow>
</div>
