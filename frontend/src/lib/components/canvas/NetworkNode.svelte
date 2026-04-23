<script lang="ts">
  import { Handle, Position } from '@xyflow/svelte';

  export let data: {
    label: string;
    icon?: string;
  };
</script>

<div class="rounded-full border border-blue-500 bg-blue-900/40 px-2.5 py-1 text-[7px] font-medium text-blue-200 flex items-center gap-1.5">
  <Handle type="target" position={Position.Top} class="!h-1.5 !w-1.5 !bg-blue-400" />
  <span class="truncate">{data.label}</span>
  <Handle type="source" position={Position.Bottom} class="!h-1.5 !w-1.5 !bg-blue-400" />
</div>
