<script lang="ts">
  import { Handle, Position } from '@xyflow/svelte';

  export let data: {
    label: string;
    icon?: string;
    status?: number;
    type?: string;
  };
</script>

<div class="rounded border px-2.5 py-1.5 text-[11px] font-medium flex items-center gap-1.5"
  class:bg-gray-800={data.status !== 2}
  class:border-gray-600={data.status !== 2}
  class:bg-gray-900={data.status === 2}
  class:border-emerald-500={data.status === 2}
  class:text-gray-100={true}
>
  <Handle type="target" position={Position.Left} class="!h-1.5 !w-1.5 !bg-gray-400" />
  <span class="truncate">{data.label}</span>
  <Handle type="source" position={Position.Right} class="!h-1.5 !w-1.5 !bg-gray-400" />
</div>
