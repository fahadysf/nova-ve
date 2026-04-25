<script lang="ts">
  import { Handle, Position } from '@xyflow/svelte';

  export let data: {
    label: string;
    icon?: string;
    status?: number;
    type?: string;
    template?: string;
    console?: string;
  };

  $: running = data.status === 2;
  $: statusLabel = running ? 'running' : 'stopped';
  $: typeLabel = (data.type || 'node').toUpperCase();
</script>

<div
  class={`relative min-w-[9.5rem] rounded-xl border px-3 py-2 text-gray-100 shadow-lg shadow-black/20 ${
    running ? 'border-emerald-500/80 bg-gray-900' : 'border-gray-700 bg-gray-800/95'
  }`}
>
  <Handle
    type="target"
    position={Position.Left}
    class={`!h-2 !w-2 !border !border-gray-950 ${running ? '!bg-emerald-400' : '!bg-gray-500'}`}
  />

  <div class="flex items-start gap-2">
    <span class={`mt-0.5 h-2.5 w-2.5 shrink-0 rounded-full ${running ? 'bg-emerald-500' : 'bg-gray-500'}`}></span>
    <div class="min-w-0 flex-1">
      <div class="text-[9px] uppercase tracking-[0.05em] text-gray-500">{typeLabel}</div>
      <div class="mt-1 truncate text-[11px] font-semibold tracking-tight text-gray-100">{data.label}</div>
      <div class="mt-1 flex items-center justify-between gap-2">
        <span class="truncate font-mono text-[10px] text-gray-400">{data.template || data.console || 'template'}</span>
        <span
          class={`rounded-full px-1.5 py-0.5 text-[9px] uppercase tracking-[0.16em] ${
            running ? 'bg-emerald-500/20 text-emerald-200' : 'bg-gray-900 text-gray-300'
          }`}
        >
          {statusLabel}
        </span>
      </div>
    </div>
  </div>

  <Handle
    type="source"
    position={Position.Right}
    class={`!h-2 !w-2 !border !border-gray-950 ${running ? '!bg-emerald-400' : '!bg-gray-500'}`}
  />
</div>
