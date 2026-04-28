<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import type { ComponentType } from 'svelte';
  import { Container, Cpu, HardDrive, Network } from 'lucide-svelte';
  import type {
    NodeCatalog,
    NodeCatalogExtraField,
    NodeCatalogTemplate,
    NodeData,
  } from '$lib/types';

  export let open = false;
  export let mode: 'create' | 'edit' = 'create';
  export let catalog: NodeCatalog | null = null;
  export let node: NodeData | null = null;
  export let submitting = false;

  type NodeTypeKey = NodeData['type'];
  type ExtrasMap = Record<string, unknown>;

  type CreateSubmitPayload = {
    type: NodeTypeKey;
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
    extras: ExtrasMap;
    interface_naming_scheme: string | null;
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
    extras: ExtrasMap;
    interface_naming_scheme: string | null;
  };

  const dispatch = createEventDispatcher<{
    cancel: void;
    submit: { mode: 'create'; payload: CreateSubmitPayload } | { mode: 'edit'; nodeId: number; payload: EditSubmitPayload };
  }>();

  const NODE_TYPES: ReadonlyArray<{ key: NodeTypeKey; label: string; icon: ComponentType }> = [
    { key: 'qemu', label: 'QEMU', icon: Cpu },
    { key: 'docker', label: 'Docker', icon: Container },
    { key: 'iol', label: 'IOL', icon: Network },
    { key: 'dynamips', label: 'Dynamips', icon: HardDrive },
  ];
  const DEFAULT_INTERFACE_NAMING = '';
  const COMMON_INTERFACE_NAMING_OPTIONS: ReadonlyArray<{ value: string; label: string }> = [
    { value: DEFAULT_INTERFACE_NAMING, label: 'Default' },
    { value: 'GigabitEthernet0/0/{n}', label: 'GigabitEthernet0/0/{n}' },
    { value: 'eth{n}', label: 'eth{n}' },
    { value: 'Port{n}', label: 'Port{n}' },
  ];
  const DOCKER_INTERFACE_NAMING_OPTIONS: ReadonlyArray<{ value: string; label: string }> = [
    { value: DEFAULT_INTERFACE_NAMING, label: 'Default' },
    { value: 'eth{n}', label: 'eth{n}' },
  ];

  let selectedType: NodeTypeKey = 'qemu';
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
  let extras: ExtrasMap = {};
  let interfaceNamingScheme = DEFAULT_INTERFACE_NAMING;
  let dirty: Record<string, boolean> = {};
  let lastSignature = '';

  function templateId(template: NodeCatalogTemplate): string {
    return `${template.type}:${template.key}`;
  }

  function templateForId(templateValue: string): NodeCatalogTemplate | undefined {
    return (catalog?.templates ?? []).find((template) => templateId(template) === templateValue);
  }

  function templatesForType(type: NodeTypeKey): NodeCatalogTemplate[] {
    return (catalog?.templates ?? []).filter((template) => template.type === type);
  }

  function firstAvailableType(): NodeTypeKey {
    for (const entry of NODE_TYPES) {
      if (templatesForType(entry.key).length > 0) {
        return entry.key;
      }
    }
    return 'qemu';
  }

  function firstTemplateIdOfType(type: NodeTypeKey): string {
    const candidates = templatesForType(type);
    const withImage = candidates.find((template) => template.images.length > 0);
    const chosen = withImage ?? candidates[0];
    return chosen ? templateId(chosen) : '';
  }

  function extrasSchemaFor(template: NodeCatalogTemplate | undefined): NodeCatalogExtraField[] {
    return template?.extras_schema ?? [];
  }

  function interfaceNamingOptionsFor(type: NodeTypeKey): ReadonlyArray<{ value: string; label: string }> {
    return type === 'docker' ? DOCKER_INTERFACE_NAMING_OPTIONS : COMMON_INTERFACE_NAMING_OPTIONS;
  }

  function normalizedInterfaceNamingScheme(): string | null {
    return interfaceNamingScheme || null;
  }

  function defaultExtrasFromTemplate(template: NodeCatalogTemplate | undefined): ExtrasMap {
    const result: ExtrasMap = {};
    for (const field of extrasSchemaFor(template)) {
      result[field.key] = field.default ?? (field.type === 'env' ? [] : '');
    }
    const declared = (template?.defaults?.extras ?? {}) as ExtrasMap;
    for (const [key, value] of Object.entries(declared)) {
      result[key] = value;
    }
    return result;
  }

  function resetDirty() {
    dirty = {};
  }

  function markDirty(field: string) {
    dirty = { ...dirty, [field]: true };
  }

  function initializeForm() {
    if (!catalog) {
      return;
    }

    if (mode === 'edit' && node) {
      const matchingTemplate = catalog.templates.find(
        (template) => template.type === node.type && template.key === node.template
      );
      selectedType = node.type;
      selectedTemplateId = matchingTemplate ? templateId(matchingTemplate) : firstTemplateIdOfType(node.type);
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
      interfaceNamingScheme = node.interface_naming_scheme ?? DEFAULT_INTERFACE_NAMING;
      const baseExtras = defaultExtrasFromTemplate(matchingTemplate);
      const persistedExtras = ((node as unknown as { extras?: ExtrasMap }).extras ?? {}) as ExtrasMap;
      extras = { ...baseExtras, ...persistedExtras };
      resetDirty();
      return;
    }

    selectedType = firstAvailableType();
    const templateValue = firstTemplateIdOfType(selectedType);
    selectedTemplateId = templateValue;
    name = '';
    count = 1;
    placement = 'grid';
    interfaceNamingScheme = DEFAULT_INTERFACE_NAMING;
    resetDirty();
    applyTemplateDefaults(templateForId(templateValue), { force: true });
  }

  function applyTemplateDefaults(
    template: NodeCatalogTemplate | undefined,
    options: { force?: boolean } = {}
  ) {
    const force = options.force === true;
    const setIfClean = <T>(field: string, current: T, next: T): T => {
      if (force || !dirty[field]) {
        return next;
      }
      return current;
    };

    if (!template) {
      image = setIfClean('image', image, '');
      icon = setIfClean('icon', icon, '');
      cpu = setIfClean('cpu', cpu, 1);
      ram = setIfClean('ram', ram, 1024);
      ethernet = setIfClean('ethernet', ethernet, 1);
      consoleMode = setIfClean('consoleMode', consoleMode, 'telnet');
      delay = setIfClean('delay', delay, 0);
      namePrefix = setIfClean('namePrefix', namePrefix, 'Node');
      extras = force ? {} : extras;
      return;
    }

    image = setIfClean('image', image, template.defaults.image);
    icon = setIfClean('icon', icon, template.defaults.icon);
    cpu = setIfClean('cpu', cpu, template.defaults.cpu);
    ram = setIfClean('ram', ram, template.defaults.ram);
    ethernet = setIfClean('ethernet', ethernet, template.defaults.ethernet);
    consoleMode = setIfClean('consoleMode', consoleMode, template.defaults.console_type);
    delay = setIfClean('delay', delay, template.defaults.delay);
    namePrefix = setIfClean('namePrefix', namePrefix, template.name);

    const templateExtras = defaultExtrasFromTemplate(template);
    const nextExtras: ExtrasMap = {};
    for (const [key, value] of Object.entries(templateExtras)) {
      const dirtyKey = `extras.${key}`;
      if (!force && dirty[dirtyKey] && key in extras) {
        nextExtras[key] = extras[key];
      } else {
        nextExtras[key] = value;
      }
    }
    extras = nextExtras;
  }

  function isStoppedOnly(field: string): boolean {
    return mode === 'edit' && node?.status === 2 && (catalog?.runtime_editability.stopped_only ?? []).includes(field);
  }

  function isExtraDisabled(field: NodeCatalogExtraField): boolean {
    return mode === 'edit' && node?.status === 2 && Boolean(field.stoppedOnly);
  }

  function setExtra(key: string, value: unknown) {
    extras = { ...extras, [key]: value };
    markDirty(`extras.${key}`);
  }

  function envEntries(value: unknown): Array<{ key: string; value: string }> {
    if (Array.isArray(value)) {
      return value.map((entry) => {
        if (entry && typeof entry === 'object') {
          const obj = entry as { key?: string; value?: string };
          return { key: String(obj.key ?? ''), value: String(obj.value ?? '') };
        }
        return { key: '', value: '' };
      });
    }
    return [];
  }

  function envSet(key: string, entries: Array<{ key: string; value: string }>) {
    setExtra(key, entries);
  }

  function envAdd(key: string) {
    envSet(key, [...envEntries(extras[key]), { key: '', value: '' }]);
  }

  function envRemove(key: string, index: number) {
    const next = envEntries(extras[key]).slice();
    next.splice(index, 1);
    envSet(key, next);
  }

  function envUpdate(key: string, index: number, field: 'key' | 'value', value: string) {
    const next = envEntries(extras[key]).slice();
    if (!next[index]) {
      return;
    }
    next[index] = { ...next[index], [field]: value };
    envSet(key, next);
  }

  function selectType(type: NodeTypeKey) {
    if (mode === 'edit' || type === selectedType) {
      return;
    }
    if (templatesForType(type).length === 0) {
      return;
    }
    selectedType = type;
    selectedTemplateId = firstTemplateIdOfType(type);
    applyTemplateDefaults(templateForId(selectedTemplateId), { force: true });
    resetDirty();
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
          extras,
          interface_naming_scheme: normalizedInterfaceNamingScheme(),
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
        extras,
        interface_naming_scheme: normalizedInterfaceNamingScheme(),
      },
    });
  }

  function onTemplateChange() {
    applyTemplateDefaults(selectedTemplate);
  }

  $: selectedTemplate = templateForId(selectedTemplateId);
  $: visibleTemplates = catalog ? templatesForType(selectedType) : [];
  $: availableInterfaceNamingOptions = interfaceNamingOptionsFor(selectedType);
  $: if (!availableInterfaceNamingOptions.some((option) => option.value === interfaceNamingScheme)) {
    interfaceNamingScheme = DEFAULT_INTERFACE_NAMING;
  }
  $: signature = `${open}:${mode}:${catalog?.templates.length ?? 0}:${node?.id ?? 'create'}:${node?.status ?? 0}`;
  $: if (open && catalog && signature !== lastSignature) {
    lastSignature = signature;
    initializeForm();
  }
</script>

{#if open}
  <div class="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/70 p-4 backdrop-blur-sm">
    <div
      role="dialog"
      tabindex="-1"
      aria-modal="true"
      aria-label={mode === 'edit' ? 'Edit node' : 'Add node'}
      class="flex max-h-[calc(100dvh-2rem)] w-full max-w-3xl flex-col overflow-hidden rounded-[1.4rem] border border-slate-700 bg-slate-950/95 shadow-2xl shadow-black/40"
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
        <form class="flex flex-1 flex-col overflow-hidden" on:submit|preventDefault={submitForm}>
          <div class="flex-1 space-y-5 overflow-y-auto px-5 py-5">
            <div class="grid grid-cols-2 gap-2 sm:grid-cols-4">
              {#each NODE_TYPES as entry}
                {@const available = templatesForType(entry.key).length > 0}
                {@const active = entry.key === selectedType}
                <button
                  type="button"
                  class="flex flex-col items-center gap-1.5 rounded-2xl border px-3 py-3 transition
                    {active
                      ? 'border-blue-500/60 bg-blue-500/15 text-blue-100'
                      : 'border-slate-800 bg-slate-900/60 text-slate-300 hover:border-slate-600 hover:text-white'}
                    {!available || mode === 'edit' ? 'cursor-not-allowed opacity-50' : ''}"
                  disabled={!available || mode === 'edit'}
                  aria-pressed={active}
                  on:click={() => selectType(entry.key)}
                >
                  <svelte:component this={entry.icon} size={22} strokeWidth={1.6} />
                  <span class="text-[11px] font-medium uppercase tracking-[0.06em]">{entry.label}</span>
                </button>
              {/each}
            </div>

            <div class="grid gap-4 md:grid-cols-3">
              {#if mode === 'create'}
                <label class="block">
                  <div class="mb-1 text-[10px] uppercase tracking-[0.05em] text-slate-500">Template</div>
                  <select
                    bind:value={selectedTemplateId}
                    class="w-full rounded-xl border border-slate-800 bg-slate-900 px-3 py-1.5 text-sm text-slate-100"
                    on:change={onTemplateChange}
                  >
                    {#each visibleTemplates as template}
                      <option value={templateId(template)}>{template.name}</option>
                    {/each}
                  </select>
                </label>
                <label class="block">
                  <div class="mb-1 text-[10px] uppercase tracking-[0.05em] text-slate-500">Name prefix</div>
                  <input
                    bind:value={namePrefix}
                    on:input={() => markDirty('namePrefix')}
                    class="w-full rounded-xl border border-slate-800 bg-slate-900 px-3 py-1.5 text-sm text-slate-100"
                  />
                </label>
                <label class="block">
                  <div class="mb-1 text-[10px] uppercase tracking-[0.05em] text-slate-500">Nodes to add</div>
                  <input
                    bind:value={count}
                    type="number"
                    min="1"
                    max="24"
                    class="w-full rounded-xl border border-slate-800 bg-slate-900 px-3 py-1.5 text-sm text-slate-100"
                  />
                </label>
              {:else}
                <label class="block">
                  <div class="mb-1 text-[10px] uppercase tracking-[0.05em] text-slate-500">Template</div>
                  <input
                    value={selectedTemplate ? `${selectedTemplate.name} (${selectedTemplate.type})` : `${node?.template ?? ''}`}
                    disabled
                    class="w-full rounded-xl border border-slate-800 bg-slate-900 px-3 py-1.5 text-sm text-slate-400"
                  />
                </label>
                <label class="block md:col-span-2">
                  <div class="mb-1 text-[10px] uppercase tracking-[0.05em] text-slate-500">Name</div>
                  <input
                    bind:value={name}
                    on:input={() => markDirty('name')}
                    class="w-full rounded-xl border border-slate-800 bg-slate-900 px-3 py-1.5 text-sm text-slate-100"
                  />
                </label>
              {/if}
            </div>

            <div class="grid gap-4 md:grid-cols-3">
              <label class="block">
                <div class="mb-1 text-[10px] uppercase tracking-[0.05em] text-slate-500">Image</div>
                <select
                  bind:value={image}
                  on:change={() => markDirty('image')}
                  disabled={isStoppedOnly('image')}
                  class="w-full rounded-xl border border-slate-800 bg-slate-900 px-3 py-1.5 text-sm text-slate-100 disabled:text-slate-500"
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
                  on:change={() => markDirty('icon')}
                  class="w-full rounded-xl border border-slate-800 bg-slate-900 px-3 py-1.5 text-sm text-slate-100"
                >
                  {#each selectedTemplate?.icon_options ?? catalog.icon_options as iconOption}
                    <option value={iconOption}>{iconOption}</option>
                  {/each}
                </select>
              </label>

              <label class="block">
                <div class="mb-1 text-[10px] uppercase tracking-[0.05em] text-slate-500">Console</div>
                <select
                  bind:value={consoleMode}
                  on:change={() => markDirty('consoleMode')}
                  disabled={isStoppedOnly('console')}
                  class="w-full rounded-xl border border-slate-800 bg-slate-900 px-3 py-1.5 text-sm text-slate-100 disabled:text-slate-500"
                >
                  <option value="telnet">telnet</option>
                  <option value="vnc">vnc</option>
                  <option value="rdp">rdp</option>
                </select>
              </label>
            </div>

            <div class="grid gap-4 md:grid-cols-3">
              <label class="block">
                <div class="mb-1 text-[10px] uppercase tracking-[0.05em] text-slate-500">CPU</div>
                <input
                  bind:value={cpu}
                  on:input={() => markDirty('cpu')}
                  type="number"
                  min="1"
                  disabled={isStoppedOnly('cpu')}
                  class="w-full rounded-xl border border-slate-800 bg-slate-900 px-3 py-1.5 text-sm text-slate-100 disabled:text-slate-500"
                />
              </label>
              <label class="block">
                <div class="mb-1 text-[10px] uppercase tracking-[0.05em] text-slate-500">RAM (MB)</div>
                <input
                  bind:value={ram}
                  on:input={() => markDirty('ram')}
                  type="number"
                  min="128"
                  step="128"
                  disabled={isStoppedOnly('ram')}
                  class="w-full rounded-xl border border-slate-800 bg-slate-900 px-3 py-1.5 text-sm text-slate-100 disabled:text-slate-500"
                />
              </label>
              <label class="block">
                <div class="mb-1 text-[10px] uppercase tracking-[0.05em] text-slate-500">Ethernets</div>
                <input
                  bind:value={ethernet}
                  on:input={() => markDirty('ethernet')}
                  type="number"
                  min="1"
                  disabled={isStoppedOnly('ethernet')}
                  class="w-full rounded-xl border border-slate-800 bg-slate-900 px-3 py-1.5 text-sm text-slate-100 disabled:text-slate-500"
                />
              </label>
            </div>

            <div class="grid gap-4 md:grid-cols-3">
              <label class="block">
                <div class="mb-1 text-[10px] uppercase tracking-[0.05em] text-slate-500">Delay (s)</div>
                <input
                  bind:value={delay}
                  on:input={() => markDirty('delay')}
                  type="number"
                  min="0"
                  disabled={isStoppedOnly('delay')}
                  class="w-full rounded-xl border border-slate-800 bg-slate-900 px-3 py-1.5 text-sm text-slate-100 disabled:text-slate-500"
                />
              </label>
              <label class="block">
                <div class="mb-1 text-[10px] uppercase tracking-[0.05em] text-slate-500">Interface naming</div>
                <select
                  bind:value={interfaceNamingScheme}
                  on:change={() => markDirty('interfaceNamingScheme')}
                  class="w-full rounded-xl border border-slate-800 bg-slate-900 px-3 py-1.5 text-sm text-slate-100"
                >
                  {#each availableInterfaceNamingOptions as option}
                    <option value={option.value}>{option.label}</option>
                  {/each}
                </select>
                <div class="mt-1 text-[11px] text-slate-500">
                  Default keeps template or platform naming. Set an override to render ports from the selected format.
                </div>
              </label>
              {#if mode === 'create'}
                <label class="block">
                  <div class="mb-1 text-[10px] uppercase tracking-[0.05em] text-slate-500">Placement</div>
                  <select
                    bind:value={placement}
                    class="w-full rounded-xl border border-slate-800 bg-slate-900 px-3 py-1.5 text-sm text-slate-100"
                  >
                    <option value="grid">grid</option>
                    <option value="row">row</option>
                  </select>
                </label>
              {/if}
            </div>

            {#if extrasSchemaFor(selectedTemplate).length > 0}
              <div class="space-y-3 rounded-2xl border border-slate-800 bg-slate-900/40 p-4">
                <div class="text-[10px] uppercase tracking-[0.05em] text-slate-500">
                  {selectedTemplate?.type ?? ''} options
                </div>
                <div class="grid gap-4 md:grid-cols-3">
                  {#each extrasSchemaFor(selectedTemplate) as field}
                    {#if field.type === 'textarea' || field.type === 'env'}
                      <div class="md:col-span-3">
                        <div class="mb-1 text-[10px] uppercase tracking-[0.05em] text-slate-500">{field.label}</div>
                        {#if field.type === 'textarea'}
                          <textarea
                            value={String(extras[field.key] ?? '')}
                            on:input={(event) => setExtra(field.key, (event.target as HTMLTextAreaElement).value)}
                            placeholder={field.placeholder ?? ''}
                            disabled={isExtraDisabled(field)}
                            rows="3"
                            class="w-full rounded-xl border border-slate-800 bg-slate-900 px-3 py-1.5 text-sm text-slate-100 disabled:text-slate-500"
                          ></textarea>
                        {:else}
                          <div class="space-y-2">
                            {#each envEntries(extras[field.key]) as entry, index}
                              <div class="flex items-center gap-2">
                                <input
                                  value={entry.key}
                                  on:input={(event) => envUpdate(field.key, index, 'key', (event.target as HTMLInputElement).value)}
                                  placeholder="KEY"
                                  disabled={isExtraDisabled(field)}
                                  class="w-1/3 rounded-lg border border-slate-800 bg-slate-900 px-2 py-1.5 text-xs text-slate-100 disabled:text-slate-500"
                                />
                                <input
                                  value={entry.value}
                                  on:input={(event) => envUpdate(field.key, index, 'value', (event.target as HTMLInputElement).value)}
                                  placeholder="value"
                                  disabled={isExtraDisabled(field)}
                                  class="flex-1 rounded-lg border border-slate-800 bg-slate-900 px-2 py-1.5 text-xs text-slate-100 disabled:text-slate-500"
                                />
                                <button
                                  type="button"
                                  class="rounded-lg border border-slate-800 px-2 py-1 text-xs text-slate-400 transition hover:border-rose-500/40 hover:text-rose-200"
                                  on:click={() => envRemove(field.key, index)}
                                  disabled={isExtraDisabled(field)}
                                >
                                  ×
                                </button>
                              </div>
                            {/each}
                            <button
                              type="button"
                              class="rounded-lg border border-slate-800 px-3 py-1.5 text-xs text-slate-300 transition hover:border-blue-500/40 hover:text-white disabled:cursor-not-allowed disabled:opacity-60"
                              on:click={() => envAdd(field.key)}
                              disabled={isExtraDisabled(field)}
                            >
                              Add entry
                            </button>
                          </div>
                        {/if}
                        {#if field.description}
                          <div class="mt-1 text-[11px] text-slate-500">{field.description}</div>
                        {/if}
                      </div>
                    {:else}
                      <label class="block">
                        <div class="mb-1 text-[10px] uppercase tracking-[0.05em] text-slate-500">{field.label}</div>
                        {#if field.type === 'select'}
                          <select
                            value={String(extras[field.key] ?? '')}
                            on:change={(event) => setExtra(field.key, (event.target as HTMLSelectElement).value)}
                            disabled={isExtraDisabled(field)}
                            class="w-full rounded-xl border border-slate-800 bg-slate-900 px-3 py-1.5 text-sm text-slate-100 disabled:text-slate-500"
                          >
                            {#each field.options ?? [] as option}
                              <option value={option.value}>{option.label ?? option.value}</option>
                            {/each}
                          </select>
                        {:else if field.type === 'number'}
                          <input
                            type="number"
                            value={Number(extras[field.key] ?? 0)}
                            on:input={(event) => setExtra(field.key, Number((event.target as HTMLInputElement).value))}
                            disabled={isExtraDisabled(field)}
                            placeholder={field.placeholder ?? ''}
                            class="w-full rounded-xl border border-slate-800 bg-slate-900 px-3 py-1.5 text-sm text-slate-100 disabled:text-slate-500"
                          />
                        {:else if field.type === 'readonly'}
                          <input
                            value={String(extras[field.key] ?? '')}
                            disabled
                            class="w-full rounded-xl border border-slate-800 bg-slate-900 px-3 py-1.5 text-sm text-slate-400"
                          />
                        {:else}
                          <input
                            value={String(extras[field.key] ?? '')}
                            on:input={(event) => setExtra(field.key, (event.target as HTMLInputElement).value)}
                            disabled={isExtraDisabled(field)}
                            placeholder={field.placeholder ?? ''}
                            class="w-full rounded-xl border border-slate-800 bg-slate-900 px-3 py-1.5 text-sm text-slate-100 disabled:text-slate-500"
                          />
                        {/if}
                        {#if field.description}
                          <div class="mt-1 text-[11px] text-slate-500">{field.description}</div>
                        {/if}
                      </label>
                    {/if}
                  {/each}
                </div>
              </div>
            {/if}

            {#if selectedTemplate?.capabilities}
              {@const caps = selectedTemplate.capabilities}
              <div class="rounded-2xl border border-slate-700 bg-slate-900/60 p-4" data-testid="capabilities-banner">
                <div class="text-[10px] uppercase tracking-[0.05em] text-slate-500">Capabilities</div>
                <div class="mt-2 flex flex-wrap gap-2 text-[11px]">
                  {#if caps.hotplug}
                    <span class="rounded-full border border-emerald-700/50 bg-emerald-900/30 px-2 py-1 text-emerald-300">
                      Hot-plug supported
                    </span>
                  {:else}
                    <span class="rounded-full border border-amber-700/50 bg-amber-900/30 px-2 py-1 text-amber-300">
                      Restart required to change topology
                    </span>
                  {/if}
                  <span class="rounded-full border border-slate-700 bg-slate-900 px-2 py-1 text-slate-300">
                    max NICs: {caps.max_nics}
                  </span>
                  {#if caps.machine}
                    <span class="rounded-full border border-slate-700 bg-slate-900 px-2 py-1 text-slate-300">
                      machine: {caps.machine}
                    </span>
                  {/if}
                </div>
              </div>
            {/if}

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
          </div>

          <div class="flex justify-end gap-3 border-t border-slate-800 bg-slate-950/80 px-5 py-4">
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
        </form>
      {/if}
    </div>
  </div>
{/if}
