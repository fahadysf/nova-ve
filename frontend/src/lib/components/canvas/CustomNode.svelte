<script lang="ts">
  import { Handle, Position } from '@xyflow/svelte';

  export let data: {
    label: string;
    icon?: string;
    status?: number;
    type?: string;
  };
</script>

<div class="px-3 py-2 rounded border text-xs font-medium flex items-center gap-2"
  class:bg-gray-800={data.status !== 2}
  class:border-gray-600={data.status !== 2}
  class:bg-gray-900={data.status === 2}
  class:border-emerald-500={data.status === 2}
  class:text-gray-100={true}
>
  <Handle type="target" position={Position.Left} class="!w-2 !h-2 !bg-gray-400" />
  <span>{data.label}</span>
  <Handle type="source" position={Position.Right} class="!w-2 !h-2 !bg-gray-400" />
</div>
