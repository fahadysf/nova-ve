<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import type { NodeCatalog, NodeCatalogTemplate, NodeData } from '$lib/types';

  export let open = false;
  export let mode: 'create' | 'edit' = 'create';
  export let catalog: NodeCatalog | null = null;
  export let node: NodeData | null = null;
  export let submitting = false;

  type CreateSubmitPayload = {
    type: NodeCatalogTemplate['type'];
    template: string;
    image: string;
    name_prefix: string;
    count: number;
    placement: 'grid' | 'row';
    icon: string;
    cpu: number;
    ram: number;
    ethernet: number;
    console: NodeData['console'];
    delay: number;
  };

  type EditSubmitPayload = {
    name: string;
    image: string;
    icon: string;
    cpu: number;
    ram: number;
    ethernet: number;
    console: NodeData['console'];
    delay: number;
  };

  const dispatch = createEventDispatcher<{
    cancel: void;
    submit: { mode: 'create'; payload: CreateSubmitPayload } | { mode: 'edit'; nodeId: number; payload: EditSubmitPayload };
  }>();

  let selectedTemplateId = '';
  let name = '';
  let namePrefix = '';
  let image = '';
  let icon = '';
  let count = 1;
  let placement: 'grid' | 'row' = 'grid';
  let cpu = 1;
  let ram = 1024;
  let ethernet = 1;
  let consoleMode: NodeData['console'] = 'telnet';
  let delay = 0;
  let lastSignature = '';

  function templateId(template: NodeCatalogTemplate): string {
    return `${template.type}:${template.key}`;
  }

  function templateForId(templateValue: string): NodeCatalogTemplate | undefined {
    return (catalog?.templates ?? []).find((template) => templateId(template) === templateValue);
  }

  function firstTemplateId(): string {
    const template = (catalog?.templates ?? []).find((item) => item.images.length > 0) ?? catalog?.templates?.[0];
    return template ? templateId(template) : '';
  }

  function initializeForm() {
    if (!catalog) {
      return;
    }

    if (mode === 'edit' && node) {
      const matchingTemplate = catalog.templates.find(
        (template) => template.type === node.type && template.key === node.template
      );
      selectedTemplateId = matchingTemplate ? templateId(matchingTemplate) : firstTemplateId();
      name = node.name;
      namePrefix = node.name;
      image = node.image;
      icon = node.icon;
      count = 1;
      placement = 'grid';
      cpu = node.cpu;
      ram = node.ram;
      ethernet = node.ethernet;
      consoleMode = node.console;
      delay = node.delay ?? 0;
      return;
    }

    const templateValue = firstTemplateId();
    selectedTemplateId = templateValue;
    name = '';
    count = 1;
    placement = 'grid';
    applyTemplateDefaults(templateForId(templateValue));
  }

  function applyTemplateDefaults(template: NodeCatalogTemplate | undefined) {
    if (!template) {
      image = '';
      icon = '';
      cpu = 1;
      ram = 1024;
      ethernet = 1;
      consoleMode = 'telnet';
      delay = 0;
      namePrefix = 'Node';
      return;
    }

    image = template.defaults.image;
    icon = template.defaults.icon;
    cpu = template.defaults.cpu;
    ram = template.defaults.ram;
    ethernet = template.defaults.ethernet;
    consoleMode = template.defaults.console;
    delay = template.defaults.delay;
    namePrefix = template.name;
  }

  function isStoppedOnly(field: string): boolean {
    return mode === 'edit' && node?.status === 2 && (catalog?.runtime_editability.stopped_only ?? []).includes(field);
  }

  function submitForm() {
    if (mode === 'edit' && node) {
      dispatch('submit', {
        mode: 'edit',
        nodeId: node.id,
        payload: {
          name,
          image,
          icon,
          cpu,
          ram,
          ethernet,
          console: consoleMode,
          delay,
        },
      });
      return;
    }

    const template = templateForId(selectedTemplateId);
    if (!template) {
      return;
    }

    dispatch('submit', {
      mode: 'create',
      payload: {
        type: template.type,
        template: template.key,
        image,
        name_prefix: namePrefix,
        count,
        placement,
        icon,
        cpu,
        ram,
        ethernet,
        console: consoleMode,
        delay,
      },
    });
  }

  $: selectedTemplate = templateForId(selectedTemplateId);
  $: signature = `${open}:${mode}:${catalog?.templates.length ?? 0}:${node?.id ?? 'create'}:${node?.status ?? 0}`;
  $: if (open && catalog && signature !== lastSignature) {
    lastSignature = signature;
    initializeForm();
  }
</script>

{#if open}
  <div class="absolute inset-0 z-40 flex items-center justify-center bg-slate-950/70 p-4 backdrop-blur-sm">
    <div
      role="dialog"
      tabindex="-1"
      aria-modal="true"
      aria-label={mode === 'edit' ? 'Edit node' : 'Add node'}
      class="w-full max-w-3xl overflow-hidden rounded-[1.4rem] border border-slate-700 bg-slate-950/95 shadow-2xl shadow-black/40"
    >
      <div class="flex items-center justify-between border-b border-slate-800 px-5 py-4">
        <div>
          <div class="text-[10px] uppercase tracking-[0.05em] text-slate-500">
            {mode === 'edit' ? 'Edit node' : 'Add node'}
          </div>
          <div class="mt-1 text-sm font-semibold text-slate-100">
            {#if mode === 'edit'}
              {node?.name ?? 'Node'}
            {:else}
              Configure node properties
            {/if}
          </div>
        </div>
        <button
          type="button"
          class="inline-flex h-8 w-8 items-center justify-center rounded-full border border-slate-800 bg-slate-900 text-slate-300 transition hover:border-blue-500/40 hover:text-white"
          aria-label="Close node configuration modal"
          on:click={() => dispatch('cancel')}
        >
          ×
        </button>
      </div>

      {#if !catalog}
        <div class="px-5 py-10 text-sm text-slate-400">Loading node catalog…</div>
      {:else}
        <form
          class="grid gap-5 px-5 py-5 md:grid-cols-[1.2fr_0.8fr]"
          on:submit|preventDefault={submitForm}
        >
          <div class="space-y-4">
            {#if mode === 'create'}
              <label class="block">
                <div class="mb-1 text-[10px] uppercase tracking-[0.05em] text-slate-500">Template</div>
                <select
                  bind:value={selectedTemplateId}
                  class="w-full rounded-xl border border-slate-800 bg-slate-900 px-3 py-2.5 text-sm text-slate-100"
                  on:change={() => applyTemplateDefaults(selectedTemplate)}
                >
                  {#each catalog.templates as template}
                    <option value={templateId(template)}>{template.name} ({template.type})</option>
                  {/each}
                </select>
              </label>
            {:else}
              <div class="grid gap-4 md:grid-cols-2">
                <label class="block">
                  <div class="mb-1 text-[10px] uppercase tracking-[0.05em] text-slate-500">Template</div>
                  <input
                    value={selectedTemplate ? `${selectedTemplate.name} (${selectedTemplate.type})` : `${node?.template ?? ''}`}
                    disabled
                    class="w-full rounded-xl border border-slate-800 bg-slate-900 px-3 py-2.5 text-sm text-slate-400"
                  />
                </label>
                <label class="block">
                  <div class="mb-1 text-[10px] uppercase tracking-[0.05em] text-slate-500">Name</div>
                  <input
                    bind:value={name}
                    class="w-full rounded-xl border border-slate-800 bg-slate-900 px-3 py-2.5 text-sm text-slate-100"
                  />
                </label>
              </div>
            {/if}

            <div class="grid gap-4 md:grid-cols-2">
              {#if mode === 'create'}
                <label class="block">
                  <div class="mb-1 text-[10px] uppercase tracking-[0.05em] text-slate-500">Name prefix</div>
                  <input
                    bind:value={namePrefix}
                    class="w-full rounded-xl border border-slate-800 bg-slate-900 px-3 py-2.5 text-sm text-slate-100"
                  />
                </label>
                <label class="block">
                  <div class="mb-1 text-[10px] uppercase tracking-[0.05em] text-slate-500">Nodes to add</div>
                  <input
                    bind:value={count}
                    type="number"
                    min="1"
                    max="24"
                    class="w-full rounded-xl border border-slate-800 bg-slate-900 px-3 py-2.5 text-sm text-slate-100"
                  />
                </label>
              {/if}

              <label class="block">
                <div class="mb-1 text-[10px] uppercase tracking-[0.05em] text-slate-500">Image</div>
                <select
                  bind:value={image}
                  disabled={isStoppedOnly('image')}
                  class="w-full rounded-xl border border-slate-800 bg-slate-900 px-3 py-2.5 text-sm text-slate-100 disabled:text-slate-500"
                >
                  {#each selectedTemplate?.images ?? [] as imageOption}
                    <option value={imageOption.image}>{imageOption.image}</option>
                  {/each}
                </select>
              </label>

              <label class="block">
                <div class="mb-1 text-[10px] uppercase tracking-[0.05em] text-slate-500">Icon</div>
                <select
                  bind:value={icon}
                  class="w-full rounded-xl border border-slate-800 bg-slate-900 px-3 py-2.5 text-sm text-slate-100"
                >
                  {#each selectedTemplate?.icon_options ?? catalog.icon_options as iconOption}
                    <option value={iconOption}>{iconOption}</option>
                  {/each}
                </select>
              </label>
            </div>

            <div class="grid gap-4 md:grid-cols-2">
              <label class="block">
                <div class="mb-1 text-[10px] uppercase tracking-[0.05em] text-slate-500">CPU</div>
                <input
                  bind:value={cpu}
                  type="number"
                  min="1"
                  disabled={isStoppedOnly('cpu')}
                  class="w-full rounded-xl border border-slate-800 bg-slate-900 px-3 py-2.5 text-sm text-slate-100 disabled:text-slate-500"
                />
              </label>
              <label class="block">
                <div class="mb-1 text-[10px] uppercase tracking-[0.05em] text-slate-500">RAM (MB)</div>
                <input
                  bind:value={ram}
                  type="number"
                  min="128"
                  step="128"
                  disabled={isStoppedOnly('ram')}
                  class="w-full rounded-xl border border-slate-800 bg-slate-900 px-3 py-2.5 text-sm text-slate-100 disabled:text-slate-500"
                />
              </label>
              <label class="block">
                <div class="mb-1 text-[10px] uppercase tracking-[0.05em] text-slate-500">Ethernets</div>
                <input
                  bind:value={ethernet}
                  type="number"
                  min="1"
                  disabled={isStoppedOnly('ethernet')}
                  class="w-full rounded-xl border border-slate-800 bg-slate-900 px-3 py-2.5 text-sm text-slate-100 disabled:text-slate-500"
                />
              </label>
              <label class="block">
                <div class="mb-1 text-[10px] uppercase tracking-[0.05em] text-slate-500">Delay (s)</div>
                <input
                  bind:value={delay}
                  type="number"
                  min="0"
                  disabled={isStoppedOnly('delay')}
                  class="w-full rounded-xl border border-slate-800 bg-slate-900 px-3 py-2.5 text-sm text-slate-100 disabled:text-slate-500"
                />
              </label>
            </div>
          </div>

          <div class="space-y-4 rounded-[1.2rem] border border-slate-800 bg-slate-900/60 p-4">
            <label class="block">
              <div class="mb-1 text-[10px] uppercase tracking-[0.05em] text-slate-500">Console</div>
              <select
                bind:value={consoleMode}
                disabled={isStoppedOnly('console')}
                class="w-full rounded-xl border border-slate-800 bg-slate-950 px-3 py-2.5 text-sm text-slate-100 disabled:text-slate-500"
              >
                <option value="telnet">telnet</option>
                <option value="vnc">vnc</option>
                <option value="rdp">rdp</option>
              </select>
            </label>

            {#if mode === 'create'}
              <label class="block">
                <div class="mb-1 text-[10px] uppercase tracking-[0.05em] text-slate-500">Placement</div>
                <select
                  bind:value={placement}
                  class="w-full rounded-xl border border-slate-800 bg-slate-950 px-3 py-2.5 text-sm text-slate-100"
                >
                  <option value="grid">grid</option>
                  <option value="row">row</option>
                </select>
              </label>
            {/if}

            <div class="rounded-2xl border border-slate-800 bg-slate-950/80 p-4">
              <div class="text-[10px] uppercase tracking-[0.05em] text-slate-500">Runtime editability</div>
              {#if mode === 'edit' && node?.status === 2}
                <div class="mt-2 text-xs text-amber-200">
                  Stop the node before changing image, CPU, RAM, interface count, console, or delay.
                </div>
              {:else}
                <div class="mt-2 text-xs text-slate-400">
                  Metadata fields are always safe. Runtime fields are editable while the node is stopped.
                </div>
              {/if}
            </div>

            <div class="rounded-2xl border border-slate-800 bg-slate-950/80 p-4">
              <div class="text-[10px] uppercase tracking-[0.05em] text-slate-500">Template summary</div>
              <div class="mt-2 text-sm text-slate-100">{selectedTemplate?.name ?? 'No template selected'}</div>
              {#if selectedTemplate?.description}
                <div class="mt-2 text-xs text-slate-400">{selectedTemplate.description}</div>
              {/if}
              <div class="mt-3 flex flex-wrap gap-2 text-[11px] text-slate-300">
                <span class="rounded-full border border-slate-800 px-2 py-1">{selectedTemplate?.type ?? 'n/a'}</span>
                <span class="rounded-full border border-slate-800 px-2 py-1">{image || 'no image'}</span>
              </div>
            </div>

            <div class="flex justify-end gap-3 pt-2">
              <button
                type="button"
                class="rounded-full border border-slate-800 px-4 py-2 text-sm text-slate-300 transition hover:border-blue-500/40 hover:text-white"
                on:click={() => dispatch('cancel')}
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={submitting || !selectedTemplate || !image}
                class="rounded-full border border-blue-500/40 bg-blue-500/15 px-4 py-2 text-sm text-blue-100 transition hover:bg-blue-500/25 disabled:cursor-not-allowed disabled:opacity-60"
              >
                {#if submitting}
                  Saving…
                {:else if mode === 'edit'}
                  Save node
                {:else}
                  Add node
                {/if}
              </button>
            </div>
          </div>
        </form>
      {/if}
    </div>
  </div>
{/if}
