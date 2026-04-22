<script lang="ts">
  import { Handle, Position } from '@xyflow/svelte';

  export let data: {
    label: string;
    icon?: string;
  };
</script>

<div class="px-3 py-1.5 rounded-full border border-blue-500 bg-blue-900/40 text-blue-200 text-xs font-medium flex items-center gap-2">
  <Handle type="target" position={Position.Top} class="!w-2 !h-2 !bg-blue-400" />
  <span>{data.label}</span>
  <Handle type="source" position={Position.Bottom} class="!w-2 !h-2 !bg-blue-400" />
</div>
