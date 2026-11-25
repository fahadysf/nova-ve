<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<!-- File-explorer style Labs browser (#322): folder tree + Name/Owner/Modified list. -->
<script lang="ts">
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { FileText, Folder, FolderPlus, FilePlus, ChevronUp, Users, Shield } from 'lucide-svelte';
  import { apiGetData, apiRequest, ApiError, moveLab } from '$lib/api';
  import { authStore } from '$lib/stores/auth';
  import { toastStore } from '$lib/stores/toasts';
  import FolderTreeNode from '$lib/components/labs/FolderTreeNode.svelte';
  import {
    buildTree,
    countLabs,
    crumbs,
    findNode,
    labDisplayName,
    normalizePath,
    ownerLabel,
    parentPath,
    SHARED_OWNER,
    SYSTEM_OWNER,
    type ExplorerNode
  } from '$lib/services/labsExplorer';
  import type { FolderListing, LabListItem } from '$lib/types';

  const STATIC_ROOT_FOLDERS = new Set(['Running', 'Users']);

  let root: ExplorerNode = buildTree({ folders: [], labs: [] });
  let currentFolder = '';
  let expanded = new Set<string>(['']);
  let selected = '';
  let loading = true;
  let busy = false;
  let error = '';

  $: current = findNode(root, currentFolder) ?? root;
  $: folderSegments = currentFolder.split('/').filter(Boolean);
  $: inVirtualRootFolder = folderSegments.length === 1 && STATIC_ROOT_FOLDERS.has(folderSegments[0]);
  $: pathCrumbs = crumbs(currentFolder);

  onMount(async () => {
    if (!$authStore.authenticated) {
      goto('/login');
      return;
    }
    await load();
  });

  async function load() {
    loading = true;
    error = '';
    try {
      const data = await apiGetData<FolderListing>('/folders/');
      root = buildTree(data);
      if (!findNode(root, currentFolder)) currentFolder = '';
    } catch (e) {
      error = e instanceof ApiError ? e.message : 'Unable to load labs.';
    } finally {
      loading = false;
    }
  }

  function navigate(path: string) {
    currentFolder = normalizePath(path);
    selected = '';
    // Expand every ancestor so the tree always reveals the current folder.
    const next = new Set(expanded);
    let walked = '';
    next.add('');
    for (const segment of currentFolder.split('/').filter(Boolean)) {
      walked += `/${segment}`;
      next.add(walked);
    }
    expanded = next;
  }

  function toggle(path: string) {
    const next = new Set(expanded);
    if (next.has(path)) next.delete(path);
    else next.add(path);
    expanded = next;
  }

  function openLab(lab: LabListItem) {
    goto(`/labs${lab.path}`);
  }

  const isStatic = (folder: { name: string; path: string }) =>
    STATIC_ROOT_FOLDERS.has(folder.name) && folder.path === `/${folder.name}`;

  async function mutate(action: () => Promise<void>) {
    busy = true;
    try {
      await action();
    } catch (_error) {
      // toast already handled by api layer
    } finally {
      busy = false;
    }
  }

  async function createFolder() {
    if (inVirtualRootFolder) {
      toastStore.push('Cannot create folders inside virtual root folders.', 'error');
      return;
    }
    const name = window.prompt('New folder name');
    if (!name) return;
    await mutate(async () => {
      await apiRequest('/folders/', { method: 'POST', body: { path: currentFolder, name } });
      toastStore.push('Folder created.');
      await load();
    });
  }

  async function createLab() {
    if (inVirtualRootFolder) {
      toastStore.push('Cannot create labs inside virtual root folders.', 'error');
      return;
    }
    const name = window.prompt('New lab name');
    if (!name) return;
    await mutate(async () => {
      const response = await apiRequest<{ path: string }>('/labs/', {
        method: 'POST',
        body: { name, path: currentFolder || '', author: $authStore.user?.username || '', description: '' }
      });
      toastStore.push('Lab created.', 'success');
      await goto(`/labs${response.data.path}`);
    });
  }

  async function renameFolder(folder: ExplorerNode) {
    if (isStatic(folder)) {
      toastStore.push('Static folders cannot be renamed.', 'error');
      return;
    }
    const nextName = window.prompt('Rename folder', folder.name);
    if (!nextName || nextName === folder.name) return;
    const nextPath = normalizePath(`${parentPath(folder.path)}/${nextName}`);
    await mutate(async () => {
      await apiRequest(`/folders${folder.path}`, { method: 'PUT', body: { path: nextPath } });
      toastStore.push('Folder renamed.');
      await load();
    });
  }

  async function deleteFolder(folder: ExplorerNode) {
    if (isStatic(folder)) {
      toastStore.push('Static folders cannot be deleted.', 'error');
      return;
    }
    if (!window.confirm(`Delete folder "${folder.name}" and all nested contents?`)) return;
    await mutate(async () => {
      await apiRequest(`/folders${folder.path}`, { method: 'DELETE' });
      toastStore.push('Folder deleted.');
      await load();
    });
  }

  async function deleteLab(lab: LabListItem) {
    if (!window.confirm(`Delete lab "${lab.file}"?`)) return;
    await mutate(async () => {
      await apiRequest(`/labs/_${lab.path}`, { method: 'DELETE' });
      toastStore.push('Lab deleted.');
      await load();
    });
  }

  let movePicker: { lab: LabListItem; options: string[] } | null = null;
  let moveDestination = '';

  function collectFolderPaths(node: ExplorerNode, into: Set<string>) {
    for (const child of node.folders) {
      into.add(child.path);
      collectFolderPaths(child, into);
    }
  }

  function openMovePicker(lab: LabListItem) {
    const paths = new Set<string>(['/Shared']);
    collectFolderPaths(root, paths);
    const currentDir = parentPath(lab.path);
    const options = [...paths].filter((path) => path !== currentDir).sort();
    moveDestination = options[0] || '/Shared';
    movePicker = { lab, options };
  }

  function cancelMove() {
    movePicker = null;
    moveDestination = '';
  }

  async function confirmMove() {
    if (!movePicker || !moveDestination) return;
    const { lab } = movePicker;
    const destination = moveDestination;
    await mutate(async () => {
      await moveLab(lab.path, destination);
      toastStore.push(`Lab moved to ${destination}.`, 'success');
      movePicker = null;
      moveDestination = '';
      await load();
    });
  }

  function goUp() {
    if (currentFolder) navigate(parentPath(currentFolder));
  }
</script>

<div class="flex h-full min-h-[calc(100vh-4rem)] flex-col gap-4 p-6">
  <header class="flex flex-wrap items-center justify-between gap-3">
    <div>
      <div class="text-[10px] uppercase tracking-[0.05em] text-gray-500">Labs Browser</div>
      <nav class="mt-2 flex flex-wrap items-center gap-1 text-sm text-gray-300" aria-label="Path">
        {#each pathCrumbs as crumb, index}
          {#if index > 0}<span class="text-gray-600">/</span>{/if}
          <button
            class="rounded px-1.5 py-0.5 transition hover:bg-gray-800 hover:text-white {index === pathCrumbs.length - 1
              ? 'font-semibold text-gray-100'
              : ''}"
            on:click={() => navigate(crumb.path)}>{crumb.name}</button
          >
        {/each}
      </nav>
    </div>
    <div class="flex flex-wrap items-center gap-2">
      <button
        class="flex items-center gap-1.5 rounded-md border border-gray-700 px-3 py-2 text-xs text-gray-300 transition hover:border-blue-500 hover:text-white disabled:opacity-40"
        disabled={!currentFolder || busy}
        on:click={goUp}><ChevronUp size={14} /> Up</button
      >
      <button
        class="flex items-center gap-1.5 rounded-md border border-gray-700 px-3 py-2 text-xs text-gray-300 transition hover:border-blue-500 hover:text-white disabled:opacity-40"
        disabled={busy}
        on:click={createFolder}><FolderPlus size={14} /> New folder</button
      >
      <button
        class="flex items-center gap-1.5 rounded-md border border-blue-500 px-3 py-2 text-xs text-blue-200 transition hover:bg-blue-500/10 disabled:opacity-40"
        disabled={busy}
        on:click={createLab}><FilePlus size={14} /> New lab</button
      >
    </div>
  </header>

  {#if loading}
    <div class="animate-pulse rounded-xl border border-gray-800 bg-gray-900 p-6 text-sm text-gray-500">Loading labs…</div>
  {:else if error}
    <div class="max-w-xl rounded-xl border border-red-500/30 bg-red-500/10 p-4 text-sm text-red-100">
      <div class="font-medium">Unable to load labs</div>
      <p class="mt-2 text-red-100/80">{error}</p>
      <button class="mt-4 rounded-md border border-red-400/40 px-3 py-2 text-xs hover:bg-red-500/10" on:click={load}>Retry</button>
    </div>
  {:else}
    <div class="grid flex-1 grid-cols-[16rem_1fr] overflow-hidden rounded-xl border border-gray-800 bg-gray-900/95">
      <div class="overflow-y-auto border-r border-gray-800 p-2" role="tree" aria-label="Folders">
        <FolderTreeNode node={root} current={currentFolder} {expanded} onSelect={navigate} onToggle={toggle} />
      </div>

      <section class="flex min-w-0 flex-col">
        <div
          class="grid grid-cols-[minmax(0,1fr)_10rem_11rem_11rem] items-center gap-4 border-b border-gray-800 px-4 py-2 text-[10px] uppercase tracking-[0.05em] text-gray-500"
        >
          <div>Name</div>
          <div>Owner</div>
          <div>Modified</div>
          <div class="text-right normal-case tracking-normal">{current.folders.length} folders · {current.labs.length} labs</div>
        </div>

        <ul class="flex-1 divide-y divide-gray-800/70 overflow-y-auto" role="listbox" aria-label="Contents of {current.name}">
          {#each current.folders as folder (folder.path)}
            <li
              role="option"
              aria-selected={selected === folder.path}
              tabindex="0"
              class="group grid cursor-default grid-cols-[minmax(0,1fr)_10rem_11rem_11rem] items-center gap-4 px-4 py-2 text-sm {selected ===
              folder.path
                ? 'bg-blue-500/15'
                : 'hover:bg-gray-800/60'}"
              on:click={() => (selected = folder.path)}
              on:dblclick={() => navigate(folder.path)}
              on:keydown={(event) => event.key === 'Enter' && navigate(folder.path)}
            >
              <div class="flex min-w-0 items-center gap-3">
                <Folder size={18} class="shrink-0 fill-amber-400/20 text-amber-400" />
                <span class="truncate text-gray-100">{folder.name}</span>
                <span class="text-[10px] text-gray-600">{countLabs(folder)} labs</span>
                {#if folder.path === '/Shared'}
                  <span class="rounded border border-blue-500/40 px-1.5 py-0.5 text-[9px] uppercase tracking-wider text-blue-300">Shared space</span>
                {/if}
              </div>
              <div class="flex items-center gap-1.5 truncate text-gray-400">
                {#if folder.owner === SHARED_OWNER}<Users size={13} class="text-blue-300" />{:else if folder.owner === SYSTEM_OWNER}<Shield size={13} />{/if}
                {ownerLabel(folder.owner)}
              </div>
              <div class="text-gray-500">{folder.mtime ?? '—'}</div>
              <div class="flex justify-end gap-1 opacity-0 transition group-hover:opacity-100 group-focus-within:opacity-100">
                <button class="rounded px-2 py-1 text-xs text-gray-300 hover:bg-gray-700" on:click|stopPropagation={() => navigate(folder.path)}>Open</button>
                <button class="rounded px-2 py-1 text-xs text-gray-300 hover:bg-gray-700 disabled:opacity-30" disabled={isStatic(folder)} on:click|stopPropagation={() => renameFolder(folder)}>Rename</button>
                <button class="rounded px-2 py-1 text-xs text-red-300 hover:bg-red-500/10 disabled:opacity-30" disabled={isStatic(folder)} on:click|stopPropagation={() => deleteFolder(folder)}>Delete</button>
              </div>
            </li>
          {/each}

          {#each current.labs as lab (lab.path)}
            <li
              role="option"
              aria-selected={selected === lab.path}
              tabindex="0"
              class="group grid cursor-default grid-cols-[minmax(0,1fr)_10rem_11rem_11rem] items-center gap-4 px-4 py-2 text-sm {selected ===
              lab.path
                ? 'bg-blue-500/15'
                : 'hover:bg-gray-800/60'}"
              on:click={() => (selected = lab.path)}
              on:dblclick={() => openLab(lab)}
              on:keydown={(event) => event.key === 'Enter' && openLab(lab)}
            >
              <div class="flex min-w-0 items-center gap-3">
                <FileText size={18} class="shrink-0 text-sky-400" />
                <span class="truncate text-gray-100">{labDisplayName(lab)}</span>
              </div>
              <div class="truncate text-gray-400">{ownerLabel(lab.owner)}</div>
              <div class="text-gray-500">{lab.mtime}</div>
              <div class="flex justify-end gap-1 opacity-0 transition group-hover:opacity-100 group-focus-within:opacity-100">
                <button class="rounded px-2 py-1 text-xs text-gray-300 hover:bg-gray-700" on:click|stopPropagation={() => openLab(lab)}>Open</button>
                <button class="rounded px-2 py-1 text-xs text-gray-300 hover:bg-gray-700 disabled:opacity-30" disabled={busy} on:click|stopPropagation={() => openMovePicker(lab)}>Move…</button>
                <button class="rounded px-2 py-1 text-xs text-red-300 hover:bg-red-500/10" on:click|stopPropagation={() => deleteLab(lab)}>Delete</button>
              </div>
            </li>
          {/each}

          {#if current.folders.length === 0 && current.labs.length === 0}
            <li class="px-4 py-10 text-center text-sm text-gray-500">This folder is empty.</li>
          {/if}
        </ul>
      </section>
    </div>
  {/if}

  {#if movePicker}
    <div class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
      <div class="w-full max-w-md rounded-2xl border border-gray-700 bg-gray-900 p-5">
        <div class="text-[10px] uppercase tracking-[0.05em] text-gray-500">Move lab</div>
        <h2 class="mt-2 text-lg font-semibold text-gray-100">{movePicker.lab.file}</h2>
        <label for="move-destination" class="mt-4 block text-[10px] uppercase tracking-[0.05em] text-gray-500">
          Target folder
        </label>
        <select
          id="move-destination"
          bind:value={moveDestination}
          class="mt-2 w-full rounded-md border border-gray-700 bg-gray-800 px-3 py-2 text-sm text-gray-100"
        >
          {#each movePicker.options as option}
            <option value={option}>{option}</option>
          {/each}
        </select>
        <div class="mt-5 flex justify-end gap-2">
          <button
            class="rounded-md border border-gray-700 px-3 py-2 text-[10px] uppercase tracking-[0.05em] text-gray-300 transition hover:border-blue-500 hover:text-white"
            on:click={cancelMove}>Cancel</button
          >
          <button
            class="rounded-md border border-blue-500 px-3 py-2 text-[10px] uppercase tracking-[0.05em] text-blue-200 transition hover:bg-blue-500/10 disabled:opacity-40"
            disabled={busy || !moveDestination}
            on:click={confirmMove}>Move</button
          >
        </div>
      </div>
    </div>
  {/if}
</div>
