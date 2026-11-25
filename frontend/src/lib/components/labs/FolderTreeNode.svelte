<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  import { ChevronRight, Folder, FolderOpen } from 'lucide-svelte';
  import type { ExplorerNode } from '$lib/services/labsExplorer';

  export let node: ExplorerNode;
  export let current = '';
  export let expanded: Set<string>;
  export let depth = 0;
  export let onSelect: (path: string) => void;
  export let onToggle: (path: string) => void;

  $: isOpen = expanded.has(node.path);
  $: isCurrent = current === node.path;
</script>

<div role="treeitem" aria-selected={isCurrent} aria-expanded={node.folders.length ? isOpen : undefined}>
  <div
    class="group flex items-center gap-1 rounded-md py-1 pr-2 text-sm {isCurrent
      ? 'bg-blue-500/15 text-blue-100'
      : 'text-gray-300 hover:bg-gray-800'}"
    style="padding-left: {depth * 14 + 4}px"
  >
    <button
      class="flex h-5 w-5 items-center justify-center rounded text-gray-500 hover:text-white {node.folders.length
        ? ''
        : 'invisible'}"
      aria-label={isOpen ? 'Collapse' : 'Expand'}
      on:click={() => onToggle(node.path)}
    >
      <ChevronRight size={14} class="transition-transform {isOpen ? 'rotate-90' : ''}" />
    </button>
    <button class="flex min-w-0 flex-1 items-center gap-2 text-left" on:click={() => onSelect(node.path)}>
      {#if isOpen || isCurrent}
        <FolderOpen size={16} class="shrink-0 text-amber-400" />
      {:else}
        <Folder size={16} class="shrink-0 text-amber-400" />
      {/if}
      <span class="truncate">{node.name}</span>
    </button>
  </div>
  {#if isOpen}
    <div role="group">
      {#each node.folders as child (child.path)}
        <svelte:self node={child} {current} {expanded} depth={depth + 1} {onSelect} {onToggle} />
      {/each}
    </div>
  {/if}
</div>
