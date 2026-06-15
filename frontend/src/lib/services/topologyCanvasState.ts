// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import type { LabDefaults, Link, NetworkData, NodeData, TopologyLink } from '$lib/types';

export interface TopologyCanvasLocalState {
  nodes: Record<string, NodeData>;
  networks: Record<string, NetworkData>;
  topology: TopologyLink[];
  links: Link[];
  defaults: LabDefaults;
}

export interface TopologyCanvasPropRefs {
  nodes: Record<string, NodeData>;
  networks: Record<string, NetworkData>;
  topology: TopologyLink[];
  links: Link[];
  defaults: LabDefaults;
}

export interface TopologyCanvasPropSyncResult {
  changed: boolean;
  local: TopologyCanvasLocalState;
  refs: TopologyCanvasPropRefs;
}

function deepClone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

export function syncTopologyCanvasLocalState(
  local: TopologyCanvasLocalState,
  refs: TopologyCanvasPropRefs,
  incoming: TopologyCanvasPropRefs
): TopologyCanvasPropSyncResult {
  const nodesChanged = incoming.nodes !== refs.nodes;
  const networksChanged = incoming.networks !== refs.networks;
  const topologyChanged = incoming.topology !== refs.topology;
  const linksChanged = incoming.links !== refs.links;
  const defaultsChanged = incoming.defaults !== refs.defaults;
  const changed =
    nodesChanged || networksChanged || topologyChanged || linksChanged || defaultsChanged;

  if (!changed) {
    return { changed: false, local, refs };
  }

  return {
    changed: true,
    refs: incoming,
    local: {
      nodes: nodesChanged ? deepClone(incoming.nodes) : local.nodes,
      networks: networksChanged ? deepClone(incoming.networks) : local.networks,
      topology: topologyChanged ? deepClone(incoming.topology) : local.topology,
      links: linksChanged ? deepClone(incoming.links) : local.links,
      defaults: defaultsChanged ? deepClone(incoming.defaults) : local.defaults,
    },
  };
}
