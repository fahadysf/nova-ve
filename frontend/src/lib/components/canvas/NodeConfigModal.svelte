<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  import { createEventDispatcher, onDestroy } from 'svelte';
  import type { ComponentType } from 'svelte';
  import { Container, Cpu, HardDrive, Network } from 'lucide-svelte';
  import type {
    NodeCatalog,
    NodeCatalogExtraField,
    NodeCatalogPairedTemplate,
    NodeCatalogTemplate,
    NodeData,
  } from '$lib/types';
  import { apiGetData } from '$lib/api';
  import { validateInterfaceNamingScheme } from '$lib/services/interfaceNaming';

  export let open = false;
  export let mode: 'create' | 'edit' = 'create';
  export let catalog: NodeCatalog | null = null;
  export let node: NodeData | null = null;
  export let submitting = false;
  export let labPath = '';

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

  type CreatePairedSubmitPayload = {
    template_key: string;
    name_overrides: Record<string, string>;
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
    submit:
      | { mode: 'create'; payload: CreateSubmitPayload }
      | { mode: 'create-paired'; payload: CreatePairedSubmitPayload }
      | { mode: 'edit'; nodeId: number; payload: EditSubmitPayload };
  }>();

  const NODE_TYPES: ReadonlyArray<{ key: NodeTypeKey; label: string; icon: ComponentType }> = [
    { key: 'qemu', label: 'QEMU', icon: Cpu },
    { key: 'docker', label: 'Docker', icon: Container },
    { key: 'iol', label: 'IOL', icon: Network },
    { key: 'dynamips', label: 'Dynamips', icon: HardDrive },
  ];
  const DEFAULT_INTERFACE_NAMING = '';
  const DOCKER_INTERFACE_NAMING_FIXED = 'eth{n}';

  // #179 — vendor presets for the interface-naming scheme. Each preset
  // value is a comma-separated list (last entry carries the placeholder).
  // Empty value = (template default); 'custom' = whatever the operator
  // typed by hand. Presets just populate the freeform input — the input
  // remains the source of truth and is independently validated.
  type InterfaceNamingPreset = { label: string; value: string };
  const INTERFACE_NAMING_PRESETS: ReadonlyArray<InterfaceNamingPreset> = [
    { label: '(template default)', value: '' },
    { label: 'Linux — eth{n}', value: 'eth{n}' },
    { label: 'Cisco IOS — Gi{port}', value: 'Gi{port}' },
    { label: 'Cisco IOS-XE — GigabitEthernet0/0/{n}', value: 'GigabitEthernet0/0/{n}' },
    { label: 'Arista — Ethernet{port}', value: 'Ethernet{port}' },
    { label: 'Linux + mgmt0 — mgmt0,eth{n}', value: 'mgmt0,eth{n}' },
    { label: 'Juniper vMX — fxp0,ge-0/0/{n}', value: 'fxp0,ge-0/0/{n}' },
    { label: 'Arista + Management1', value: 'Management1,Ethernet{port}' },
    { label: 'Cisco IOSv-L2', value: 'Management0/0,GigabitEthernet0/{n}' },
  ];
  const INTERFACE_NAMING_CUSTOM = '__custom__';

  let selectedType: NodeTypeKey = 'qemu';
  let selectedTemplateId = '';
  let pairedTemplateKey = '';
  let pairedNameOverrides: Record<string, string> = {};
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
    // #206 — exclude synthetic paired-child entries (paired_parent set). They
    // exist in the catalog so node.template lookups in edit mode + capability
    // gates can resolve them, but they must not surface as standalone choices
    // in the create-flow type tabs (operators reach paired children only via
    // the paired-template panel).
    return (catalog?.templates ?? []).filter(
      (template) => template.type === type && !template.paired_parent
    );
  }

  function pairedTemplates(): NodeCatalogPairedTemplate[] {
    return catalog?.paired_templates ?? [];
  }

  function pairedTemplateForKey(key: string): NodeCatalogPairedTemplate | undefined {
    return pairedTemplates().find((entry) => entry.key === key);
  }

  function setPairedNameOverride(childId: string, value: string) {
    pairedNameOverrides = { ...pairedNameOverrides, [childId]: value };
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

  function interfaceNamingErrorFor(value: string, type: NodeTypeKey): string | null {
    const trimmed = value.trim();
    if (type === 'docker') {
      if (trimmed === '' || trimmed === DOCKER_INTERFACE_NAMING_FIXED) return null;
      return `Docker uses ${DOCKER_INTERFACE_NAMING_FIXED}; the field is system-fixed.`;
    }
    return validateInterfaceNamingScheme(value);
  }

  function normalizedInterfaceNamingScheme(): string | null {
    const trimmed = interfaceNamingScheme.trim();
    return trimmed || null;
  }

  function presetValueFor(scheme: string): string {
    const trimmed = scheme.trim();
    const match = INTERFACE_NAMING_PRESETS.find((preset) => preset.value === trimmed);
    return match ? match.value : INTERFACE_NAMING_CUSTOM;
  }

  function applyInterfaceNamingPreset(presetValue: string) {
    if (presetValue === INTERFACE_NAMING_CUSTOM) {
      return;
    }
    interfaceNamingScheme = presetValue;
    markDirty('interfaceNamingScheme');
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
    pairedTemplateKey = '';
    pairedNameOverrides = {};
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
    // #208c — interfaceNamingError gates only the single-node form. The
    // paired-template path doesn't surface the freeform interface-naming
    // input, so a stale validation error there must NOT block submission.
    if (!pairedActive && interfaceNamingError) return;
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

    if (pairedTemplateKey) {
      const sanitizedOverrides: Record<string, string> = {};
      for (const [childId, value] of Object.entries(pairedNameOverrides)) {
        const trimmed = value.trim();
        if (trimmed) {
          sanitizedOverrides[childId] = trimmed;
        }
      }
      dispatch('submit', {
        mode: 'create-paired',
        payload: {
          template_key: pairedTemplateKey,
          name_overrides: sanitizedOverrides,
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
  $: visiblePairedTemplates = catalog ? pairedTemplates() : [];
  $: selectedPairedTemplate = pairedTemplateKey ? pairedTemplateForKey(pairedTemplateKey) : undefined;
  $: pairedActive = mode === 'create' && Boolean(selectedPairedTemplate);
  $: interfaceNamingError = interfaceNamingErrorFor(interfaceNamingScheme, selectedType);
  $: selectedInterfaceNamingPreset = presetValueFor(interfaceNamingScheme);
  $: signature = `${open}:${mode}:${catalog?.templates.length ?? 0}:${node?.id ?? 'create'}:${node?.status ?? 0}`;
  $: if (open && catalog && signature !== lastSignature) {
    lastSignature = signature;
    initializeForm();
  }

  // ── QEMU computed command preview ────────────────────────────────────────
  type PreviewToken = { token: string; source: 'default' | 'user' | 'actual' };
  let previewTokens: PreviewToken[] = [];
  let previewLoading = false;
  let previewRunning = false;
  let previewError = false;
  let previewTimer: ReturnType<typeof setTimeout> | null = null;

  onDestroy(() => {
    if (previewTimer) clearTimeout(previewTimer);
  });

  async function doFetchPreview() {
    if (!labPath || !node || mode !== 'edit' || selectedTemplate?.type !== 'qemu') {
      previewLoading = false;
      return;
    }
    previewError = false;
    try {
      const data = await apiGetData<{ tokens: PreviewToken[]; running: boolean }>(
        `/labs/${labPath}/nodes/${node.id}/qemu-preview`,
        { suppressToast: true }
      );
      previewTokens = data.tokens;
      previewRunning = data.running;
    } catch {
      previewTokens = [];
      previewError = true;
    } finally {
      previewLoading = false;
    }
  }

  // Only fetch on modal open — preview reflects last-saved config, not in-flight edits.
  $: if (open && mode === 'edit' && selectedTemplate?.type === 'qemu' && node) {
    previewLoading = true;
    if (previewTimer) clearTimeout(previewTimer);
    previewTimer = setTimeout(doFetchPreview, 400);
  } else if (!open) {
    previewTokens = [];
    previewLoading = false;
    previewError = false;
    previewRunning = false;
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
                    {!available || mode === 'edit' || pairedActive ? 'cursor-not-allowed opacity-50' : ''}"
                  disabled={!available || mode === 'edit' || pairedActive}
                  aria-pressed={active}
                  on:click={() => selectType(entry.key)}
                >
                  <svelte:component this={entry.icon} size={22} strokeWidth={1.6} />
                  <span class="text-[11px] font-medium uppercase tracking-[0.06em]">{entry.label}</span>
                </button>
              {/each}
            </div>

            {#if mode === 'create' && visiblePairedTemplates.length > 0}
              <div class="rounded-2xl border border-purple-500/30 bg-purple-500/5 p-4">
                <div class="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <div class="text-[10px] uppercase tracking-[0.06em] text-purple-300">Multi-node templates</div>
                    <div class="mt-0.5 text-xs text-slate-400">Templates that instantiate multiple nodes plus auto-links as a single group.</div>
                  </div>
                  <label class="flex items-center gap-2 text-xs text-slate-300">
                    <select
                      bind:value={pairedTemplateKey}
                      class="rounded-xl border border-slate-800 bg-slate-900 px-3 py-1.5 text-sm text-slate-100"
                    >
                      <option value="">— Single node —</option>
                      {#each visiblePairedTemplates as paired}
                        <option value={paired.key}>
                          {paired.name} [{paired.child_count} nodes / {paired.link_count} link{paired.link_count === 1 ? '' : 's'}]
                        </option>
                      {/each}
                    </select>
                  </label>
                </div>

                {#if selectedPairedTemplate}
                  <div class="mt-4 space-y-3">
                    <div class="rounded-xl border border-slate-800 bg-slate-950/60 p-3">
                      <div class="text-[10px] uppercase tracking-[0.06em] text-slate-500">Will create</div>
                      <ul class="mt-2 space-y-1.5 text-xs text-slate-200">
                        {#each selectedPairedTemplate.children as child}
                          <li class="flex flex-wrap items-center gap-2">
                            <span class="rounded-md border border-slate-700 bg-slate-900 px-2 py-0.5 font-mono text-[10px] uppercase tracking-wide text-slate-300">{child.kind}</span>
                            <span class="font-medium text-slate-100">{child.name}</span>
                            <span class="text-slate-500">·</span>
                            <span>{child.cpu} vCPU · {child.ram} MB · {child.ethernet} eth</span>
                          </li>
                        {/each}
                      </ul>
                    </div>

                    {#if selectedPairedTemplate.links.length > 0}
                      <div class="rounded-xl border border-slate-800 bg-slate-950/60 p-3">
                        <div class="text-[10px] uppercase tracking-[0.06em] text-slate-500">Auto-links</div>
                        <ul class="mt-2 space-y-1 text-xs text-slate-300">
                          {#each selectedPairedTemplate.links as link}
                            <li class="font-mono">
                              {link.from_node}.{link.from_iface} ↔ {link.to_node}.{link.to_iface}
                            </li>
                          {/each}
                        </ul>
                      </div>
                    {/if}

                    <div class="grid gap-2 sm:grid-cols-2">
                      {#each selectedPairedTemplate.children as child}
                        <label class="block">
                          <div class="mb-1 text-[10px] uppercase tracking-[0.05em] text-slate-500">Name · {child.id}</div>
                          <input
                            value={pairedNameOverrides[child.id] ?? ''}
                            on:input={(e) => setPairedNameOverride(child.id, (e.currentTarget as HTMLInputElement).value)}
                            placeholder={child.name}
                            class="w-full rounded-xl border border-slate-800 bg-slate-900 px-3 py-1.5 text-sm text-slate-100"
                          />
                        </label>
                      {/each}
                    </div>
                  </div>
                {/if}
              </div>
            {/if}

            {#if !pairedActive}
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
                  value={selectedInterfaceNamingPreset}
                  on:change={(event) => applyInterfaceNamingPreset((event.currentTarget as HTMLSelectElement).value)}
                  class="mb-1 w-full rounded-xl border border-slate-800 bg-slate-900 px-3 py-1.5 text-sm text-slate-100"
                  aria-label="Interface naming preset"
                >
                  {#each INTERFACE_NAMING_PRESETS as preset}
                    <option value={preset.value}>{preset.label}</option>
                  {/each}
                  <option value={INTERFACE_NAMING_CUSTOM}>Custom…</option>
                </select>
                <input
                  bind:value={interfaceNamingScheme}
                  on:input={() => markDirty('interfaceNamingScheme')}
                  type="text"
                  spellcheck="false"
                  autocomplete="off"
                  placeholder={'eth{n}  or  mgmt0,eth{n}'}
                  aria-invalid={interfaceNamingError ? 'true' : 'false'}
                  class="w-full rounded-xl border bg-slate-900 px-3 py-1.5 font-mono text-sm text-slate-100 {interfaceNamingError ? 'border-red-500/60' : 'border-slate-800'}"
                />
                {#if interfaceNamingError}
                  <div class="mt-1 text-[11px] text-red-400">{interfaceNamingError}</div>
                {:else}
                  <div class="mt-1 text-[11px] text-slate-500">
                    Empty keeps template default. Comma-separated list: only the last entry may use {'{n}'}, {'{slot}'}, or {'{port}'}.
                  </div>
                {/if}
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

            {#if selectedTemplate?.type === 'qemu' && mode === 'edit' && node}
              <div class="rounded-2xl border border-slate-800 bg-slate-900/40 p-4">
                <div class="mb-2 flex items-center gap-2">
                  <div class="text-[10px] uppercase tracking-[0.05em] text-slate-500">Computed command</div>
                  {#if previewRunning}
                    <span class="rounded bg-emerald-500/20 px-1.5 py-0.5 text-[9px] uppercase tracking-wider text-emerald-400">live</span>
                  {/if}
                </div>
                {#if previewLoading}
                  <div class="text-[11px] text-slate-600">Loading…</div>
                {:else if previewError}
                  <div class="text-[11px] text-red-500/70">Preview unavailable.</div>
                {:else if previewTokens.length > 0}
                  <pre class="flex flex-wrap gap-x-1 gap-y-0.5 whitespace-pre-wrap break-all font-mono text-[11px] leading-relaxed">{#each previewTokens as tok}<span class={tok.source === 'actual' ? 'text-emerald-400' : tok.source === 'user' ? 'text-slate-200' : 'text-slate-500'}>{tok.token} </span>{/each}</pre>
                {:else}
                  <div class="text-[11px] text-slate-600">Save changes first, then re-open to refresh preview.</div>
                {/if}
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
            {/if}
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
              disabled={submitting || (!pairedActive && (!selectedTemplate || !image)) || (!pairedActive && !!interfaceNamingError)}
              class="rounded-full border border-blue-500/40 bg-blue-500/15 px-4 py-2 text-sm text-blue-100 transition hover:bg-blue-500/25 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {#if submitting}
                Saving…
              {:else if mode === 'edit'}
                Save node
              {:else if pairedActive && selectedPairedTemplate}
                Add {selectedPairedTemplate.child_count} nodes + {selectedPairedTemplate.link_count} link{selectedPairedTemplate.link_count === 1 ? '' : 's'}
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
