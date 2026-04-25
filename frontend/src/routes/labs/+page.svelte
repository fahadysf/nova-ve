<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { apiGetData, apiRequest, ApiError } from '$lib/api';
  import { logout, authStore } from '$lib/stores/auth';
  import { toastStore } from '$lib/stores/toasts';
  import type { FolderEntry, FolderListing, LabListItem } from '$lib/types';

  const STATIC_ROOT_FOLDERS = new Set(['Running', 'Shared', 'Users']);

  let currentFolder = '';
  let folders: FolderEntry[] = [];
  let labs: LabListItem[] = [];
  let loading = true;
  let busy = false;
  let error = '';

  $: folderSegments = currentFolder.split('/').filter(Boolean);
  $: currentFolderLabel = currentFolder || '/';
  $: inVirtualRootFolder = folderSegments.length === 1 && STATIC_ROOT_FOLDERS.has(folderSegments[0]);
  $: currentUserLabel = $authStore.user?.name || $authStore.user?.username || '';

  onMount(async () => {
    if (!$authStore.authenticated) {
      goto('/login');
      return;
    }

    await fetchContents('');
  });

  function normalizePath(path: string): string {
    const cleaned = path.trim().replace(/\\/g, '/').replace(/^\/+|\/+$/g, '');
    return cleaned ? `/${cleaned}` : '';
  }

  function pathDepth(path: string): number {
    return normalizePath(path).split('/').filter(Boolean).length;
  }

  function immediateEntries(data: FolderListing, folderPath: string): FolderListing {
    if (folderPath) {
      return data;
    }

    return {
      folders: (data.folders || []).filter((entry) => pathDepth(entry.path) === 1),
      labs: (data.labs || []).filter((entry) => pathDepth(entry.path) === 1)
    };
  }

  async function fetchContents(folderPath = currentFolder) {
    loading = true;
    error = '';
    const normalizedFolder = normalizePath(folderPath);

    try {
      const data = normalizedFolder
        ? await apiGetData<FolderListing>(`/folders${normalizedFolder}`)
        : await apiGetData<FolderListing>('/folders/');

      const immediate = immediateEntries(data, normalizedFolder);
      currentFolder = normalizedFolder;
      folders = [...(immediate.folders || [])].sort((left, right) => left.name.localeCompare(right.name));
      labs = [...(immediate.labs || [])].sort((left, right) => left.file.localeCompare(right.file));
    } catch (e) {
      error = e instanceof ApiError ? e.message : 'Unable to load labs.';
    } finally {
      loading = false;
    }
  }

  function openLab(lab: LabListItem) {
    goto(`/labs${lab.path}`);
  }

  function openFolder(folder: FolderEntry) {
    void fetchContents(folder.path);
  }

  async function handleLogout() {
    await logout();
    goto('/login');
  }

  async function createFolder() {
    if (inVirtualRootFolder) {
      toastStore.push('Cannot create folders inside virtual root folders.', 'error');
      return;
    }

    const name = window.prompt('New folder name');
    if (!name) return;

    busy = true;
    try {
      await apiRequest('/folders/', {
        method: 'POST',
        body: { path: currentFolder, name }
      });
      toastStore.push('Folder created.');
      await fetchContents(currentFolder);
    } catch (_error) {
      // toast already handled by api layer
    } finally {
      busy = false;
    }
  }

  async function createLab() {
    if (inVirtualRootFolder) {
      toastStore.push('Cannot create labs inside virtual root folders.', 'error');
      return;
    }

    const name = window.prompt('New lab name');
    if (!name) return;

    busy = true;
    try {
      const response = await apiRequest<{ path: string }>('/labs/', {
        method: 'POST',
        body: {
          name,
          path: currentFolder || '',
          author: $authStore.user?.username || '',
          description: ''
        }
      });
      toastStore.push('Lab created.');
      await goto(`/labs${response.data.path}`);
    } catch (_error) {
      // toast already handled by api layer
    } finally {
      busy = false;
    }
  }

  async function renameFolder(folder: FolderEntry) {
    if (STATIC_ROOT_FOLDERS.has(folder.name)) {
      toastStore.push('Static folders cannot be renamed.', 'error');
      return;
    }

    const nextName = window.prompt('Rename folder', folder.name);
    if (!nextName || nextName === folder.name) return;

    const parentPath = normalizePath(folder.path.split('/').slice(0, -1).join('/'));
    const nextPath = normalizePath(`${parentPath}/${nextName}`);

    busy = true;
    try {
      await apiRequest(`/folders${folder.path}`, {
        method: 'PUT',
        body: { path: nextPath }
      });
      toastStore.push('Folder renamed.');
      await fetchContents(currentFolder);
    } catch (_error) {
      // toast already handled by api layer
    } finally {
      busy = false;
    }
  }

  async function deleteFolder(folder: FolderEntry) {
    if (STATIC_ROOT_FOLDERS.has(folder.name)) {
      toastStore.push('Static folders cannot be deleted.', 'error');
      return;
    }

    if (!window.confirm(`Delete folder "${folder.name}" and all nested contents?`)) return;

    busy = true;
    try {
      await apiRequest(`/folders${folder.path}`, { method: 'DELETE' });
      toastStore.push('Folder deleted.');
      await fetchContents(currentFolder);
    } catch (_error) {
      // toast already handled by api layer
    } finally {
      busy = false;
    }
  }

  async function deleteLab(lab: LabListItem) {
    if (!window.confirm(`Delete lab "${lab.file}"?`)) return;

    busy = true;
    try {
      await apiRequest(`/labs/_${lab.path}`, { method: 'DELETE' });
      toastStore.push('Lab deleted.');
      await fetchContents(currentFolder);
    } catch (_error) {
      // toast already handled by api layer
    } finally {
      busy = false;
    }
  }

  function goToRoot() {
    void fetchContents('');
  }

  function goUp() {
    if (!currentFolder) return;
    const parent = folderSegments.slice(0, -1).join('/');
    void fetchContents(parent ? `/${parent}` : '');
  }
</script>

<div class="flex flex-1 flex-col bg-gray-900">
  <header class="flex h-14 items-center justify-between border-b border-gray-700 bg-gray-800 px-4">
    <div class="text-lg font-bold tracking-tight text-gray-100">nova-ve</div>
    <div class="flex items-center gap-4">
      <span class="text-sm text-gray-400">{currentUserLabel}</span>
      <button on:click={handleLogout} class="text-sm text-red-400 transition hover:text-red-300">Logout</button>
    </div>
  </header>

  <main class="flex-1 overflow-auto p-6">
    <div class="space-y-6">
      <section class="flex flex-wrap items-start justify-between gap-4 rounded-2xl border border-gray-800 bg-gray-900/95 px-5 py-4">
        <div>
          <div class="text-[10px] uppercase tracking-[0.28em] text-gray-500">Labs Browser</div>
          <h1 class="mt-3 text-xl font-semibold tracking-tight text-gray-100">Labs</h1>
          <div class="mt-3 flex flex-wrap items-center gap-2 text-[10px] uppercase tracking-[0.2em] text-gray-500">
            <button class="transition hover:text-white" on:click={goToRoot}>Root</button>
            {#if currentFolder}
              <span>/</span>
              {#each folderSegments as segment, index}
                <button
                  class="transition hover:text-white"
                  on:click={() => fetchContents(`/${folderSegments.slice(0, index + 1).join('/')}`)}
                >
                  {segment}
                </button>
                {#if index < folderSegments.length - 1}
                  <span>/</span>
                {/if}
              {/each}
            {/if}
          </div>
        </div>

        <div class="flex flex-wrap items-center gap-2">
          <button
            class="rounded-md border border-gray-700 px-3 py-2 text-[10px] uppercase tracking-[0.2em] text-gray-300 transition hover:border-blue-500 hover:text-white disabled:opacity-40"
            disabled={!currentFolder || busy}
            on:click={goUp}
          >
            Up
          </button>
          <button
            class="rounded-md border border-gray-700 px-3 py-2 text-[10px] uppercase tracking-[0.2em] text-gray-300 transition hover:border-blue-500 hover:text-white disabled:opacity-40"
            disabled={busy}
            on:click={createFolder}
          >
            New Folder
          </button>
          <button
            class="rounded-md border border-blue-500 px-3 py-2 text-[10px] uppercase tracking-[0.2em] text-blue-200 transition hover:bg-blue-500/10 disabled:opacity-40"
            disabled={busy}
            on:click={createLab}
          >
            New Lab
          </button>
        </div>
      </section>

      {#if loading}
        <div class="space-y-6">
          <section class="space-y-3">
            <div class="text-[10px] uppercase tracking-[0.24em] text-gray-500">Folders</div>
            <div class="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
              {#each Array.from({ length: 3 }) as _}
                <div class="animate-pulse rounded-lg border border-gray-800 bg-gray-800 p-4" aria-hidden="true">
                  <div class="h-4 w-2/3 rounded bg-gray-700"></div>
                  <div class="mt-3 h-3 w-1/2 rounded bg-gray-800"></div>
                  <div class="mt-5 h-8 rounded bg-gray-900/70"></div>
                </div>
              {/each}
            </div>
          </section>

          <section class="space-y-3">
            <div class="text-[10px] uppercase tracking-[0.24em] text-gray-500">Labs</div>
            <div class="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
              {#each Array.from({ length: 3 }) as _}
                <div class="animate-pulse rounded-lg border border-gray-800 bg-gray-800 p-4" aria-hidden="true">
                  <div class="h-4 w-2/3 rounded bg-gray-700"></div>
                  <div class="mt-3 h-3 w-4/5 rounded bg-gray-800"></div>
                  <div class="mt-5 h-8 rounded bg-gray-900/70"></div>
                </div>
              {/each}
            </div>
          </section>
        </div>
      {:else if error}
        <div class="max-w-xl rounded-xl border border-red-500/30 bg-red-500/10 p-4 text-sm text-red-100">
          <div class="font-medium">Unable to load labs</div>
          <p class="mt-2 text-red-100/80">{error}</p>
          <button
            class="mt-4 rounded-md border border-red-400/40 px-3 py-2 text-xs uppercase tracking-[0.2em] text-red-100 hover:bg-red-500/10"
            on:click={() => fetchContents(currentFolder)}
          >
            Retry
          </button>
        </div>
      {:else}
        <section class="space-y-3">
          <div class="text-[10px] uppercase tracking-[0.24em] text-gray-500">Folders</div>
          {#if folders.length === 0}
            <div class="rounded-lg border border-gray-800 bg-gray-800 p-4 text-sm text-gray-400">
              No folders in {currentFolderLabel || '/'}.
            </div>
          {:else}
            <div class="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
              {#each folders as folder}
                {@const isStaticFolder = STATIC_ROOT_FOLDERS.has(folder.name)}
                <article class="rounded-lg border border-gray-700 bg-gray-800 p-4">
                  <button class="w-full text-left" on:click={() => openFolder(folder)}>
                    <div class="font-medium text-gray-100">{folder.name}</div>
                    <div class="mt-2 font-mono text-[10px] uppercase tracking-[0.18em] text-gray-500">{folder.path}</div>
                    {#if folder.mtime}
                      <div class="mt-3 text-sm text-gray-500">{folder.mtime}</div>
                    {/if}
                  </button>
                  <div class="mt-4 flex flex-wrap gap-2">
                    <button
                      class="rounded-md border border-gray-700 px-3 py-2 text-[10px] uppercase tracking-[0.2em] text-gray-300 transition hover:border-blue-500 hover:text-white"
                      on:click={() => openFolder(folder)}
                    >
                      Open
                    </button>
                    <button
                      class="rounded-md border border-gray-700 px-3 py-2 text-[10px] uppercase tracking-[0.2em] text-gray-300 transition hover:border-blue-500 hover:text-white disabled:opacity-40"
                      disabled={isStaticFolder}
                      on:click={() => renameFolder(folder)}
                    >
                      Rename
                    </button>
                    <button
                      class="rounded-md border border-red-500/40 px-3 py-2 text-[10px] uppercase tracking-[0.2em] text-red-200 transition hover:bg-red-500/10 disabled:opacity-40"
                      disabled={isStaticFolder}
                      on:click={() => deleteFolder(folder)}
                    >
                      Delete
                    </button>
                  </div>
                </article>
              {/each}
            </div>
          {/if}
        </section>

        <section class="space-y-3">
          <div class="text-[10px] uppercase tracking-[0.24em] text-gray-500">Labs</div>
          {#if labs.length === 0}
            <div class="rounded-lg border border-gray-800 bg-gray-800 p-4 text-sm text-gray-400">
              No labs in {currentFolderLabel || '/'}.
            </div>
          {:else}
            <div class="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
              {#each labs as lab}
                <article class="rounded-lg border border-gray-700 bg-gray-800 p-4">
                  <button class="w-full text-left" on:click={() => openLab(lab)}>
                    <div class="font-medium text-gray-100">{lab.file.replace('.json', '').replace('.unl', '')}</div>
                    <div class="mt-2 font-mono text-[10px] uppercase tracking-[0.18em] text-gray-500">{lab.path}</div>
                    <div class="mt-3 text-sm text-gray-500">{lab.mtime}</div>
                  </button>
                  <div class="mt-4 flex flex-wrap gap-2">
                    <button
                      class="rounded-md border border-gray-700 px-3 py-2 text-[10px] uppercase tracking-[0.2em] text-gray-300 transition hover:border-blue-500 hover:text-white"
                      on:click={() => openLab(lab)}
                    >
                      Open
                    </button>
                    <button
                      class="rounded-md border border-red-500/40 px-3 py-2 text-[10px] uppercase tracking-[0.2em] text-red-200 transition hover:bg-red-500/10"
                      on:click={() => deleteLab(lab)}
                    >
                      Delete
                    </button>
                  </div>
                </article>
              {/each}
            </div>
          {/if}
        </section>
      {/if}
    </div>
  </main>
</div>
