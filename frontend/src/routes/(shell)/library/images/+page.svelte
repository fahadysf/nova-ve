<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<!--
  Image & template library (#264). Live inventory from /api/library:
  templates with their on-box image matches (missing-image state explicit),
  and every image with size, lazy sha256 (verify computes + caches),
  modification time, referencing labs, and reference-guarded delete.
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import { Check, Copy, Pencil, RefreshCw, ShieldAlert, Trash2 } from 'lucide-svelte';
  import PageHeader from '$lib/components/shell/PageHeader.svelte';
  import TemplateDialog from '$lib/components/manage/TemplateDialog.svelte';
  import { apiRequest } from '$lib/api';
  import { toastStore } from '$lib/stores/toasts';
  import type { LibraryImage, LibraryTemplate, LibraryTemplateEdit } from '$lib/types';

  let tab: 'templates' | 'images' = 'templates';
  let templates: LibraryTemplate[] = [];
  let images: LibraryImage[] = [];
  let loading = false;
  let search = '';
  let typeFilter = '';
  let verifying: string | null = null;
  let copiedSha: string | null = null;
  let deleteTarget: LibraryImage | null = null;
  let dialogTemplate: LibraryTemplate | null = null;

  const TYPE_OPTIONS = ['qemu', 'docker', 'iol', 'dynamips'];

  async function refresh() {
    loading = true;
    try {
      const [templateData, imageData] = await Promise.all([
        apiRequest<{ templates: LibraryTemplate[] }>('/library/templates'),
        apiRequest<{ images: LibraryImage[] }>('/library/images')
      ]);
      templates = templateData.data.templates;
      images = imageData.data.images;
    } finally {
      loading = false;
    }
  }

  function formatSize(bytes: number | null): string {
    if (bytes === null || bytes === undefined) return '—';
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
    if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
    return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
  }

  function shortSha(sha: string): string {
    return `${sha.slice(0, 12)}…${sha.slice(-6)}`;
  }

  function filteredTemplates(list: LibraryTemplate[]): LibraryTemplate[] {
    return list.filter((template) => {
      if (template.paired_parent) return false;
      if (typeFilter && template.type !== typeFilter) return false;
      const needle = search.trim().toLowerCase();
      if (!needle) return true;
      return (
        template.name.toLowerCase().includes(needle) ||
        template.key.toLowerCase().includes(needle) ||
        template.image_aliases.some((alias) => alias.toLowerCase().includes(needle))
      );
    });
  }

  function filteredImages(list: LibraryImage[]): LibraryImage[] {
    return list.filter((image) => {
      if (typeFilter && image.type !== typeFilter) return false;
      const needle = search.trim().toLowerCase();
      if (!needle) return true;
      return (
        image.name.toLowerCase().includes(needle) ||
        image.filename.toLowerCase().includes(needle) ||
        image.referenced_by.some((ref) => ref.lab.toLowerCase().includes(needle))
      );
    });
  }

  function groupByType(list: LibraryTemplate[]): { type: string; items: LibraryTemplate[] }[] {
    return TYPE_OPTIONS.map((type) => ({
      type,
      items: filteredTemplates(list).filter((template) => template.type === type)
    })).filter((group) => group.items.length > 0);
  }

  async function verifyImage(image: LibraryImage) {
    verifying = `${image.type}/${image.name}`;
    try {
      const data = await apiRequest<{ sha256: string; size: number }>(
        `/library/images/${image.type}/${encodeURIComponent(image.name)}/verify`,
        { method: 'POST' }
      );
      // Immutable update — nested mutation would not invalidate `images`.
      images = images.map((item) =>
        item === image ? { ...item, sha256: data.data.sha256 } : item
      );
      toastStore.push('Checksum computed and cached.', 'success');
    } catch {
      /* toasted */
    } finally {
      verifying = null;
    }
  }

  async function copySha(image: LibraryImage) {
    if (!image.sha256) return;
    try {
      await navigator.clipboard.writeText(image.sha256);
      copiedSha = image.sha256;
      setTimeout(() => (copiedSha = null), 1500);
    } catch {
      toastStore.push('Clipboard unavailable.', 'error');
    }
  }

  function isReferenced(image: LibraryImage): boolean {
    return image.referenced_by.length > 0;
  }

  async function handleDelete() {
    const target = deleteTarget;
    deleteTarget = null;
    if (!target) return;
    try {
      await apiRequest(
        `/library/images/${target.type}/${encodeURIComponent(target.name)}`,
        { method: 'DELETE' }
      );
      toastStore.push(`Image ${target.filename} deleted.`, 'success');
      await refresh();
    } catch {
      /* toasted */
    }
  }

  async function handleTemplateSave(event: CustomEvent<{ template: LibraryTemplate; edit: LibraryTemplateEdit }>) {
    const { template, edit } = event.detail;
    try {
      await apiRequest(`/library/templates/${template.type}/${encodeURIComponent(template.key)}`, {
        method: 'PATCH',
        body: edit
      });
      toastStore.push(`Template ${template.key} updated.`, 'success');
      dialogTemplate = null;
      await refresh();
    } catch {
      /* toasted */
    }
  }

  onMount(() => {
    void refresh();
  });
</script>

<div class="p-6">
  <div class="space-y-6">
    <PageHeader issue={264}>
      <span slot="kicker" class="text-[10px] uppercase tracking-[0.05em] text-gray-500">
        Library
      </span>
      <span slot="title">Images &amp; Templates</span>
      <span slot="actions">
        <button
          class="inline-flex items-center gap-1.5 rounded-md border border-gray-700 px-3 py-2 text-[10px] uppercase tracking-[0.05em] text-gray-300 transition hover:border-blue-500 hover:text-white"
          on:click={() => void refresh()}
          data-testid="library-refresh"
        >
          <RefreshCw class="h-3 w-3" />
          Refresh
        </button>
      </span>
    </PageHeader>

    <div class="flex flex-wrap items-center justify-between gap-3">
      <div class="flex gap-1 rounded-xl border border-gray-800 bg-gray-900/95 p-1">
        <button
          class={`rounded-lg px-3 py-1.5 text-[12px] transition ${tab === 'templates' ? 'bg-blue-500/15 text-blue-200' : 'text-gray-400 hover:text-gray-100'}`}
          on:click={() => (tab = 'templates')}
          data-testid="images-tab-templates"
        >Templates ({templates.length})</button>
        <button
          class={`rounded-lg px-3 py-1.5 text-[12px] transition ${tab === 'images' ? 'bg-blue-500/15 text-blue-200' : 'text-gray-400 hover:text-gray-100'}`}
          on:click={() => (tab = 'images')}
          data-testid="images-tab-images"
        >Images ({images.length})</button>
      </div>
      <div class="flex items-center gap-2">
        <input
          class="w-56 rounded-lg border border-gray-700 bg-gray-950 px-3 py-1.5 text-[12px] text-gray-200 placeholder-gray-600 focus:border-blue-500 focus:outline-none"
          placeholder="Search…"
          bind:value={search}
          data-testid="images-search"
        />
        <select
          class="rounded-lg border border-gray-700 bg-gray-950 px-2 py-1.5 text-[12px] text-gray-300 focus:border-blue-500 focus:outline-none"
          bind:value={typeFilter}
          data-testid="images-type-filter"
        >
          <option value="">All types</option>
          {#each TYPE_OPTIONS as option (option)}
            <option value={option}>{option}</option>
          {/each}
        </select>
      </div>
    </div>

    {#if loading && !templates.length && !images.length}
      <div class="rounded-2xl border border-gray-800 bg-gray-900/95 px-5 py-10 text-center text-sm text-gray-500">
        Loading library…
      </div>
    {:else if tab === 'templates'}
      <div class="space-y-6" data-testid="templates-panel">
        {#each groupByType(templates) as group (group.type)}
          <section>
            <div class="text-[10px] uppercase tracking-[0.05em] text-gray-500">{group.type}</div>
            <div class="mt-2 grid grid-cols-1 gap-3 xl:grid-cols-2">
              {#each group.items as template (template.key)}
                <div class="rounded-2xl border border-gray-800 bg-gray-900/95 p-4" data-testid={`template-card-${template.key}`}>
                  <div class="flex items-start justify-between gap-3">
                    <div class="min-w-0">
                      <div class="flex items-center gap-2">
                        <span class="truncate text-sm font-semibold text-gray-100">{template.name}</span>
                        <span class="rounded-full border border-gray-700 px-1.5 py-px text-[9px] uppercase tracking-[0.05em] text-gray-500">{template.key}</span>
                      </div>
                      <div class="mt-0.5 truncate text-[11px] text-gray-500">{template.description || '—'}</div>
                    </div>
                    <button
                      class="shrink-0 rounded-md border border-gray-700 px-2.5 py-1.5 text-[10px] uppercase tracking-[0.05em] text-gray-300 transition hover:border-blue-500 hover:text-white"
                      on:click={() => (dialogTemplate = template)}
                      data-testid={`template-edit-${template.key}`}
                    >
                      <Pencil class="mr-1 inline h-3 w-3" />
                      Edit
                    </button>
                  </div>
                  <div class="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-gray-500">
                    <span>RAM <span class="text-gray-300">{template.ram} MB</span></span>
                    <span>vCPU <span class="text-gray-300">{template.cpu}</span></span>
                    <span>NICs <span class="text-gray-300">{template.ethernet}</span></span>
                    <span>console <span class="text-gray-300">{template.console_type}</span></span>
                  </div>
                  <div class="mt-3 border-t border-gray-800/70 pt-2.5">
                    <div class="text-[10px] uppercase tracking-[0.05em] text-gray-500">
                      Images on box ({template.images.length})
                    </div>
                    {#if template.images.length === 0}
                      <div class="mt-1.5 flex items-center gap-1.5 text-[11px] text-amber-300/90">
                        <ShieldAlert class="h-3.5 w-3.5" />
                        No image matches — nodes from this template cannot start
                      </div>
                    {:else}
                      <div class="mt-1.5 flex flex-wrap gap-1.5">
                        {#each template.images as image (image.name)}
                          <span
                            class="inline-flex items-center gap-1.5 rounded-full border border-gray-700 px-2 py-0.5 text-[11px] text-gray-300"
                            title={`${image.filename} · ${formatSize(image.size)}`}
                          >
                            <span class={`h-1.5 w-1.5 rounded-full ${image.state === 'available' ? 'bg-emerald-400' : 'bg-red-400'}`}></span>
                            {image.filename}
                            <span class="text-gray-600">{formatSize(image.size)}</span>
                          </span>
                        {/each}
                      </div>
                    {/if}
                  </div>
                </div>
              {/each}
            </div>
          </section>
        {/each}
      </div>
    {:else}
      <section class="overflow-hidden rounded-2xl border border-gray-800 bg-gray-900/95" data-testid="images-panel">
        <table class="w-full text-left text-sm">
          <thead>
            <tr class="border-b border-gray-800 text-[10px] uppercase tracking-[0.05em] text-gray-500">
              <th class="px-5 py-2.5">Image</th>
              <th class="px-3 py-2.5">Type</th>
              <th class="px-3 py-2.5">Size</th>
              <th class="px-3 py-2.5">sha256</th>
              <th class="px-3 py-2.5">Modified</th>
              <th class="px-3 py-2.5">References</th>
              <th class="px-5 py-2.5 text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {#each filteredImages(images) as image (image.path)}
              <tr class="border-b border-gray-800/60 align-top last:border-0 hover:bg-gray-800/40" data-testid={`image-row-${image.name}`}>
                <td class="px-5 py-3">
                  <div class="font-medium text-gray-100">{image.filename}</div>
                  <div class="mt-0.5 flex flex-wrap gap-1">
                    {#each image.templates as match (match.key)}
                      <span
                        class="rounded-full border border-violet-500/40 px-1.5 py-px text-[9px] text-violet-300"
                        title={`template ${match.key} (${match.type}) matches this image`}
                      >{match.name}</span>
                    {/each}
                  </div>
                </td>
                <td class="px-3 py-3 font-mono text-[12px] text-gray-400">{image.type}</td>
                <td class="px-3 py-3 text-[12px] text-gray-300">{formatSize(image.size)}</td>
                <td class="px-3 py-3">
                  {#if image.sha256}
                    <span class="inline-flex items-center gap-1.5">
                      <span class="font-mono text-[11px] text-gray-400" title={image.sha256}>{shortSha(image.sha256)}</span>
                      <button
                        class="rounded p-0.5 text-gray-500 transition hover:text-blue-300"
                        title="Copy full checksum"
                        aria-label="Copy full checksum"
                        on:click={() => void copySha(image)}
                      >
                        {#if copiedSha === image.sha256}
                          <Check class="h-3 w-3 text-emerald-400" />
                        {:else}
                          <Copy class="h-3 w-3" />
                        {/if}
                      </button>
                    </span>
                  {:else if image.type !== 'docker'}
                    <button
                      class="rounded-md border border-gray-700 px-2 py-1 text-[10px] uppercase tracking-[0.05em] text-gray-400 transition hover:border-blue-500 hover:text-white"
                      disabled={verifying === `${image.type}/${image.name}`}
                      on:click={() => void verifyImage(image)}
                      data-testid={`image-verify-${image.name}`}
                    >
                      {verifying === `${image.type}/${image.name}` ? 'hashing…' : 'Verify'}
                    </button>
                  {:else}
                    <span class="text-[11px] text-gray-600">—</span>
                  {/if}
                </td>
                <td class="px-3 py-3 text-[12px] text-gray-400">{image.mtime_iso ?? '—'}</td>
                <td class="px-3 py-3">
                  {#if image.referenced_by.length === 0}
                    <span class="text-[11px] text-gray-600">unreferenced</span>
                  {:else}
                    <div class="flex max-w-xs flex-wrap gap-1">
                      {#each image.referenced_by as ref (`${ref.lab_path}:${ref.node}`)}
                        <span
                          class="rounded-full border border-gray-700 px-1.5 py-px text-[10px] text-gray-400"
                          title={`${ref.lab_name} (${ref.lab_path}) · node ${ref.node}`}
                        >{ref.lab}</span>
                      {/each}
                    </div>
                  {/if}
                </td>
                <td class="px-5 py-3 text-right">
                  {#if image.type === 'docker'}
                    <span class="text-[10px] text-gray-600" title="Managed by the docker daemon">daemon</span>
                  {:else if isReferenced(image)}
                    <span
                      class="inline-flex items-center gap-1 text-[10px] text-gray-600"
                      title="Blocked while referenced by labs"
                      data-testid={`image-delete-blocked-${image.name}`}
                    >
                      <Trash2 class="h-3 w-3" />
                      referenced
                    </span>
                  {:else}
                    <button
                      class="rounded-md border border-gray-700 px-2.5 py-1.5 text-[10px] uppercase tracking-[0.05em] text-gray-400 transition hover:border-red-500 hover:text-red-300"
                      on:click={() => (deleteTarget = image)}
                      data-testid={`image-delete-${image.name}`}
                    >Delete</button>
                  {/if}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </section>
    {/if}
  </div>
</div>

{#if dialogTemplate}
  <TemplateDialog
    template={dialogTemplate}
    on:save={handleTemplateSave}
    on:close={() => (dialogTemplate = null)}
  />
{/if}

{#if deleteTarget}
  <div class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" role="presentation" data-testid="image-delete-confirm">
    <div class="w-full max-w-sm rounded-2xl border border-gray-800 bg-gray-900 p-5 shadow-2xl">
      <h2 class="text-sm font-semibold text-gray-100">Delete {deleteTarget.filename}?</h2>
      <p class="mt-2 text-[12px] text-gray-400">
        Removes {deleteTarget.type === 'docker' ? 'the image' : 'the image directory and all files'} from the host.
      </p>
      <div class="mt-4 flex justify-end gap-2">
        <button
          class="rounded-lg border border-gray-700 px-4 py-2 text-[12px] text-gray-300 transition hover:bg-gray-800"
          on:click={() => (deleteTarget = null)}
          data-testid="image-delete-cancel"
        >Cancel</button>
        <button
          class="rounded-lg bg-red-600 px-4 py-2 text-[12px] font-medium text-white transition hover:bg-red-500"
          on:click={handleDelete}
          data-testid="image-delete-confirm-button"
        >Delete image</button>
      </div>
    </div>
  </div>
{/if}
