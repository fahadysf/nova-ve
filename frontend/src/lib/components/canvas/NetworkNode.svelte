<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  import { Handle, Position } from '@xyflow/svelte';
  import NetworkPort from './NetworkPort.svelte';

  export let id: string | undefined = undefined;
  export let data: {
    label: string;
    icon?: string;
    count?: number;
    networkId?: number;
  };

  $: networkId = (() => {
    if (typeof data.networkId === 'number') return data.networkId;
    if (typeof id === 'string' && id.startsWith('network')) {
      const n = Number(id.slice('network'.length));
      return Number.isFinite(n) ? n : 0;
    }
    return 0;
  })();
</script>

<div
  class="relative min-w-[7.5rem] rounded-full border border-blue-500 bg-blue-500/15 px-3 py-1.5 text-blue-100 shadow-lg shadow-black/20"
  data-testid="network-node"
  data-network-id={networkId}
>
  <Handle
    type="source"
    position={Position.Top}
    id={`network:${networkId}:top`}
    class="!h-1 !w-1 !border-0 !bg-transparent !opacity-0 !pointer-events-none"
  />
  <Handle
    type="source"
    position={Position.Right}
    id={`network:${networkId}:right`}
    class="!h-1 !w-1 !border-0 !bg-transparent !opacity-0 !pointer-events-none"
  />
  <Handle
    type="source"
    position={Position.Bottom}
    id={`network:${networkId}:bottom`}
    class="!h-1 !w-1 !border-0 !bg-transparent !opacity-0 !pointer-events-none"
  />
  <Handle
    type="source"
    position={Position.Left}
    id={`network:${networkId}:left`}
    class="!h-1 !w-1 !border-0 !bg-transparent !opacity-0 !pointer-events-none"
  />

  <NetworkPort {networkId} networkName={data.label} side="top" />
  <NetworkPort {networkId} networkName={data.label} side="right" />
  <NetworkPort {networkId} networkName={data.label} side="bottom" />
  <NetworkPort {networkId} networkName={data.label} side="left" />

  <div class="flex items-center justify-between gap-2">
    <div class="min-w-0">
      <div class="text-[8px] uppercase tracking-[0.18em] text-blue-200/80">Network</div>
      <div class="truncate text-[10px] font-semibold tracking-tight">{data.label}</div>
    </div>
    <span class="rounded-full bg-blue-950/80 px-1.5 py-0.5 font-mono text-[9px] text-blue-100">
      {data.count ?? 0}
    </span>
  </div>
</div>
