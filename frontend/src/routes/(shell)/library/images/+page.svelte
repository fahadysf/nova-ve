<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<!--
  Images & Templates library (#264). Static preview: the matching,
  checksums, and lab references render from imageLibraryPreview until the
  backend surfaces real template/image inventories.
-->
<script lang="ts">
  import PageHeader from '$lib/components/shell/PageHeader.svelte';
  import {
    IMAGE_PREVIEWS,
    TEMPLATE_PREVIEWS,
    imageMatchesPattern,
    matchesTemplateType,
    type TemplateType,
  } from '$lib/services/imageLibraryPreview';

  type Tab = 'templates' | 'images';
  let tab: Tab = 'templates';
  let search = '';
  let typeFilter: TemplateType | 'all' = 'all';

  const TYPE_ORDER: readonly TemplateType[] = ['qemu', 'docker', 'iol', 'dynamips'];

  $: query = search.trim().toLowerCase();
  $: visibleTemplates = TEMPLATE_PREVIEWS.filter(
    (t) =>
      (typeFilter === 'all' || t.type === typeFilter) &&
      (!query ||
        t.name.toLowerCase().includes(query) ||
        t.key.toLowerCase().includes(query) ||
        t.imagePattern.toLowerCase().includes(query))
  );
  $: visibleImages = IMAGE_PREVIEWS.filter(
    (i) =>
      (typeFilter === 'all' || i.type === typeFilter) &&
      (!query || i.filename.toLowerCase().includes(query))
  );

  function templateMatches(key: string, pattern: string) {
    return IMAGE_PREVIEWS.filter(
      (i) => matchesTemplateType(i, byType(key)) && imageMatchesPattern(i.filename, pattern)
    );
  }

  function byType(key: string): TemplateType {
    const t = TEMPLATE_PREVIEWS.find((candidate) => candidate.key === key);
    return t?.type ?? 'qemu';
  }

  function formatSize(bytes: number): string {
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let value = bytes;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
      value /= 1024;
      unit += 1;
    }
    return `${value.toFixed(value >= 100 || unit === 0 ? 0 : 1)} ${units[unit]}`;
  }

  function shortChecksum(sha: string): string {
    return `${sha.slice(0, 12)}…${sha.slice(-6)}`;
  }

  function labsForImage(filename: string): string[] {
    return IMAGE_PREVIEWS.find((i) => i.filename === filename)?.referencedByLabs ?? [];
  }

  const typeChip: Record<TemplateType, string> = {
    qemu: 'border-sky-500/40 text-sky-300',
    docker: 'border-blue-500/40 text-blue-300',
    iol: 'border-violet-500/40 text-violet-300',
    dynamips: 'border-amber-500/40 text-amber-300',
  };
</script>

<div class="p-6">
  <div class="space-y-6">
    <PageHeader issue={264}>
      <span slot="kicker" class="text-[10px] uppercase tracking-[0.05em] text-gray-500">
        Library · Preview data
      </span>
      <span slot="title">Images &amp; Templates</span>
      <span slot="actions">
        <button
          class="rounded-md border border-gray-700 px-3 py-2 text-[10px] uppercase tracking-[0.05em] text-gray-300 transition hover:border-blue-500 hover:text-white"
          title="Planned — verify on-box image checksums against catalog entries"
          disabled
        >Verify checksums</button>
        <button
          class="rounded-md border border-blue-500 px-3 py-2 text-[10px] uppercase tracking-[0.05em] text-blue-200 transition hover:bg-blue-500/10"
          title="Planned — upload or register a new image on the host"
          disabled
        >Upload image</button>
      </span>
    </PageHeader>

    <!-- Tabs + filters -->
    <section class="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-gray-800 bg-gray-900/95 px-4 py-3">
      <div class="flex rounded-lg border border-gray-800 bg-gray-950/60 p-0.5" role="tablist" aria-label="Library views">
        <button
          role="tab"
          aria-selected={tab === 'templates'}
          class={`rounded-md px-3 py-1.5 text-[11px] uppercase tracking-[0.05em] transition ${
            tab === 'templates' ? 'bg-blue-500/20 text-blue-100' : 'text-gray-400 hover:text-gray-200'
          }`}
          on:click={() => (tab = 'templates')}
          data-testid="images-tab-templates"
        >Templates</button>
        <button
          role="tab"
          aria-selected={tab === 'images'}
          class={`rounded-md px-3 py-1.5 text-[11px] uppercase tracking-[0.05em] transition ${
            tab === 'images' ? 'bg-blue-500/20 text-blue-100' : 'text-gray-400 hover:text-gray-200'
          }`}
          on:click={() => (tab = 'images')}
          data-testid="images-tab-images"
        >Images</button>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <input
          type="search"
          placeholder="Search name or pattern…"
          bind:value={search}
          class="w-56 rounded-lg border border-gray-700 bg-gray-950 px-3 py-1.5 text-[12px] text-gray-200 outline-none transition focus:border-blue-500"
          data-testid="images-search"
        />
        <select
          bind:value={typeFilter}
          class="rounded-lg border border-gray-700 bg-gray-950 px-2 py-1.5 text-[12px] text-gray-200 outline-none transition focus:border-blue-500"
          data-testid="images-type-filter"
        >
          <option value="all">All types</option>
          {#each TYPE_ORDER as t}
            <option value={t}>{t}</option>
          {/each}
        </select>
      </div>
    </section>

    {#if tab === 'templates'}
      <!-- Templates: grouped by type, each card lists its matching on-box images -->
      <section class="space-y-5" data-testid="templates-panel">
        {#each TYPE_ORDER as type}
          {@const group = visibleTemplates.filter((t) => t.type === type)}
          {#if group.length > 0}
            <div class="space-y-3">
              <div class="text-[10px] uppercase tracking-[0.05em] text-gray-500">{type}</div>
              <div class="grid grid-cols-1 gap-4 lg:grid-cols-2">
                {#each group as template (template.key)}
                  {@const matches = templateMatches(template.key, template.imagePattern)}
                  <article class="rounded-xl border border-gray-700 bg-gray-800 p-4">
                    <div class="flex items-start justify-between gap-3">
                      <div class="min-w-0">
                        <div class="flex items-center gap-2">
                          <span class="truncate font-medium text-gray-100">{template.name}</span>
                          <span class={`rounded-full border px-1.5 py-px text-[9px] uppercase tracking-[0.05em] ${typeChip[template.type]}`}>
                            {template.type}
                          </span>
                        </div>
                        <div class="mt-1 truncate text-[12px] text-gray-500">{template.description}</div>
                      </div>
                      <button
                        class="shrink-0 rounded-md border border-gray-700 px-2.5 py-1.5 text-[10px] uppercase tracking-[0.05em] text-gray-300 transition hover:border-blue-500 hover:text-white"
                        title="Planned — edit template defaults"
                        disabled
                      >Edit</button>
                    </div>
                    <dl class="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 text-[11px] text-gray-400 sm:grid-cols-4">
                      <div><dt class="text-gray-600">RAM</dt><dd>{template.ramMb} MB</dd></div>
                      <div><dt class="text-gray-600">Ethernet</dt><dd>{template.ethernet}×</dd></div>
                      <div><dt class="text-gray-600">Console</dt><dd>{template.console}</dd></div>
                      <div class="col-span-2 sm:col-span-1"><dt class="text-gray-600">Pattern</dt><dd class="truncate font-mono">{template.imagePattern}</dd></div>
                    </dl>
                    <div class="mt-3 border-t border-gray-700/60 pt-3">
                      <div class="text-[10px] uppercase tracking-[0.05em] text-gray-600">
                        Images on box ({matches.length})
                      </div>
                      {#if matches.length === 0}
                        <div class="mt-1.5 text-[12px] text-amber-300/90">
                          No image matches — nodes from this template cannot start.
                        </div>
                      {:else}
                        <div class="mt-1.5 flex flex-wrap gap-1.5">
                          {#each matches as image (image.id)}
                            <span class="rounded-full border border-gray-700 bg-gray-950/60 px-2 py-px font-mono text-[10px] text-gray-300" title={`${image.filename} · ${formatSize(image.sizeBytes)} · ${image.referencedByLabs.length} lab(s)`}>
                              {image.filename}
                            </span>
                          {/each}
                        </div>
                      {/if}
                    </div>
                  </article>
                {/each}
              </div>
            </div>
          {/if}
        {/each}
      </section>
    {:else}
      <!-- Images: everything on the host with size, checksum, references -->
      <section class="overflow-hidden rounded-2xl border border-gray-800 bg-gray-900/95" data-testid="images-panel">
        <table class="w-full text-left text-sm">
          <thead>
            <tr class="border-b border-gray-800 text-[10px] uppercase tracking-[0.05em] text-gray-500">
              <th class="px-5 py-2.5">Image</th>
              <th class="px-3 py-2.5">Type</th>
              <th class="px-3 py-2.5">Size</th>
              <th class="px-3 py-2.5">Checksum (sha256)</th>
              <th class="px-3 py-2.5">Modified</th>
              <th class="px-3 py-2.5">Referenced by</th>
              <th class="px-3 py-2.5">State</th>
              <th class="px-5 py-2.5 text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {#each visibleImages as image (image.id)}
              <tr class="border-b border-gray-800/60 last:border-0 hover:bg-gray-800/40">
                <td class="px-5 py-3 font-mono text-[12px] text-gray-100">{image.filename}</td>
                <td class="px-3 py-3">
                  <span class={`rounded-full border px-1.5 py-px text-[9px] uppercase tracking-[0.05em] ${typeChip[image.type]}`}>
                    {image.type}
                  </span>
                </td>
                <td class="px-3 py-3 text-[12px] text-gray-300">{formatSize(image.sizeBytes)}</td>
                <td class="px-3 py-3 font-mono text-[11px] text-gray-500" title={image.sha256}>
                  {shortChecksum(image.sha256)}
                </td>
                <td class="px-3 py-3 text-[12px] text-gray-400">{image.modified}</td>
                <td class="px-3 py-3">
                  {#if labsForImage(image.filename).length === 0}
                    <span class="text-[12px] text-gray-600">—</span>
                  {:else}
                    <div class="flex max-w-xs flex-wrap gap-1">
                      {#each labsForImage(image.filename) as labPath}
                        <span class="rounded-full border border-gray-700 bg-gray-950/60 px-2 py-px font-mono text-[10px] text-gray-300" title={labPath}>
                          {labPath.split('/').filter(Boolean).pop()}
                        </span>
                      {/each}
                    </div>
                  {/if}
                </td>
                <td class="px-3 py-3">
                  <span class="inline-flex items-center gap-1.5 text-[12px]">
                    <span class={`h-1.5 w-1.5 rounded-full ${image.state === 'available' ? 'bg-emerald-400' : 'bg-red-400'}`}></span>
                    <span class={image.state === 'available' ? 'text-gray-300' : 'text-red-300'}>{image.state}</span>
                  </span>
                </td>
                <td class="px-5 py-3 text-right">
                  <button
                    class="rounded-md border border-gray-700 px-2.5 py-1.5 text-[10px] uppercase tracking-[0.05em] text-gray-300 transition hover:border-red-500/60 hover:text-red-200 disabled:hover:border-gray-700 disabled:hover:text-gray-300"
                    title={labsForImage(image.filename).length > 0
                      ? 'Blocked — image is referenced by labs'
                      : 'Planned — remove image from host'}
                    disabled
                  >Delete</button>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
        {#if visibleImages.length === 0}
          <div class="px-5 py-10 text-center text-sm text-gray-500">No images match the current filter.</div>
        {/if}
      </section>
    {/if}
  </div>
</div>
