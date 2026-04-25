<script lang="ts">
  import { Handle, Position } from '@xyflow/svelte';

  export let data: {
    label: string;
    icon?: string;
    count?: number;
  };
</script>

<div class="relative min-w-[7.5rem] rounded-full border border-blue-500 bg-blue-500/15 px-3 py-1.5 text-blue-100 shadow-lg shadow-black/20">
  <Handle type="target" position={Position.Top} class="!h-2 !w-2 !border !border-gray-950 !bg-blue-400" />

  <div class="flex items-center justify-between gap-2">
    <div class="min-w-0">
      <div class="text-[8px] uppercase tracking-[0.18em] text-blue-200/80">Network</div>
      <div class="truncate text-[10px] font-semibold tracking-tight">{data.label}</div>
    </div>
    <span class="rounded-full bg-blue-950/80 px-1.5 py-0.5 font-mono text-[9px] text-blue-100">
      {data.count ?? 0}
    </span>
  </div>

  <Handle type="source" position={Position.Bottom} class="!h-2 !w-2 !border !border-gray-950 !bg-blue-400" />
</div>
