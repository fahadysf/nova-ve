<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  import { createEventDispatcher, onMount } from 'svelte';
  import { X } from 'lucide-svelte';
  import { validateInterfaceNamingScheme } from '$lib/services/interfaceNaming';
  import type {
    LibraryTemplate, LibraryTemplateEdit, NodeCatalogExtraField, TemplateCapabilities,
    TemplateInterfaceNaming
  } from '$lib/types';
  import TemplateExtraField from './TemplateExtraField.svelte';

  const dispatch = createEventDispatcher<{
    save: { template: LibraryTemplate; edit: LibraryTemplateEdit };
    close: void;
  }>();

  export let template: LibraryTemplate;
  export let saving = false;
  export let saveError = '';

  const control = 'mt-1 block w-full rounded-lg border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-100 focus:border-blue-500 focus:outline-none disabled:opacity-60';
  const iconCategories = ['router', 'switch', 'firewall', 'server', 'host'];
  const consolesByType: Record<string, string[]> = {
    qemu: ['telnet', 'vnc'],
    docker: ['telnet', 'vnc', 'rdp'],
    iol: ['telnet'],
    dynamips: ['telnet'],
  };
  type NamingMode = 'automatic' | 'format' | 'explicit';

  function makeDraft(source: LibraryTemplate) {
    const naming = source.interface_naming;
    const namingMode: NamingMode = naming ? ('format' in naming ? 'format' : 'explicit') : 'automatic';
    return {
      name: source.name,
      description: source.description,
      icon_type: source.icon_type,
      cpu: source.cpu,
      ram: source.ram,
      ethernet: source.ethernet,
      cpulimit: source.cpulimit,
      console_type: source.console_type,
      second_console: source.second_console ?? '',
      aliases: source.image_aliases.join('\n'),
      namingMode,
      format: naming && 'format' in naming ? naming.format : '',
      explicit: naming && 'explicit' in naming ? naming.explicit.join('\n') : '',
      machine: source.capabilities?.machine ?? 'q35',
      hotplug: source.capabilities?.hotplug ?? false,
      max_nics: source.capabilities?.max_nics ?? 1,
    };
  }

  let draft: ReturnType<typeof makeDraft>;
  let dialog: HTMLDivElement;
  let extraChanges: Record<string, unknown> = {};
  $: {
    draft = makeDraft(template);
    extraChanges = {};
  }

  $: consoles = consolesByType[template.type] ?? ['telnet'];
  $: secondaryConsoles = consoles.filter((console) => console !== draft.console_type);
  $: namingError = edit.interface_naming === undefined
    ? null
    : validateNaming(draft.namingMode, draft.format, draft.explicit);
  $: edit = buildEdit(template, draft, extraChanges);

  onMount(() => {
    const opener = document.activeElement;
    dialog.focus();
    return () => {
      if (opener instanceof HTMLElement && opener.isConnected) opener.focus();
    };
  });

  function handleDialogKey(event: KeyboardEvent) {
    if (event.key === 'Escape' && !saving) {
      event.preventDefault();
      dispatch('close');
    }
    if (event.key !== 'Tab') return;

    const controls = dialog.querySelectorAll<HTMLElement>(
      'button:enabled, input:enabled, select:enabled, textarea:enabled'
    );
    const first = controls[0];
    const last = controls[controls.length - 1];
    const active = document.activeElement;
    if (!first || active === dialog || (event.shiftKey ? active === first : active === last)) {
      event.preventDefault();
      (event.shiftKey ? last : first)?.focus();
    }
  }

  function setConsole(value: string) {
    draft.console_type = value;
    if (draft.second_console === value) draft.second_console = '';
  }

  function setMachine(value: TemplateCapabilities['machine']) {
    if (value === null) return;
    draft.machine = value;
    if (value === 'pc') draft.hotplug = false;
  }

  function splitList(value: string) {
    return value.split(/[,\r\n]/).map((item) => item.trim()).filter(Boolean);
  }

  function validateNaming(mode: NamingMode, format: string, explicit: string) {
    if (mode === 'format') return format.trim()
      ? validateInterfaceNamingScheme(format)
      : 'Enter an interface name format.';
    if (mode === 'explicit' && !splitList(explicit).length) return 'Enter at least one interface name.';
    return null;
  }

  function buildEdit(source: LibraryTemplate, values: ReturnType<typeof makeDraft>, extras: Record<string, unknown>) {
    const fields = {
      name: values.name,
      description: values.description,
      icon_type: values.icon_type,
      cpu: values.cpu,
      ram: values.ram,
      ethernet: values.ethernet,
      cpulimit: values.cpulimit,
      console_type: values.console_type,
      second_console: values.second_console || null,
    };
    const changed = Object.fromEntries(
      Object.entries(fields).filter(([key, value]) => value !== (source[key as keyof typeof fields] ?? null))
    ) as LibraryTemplateEdit;
    if (changed.name !== undefined) changed.name = changed.name.trim();
    if (changed.description !== undefined) changed.description = changed.description.trim();

    if (values.aliases !== source.image_aliases.join('\n')) {
      const aliases = splitList(values.aliases);
      if (JSON.stringify(aliases) !== JSON.stringify(source.image_aliases)) changed.image_aliases = aliases;
    }

    const initialNaming = source.interface_naming ?? null;
    let naming: TemplateInterfaceNaming | null = null;
    if (values.namingMode === 'format') {
      const original = initialNaming && 'format' in initialNaming ? initialNaming.format : '';
      naming = { format: values.format === original ? original : values.format.trim() };
    }
    if (values.namingMode === 'explicit') {
      const original = initialNaming && 'explicit' in initialNaming ? initialNaming.explicit : [];
      naming = { explicit: values.explicit === original.join('\n') ? original : splitList(values.explicit) };
    }
    if (JSON.stringify(naming) !== JSON.stringify(initialNaming)) changed.interface_naming = naming;

    if (source.capabilities) {
      const capabilities: Partial<TemplateCapabilities> = {};
      if (values.max_nics !== source.capabilities.max_nics) capabilities.max_nics = values.max_nics;
      if (source.type === 'qemu') {
        if (values.machine !== source.capabilities.machine) capabilities.machine = values.machine;
        if (values.hotplug !== source.capabilities.hotplug) capabilities.hotplug = values.hotplug;
      }
      if (Object.keys(capabilities).length) changed.capabilities = capabilities;
    }

    if (Object.keys(extras).length) changed.extras = extras;
    return changed;
  }

  function extraValue(field: NodeCatalogExtraField, configured: Record<string, unknown>, changes: Record<string, unknown>) {
    return field.key in changes
      ? (changes[field.key] ?? field.default)
      : (configured[field.key] ?? field.default);
  }

  function changeExtra(field: NodeCatalogExtraField, value: unknown) {
    const original = template.extras?.[field.key] ?? field.default;
    const unchanged = field.type === 'select'
      ? String(original ?? '') === String(value ?? '')
      : JSON.stringify(original) === JSON.stringify(value);
    extraChanges = { ...extraChanges };
    if (unchanged) delete extraChanges[field.key];
    else extraChanges[field.key] = value;
  }

  function resetExtra(key: string) {
    extraChanges = { ...extraChanges };
    if (key in (template.extras ?? {})) extraChanges[key] = null;
    else delete extraChanges[key];
  }

  function submit() {
    if (saving || namingError || !Object.keys(edit).length) return;
    dispatch('save', { template, edit });
  }
</script>

<div class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-3 sm:p-6" role="presentation" data-testid="template-dialog">
  <div class="flex max-h-full w-full max-w-2xl flex-col rounded-2xl border border-gray-800 bg-gray-900 shadow-2xl"
    role="dialog" aria-modal="true" aria-labelledby="template-dialog-title" tabindex="-1" bind:this={dialog} on:keydown={handleDialogKey}>
    <header class="flex shrink-0 items-center justify-between border-b border-gray-800 px-5 py-3">
      <h2 id="template-dialog-title" class="text-sm font-semibold text-gray-100">
        Edit template <span class="font-mono text-blue-300">{template.key}</span>
      </h2>
      <button
        type="button"
        class="rounded-md p-1 text-gray-400 transition hover:bg-gray-800 hover:text-gray-200 disabled:opacity-50"
        on:click={() => dispatch('close')}
        disabled={saving}
        aria-label="Close"
        data-testid="template-dialog-close"
      >
        <X class="h-4 w-4" />
      </button>
    </header>

    <form class="flex min-h-0 flex-1 flex-col" on:submit|preventDefault={submit}>
      <div class="min-h-0 flex-1 overflow-y-auto px-5 py-4">
        <fieldset class="min-w-0 space-y-5 disabled:opacity-70" disabled={saving}>
          <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <label class="text-xs text-gray-300">
              Display name
              <input class={control} bind:value={draft.name} required data-testid="template-dialog-name" />
            </label>
            <label class="text-xs text-gray-300">
              Icon category
              <select class={control} bind:value={draft.icon_type}>
                {#if !iconCategories.includes(draft.icon_type)}
                  <option value={draft.icon_type}>{draft.icon_type}</option>
                {/if}
                {#each iconCategories as category}
                  <option value={category}>{category}</option>
                {/each}
              </select>
            </label>
            <label class="text-xs text-gray-300 sm:col-span-2">
              Description
              <textarea class={control + ' resize-y'} bind:value={draft.description} rows="2"></textarea>
            </label>
          </div>

          <section class="space-y-2 border-t border-gray-800 pt-4" aria-labelledby="template-defaults-title">
            <h3 id="template-defaults-title" class="text-sm font-semibold text-gray-100">Node defaults</h3>
            <div class="grid grid-cols-2 gap-3 sm:grid-cols-3">
              <label class="text-xs text-gray-300">
                CPU cores
                <input type="number" min="1" required class={control} bind:value={draft.cpu} data-testid="template-dialog-cpu" />
              </label>
              <label class="text-xs text-gray-300">
                RAM (MB)
                <input type="number" min="1" required class={control} bind:value={draft.ram} data-testid="template-dialog-ram" />
              </label>
              <label class="text-xs text-gray-300">
                Interfaces
                <input type="number" min="1" required class={control} bind:value={draft.ethernet} data-testid="template-dialog-ethernet" />
              </label>
              <label class="text-xs text-gray-300">
                CPU limit
                <input type="number" min="0" required class={control} bind:value={draft.cpulimit} />
              </label>
              <label class="text-xs text-gray-300">
                Primary console
                <select class={control} value={draft.console_type} on:change={(event) => setConsole(event.currentTarget.value)}>
                  {#if !consoles.includes(draft.console_type)}
                    <option value={draft.console_type}>{draft.console_type}</option>
                  {/if}
                  {#each consoles as mode}
                    <option value={mode}>{mode}</option>
                  {/each}
                </select>
              </label>
              <label class="text-xs text-gray-300">
                Second console
                <select class={control} bind:value={draft.second_console} data-testid="template-dialog-second-console">
                  <option value="">None</option>
                  {#if draft.second_console && !secondaryConsoles.includes(draft.second_console)}
                    <option value={draft.second_console}>{draft.second_console}</option>
                  {/if}
                  {#each secondaryConsoles as mode}
                    <option value={mode}>{mode}</option>
                  {/each}
                </select>
              </label>
            </div>
          </section>

          <section class="border-t border-gray-800 pt-4">
            <label for="template-image-aliases" class="text-sm font-semibold text-gray-100">Image aliases</label>
            <textarea
              id="template-image-aliases"
              class={control + ' resize-y font-mono'}
              bind:value={draft.aliases}
              rows="3"
              spellcheck={false}
              placeholder="winserver-*"
              aria-describedby="template-image-aliases-help"
              data-testid="template-dialog-aliases"
            ></textarea>
            <p id="template-image-aliases-help" class="mt-1 text-xs leading-relaxed text-gray-400">
              Separate aliases with line breaks or commas. Matching ignores case. Use
              <code>*</code> as a wildcard, as in <code>winserver-*</code>.
              {#if template.type === 'docker'}
                Docker aliases affect matches in the Library. Docker node images come from the Docker catalog.
              {/if}
            </p>
          </section>

          <section class="space-y-2 border-t border-gray-800 pt-4" aria-labelledby="template-network-title">
            <h3 id="template-network-title" class="text-sm font-semibold text-gray-100">Network</h3>
            <div class="grid gap-3 sm:grid-cols-2">
              <label class="text-xs text-gray-300">
                Interface naming
                <select class={control} bind:value={draft.namingMode}>
                  <option value="automatic">Automatic</option>
                  <option value="format">Format</option>
                  <option value="explicit">Explicit names</option>
                </select>
              </label>
              {#if template.capabilities}
                <label class="text-xs text-gray-300">
                  Maximum NICs
                  <input type="number" min="1" required class={control} bind:value={draft.max_nics} />
                </label>
              {/if}
              {#if draft.namingMode === 'format'}
                <div class="text-xs text-gray-300 sm:col-span-2">
                  <label for="template-interface-format">Interface name format</label>
                  <input id="template-interface-format" class={control + ' font-mono'} bind:value={draft.format} required spellcheck={false}
                    placeholder={'mgmt0,eth{n}'} aria-invalid={Boolean(namingError)} aria-describedby="template-interface-format-help" />
                  <span id="template-interface-format-help" class="mt-1 block" class:text-red-300={namingError} class:text-gray-400={!namingError}>
                    {namingError ?? 'Only the last entry may use {n}, {slot}, or {port}.'}
                  </span>
                </div>
              {:else if draft.namingMode === 'explicit'}
                <div class="text-xs text-gray-300 sm:col-span-2">
                  <label for="template-interface-explicit">Explicit interface names</label>
                  <textarea id="template-interface-explicit" class={control + ' resize-y font-mono'} bind:value={draft.explicit}
                    required rows="3" spellcheck={false} aria-invalid={Boolean(namingError)} aria-describedby="template-interface-explicit-help"></textarea>
                  <span id="template-interface-explicit-help" class="mt-1 block" class:text-red-300={namingError} class:text-gray-400={!namingError}>
                    {namingError ?? 'Enter names in interface order, separated by line breaks or commas.'}
                  </span>
                </div>
              {/if}
              {#if template.type === 'qemu' && template.capabilities}
                <div class="text-xs text-gray-300">
                  <label for="template-machine" class="block">QEMU machine</label>
                  <select id="template-machine" class={control} value={draft.machine} on:change={(event) => setMachine(event.currentTarget.value === 'pc' ? 'pc' : 'q35')}>
                    <option value="q35">q35</option>
                    <option value="pc">pc</option>
                  </select>
                  <p class="mt-1 text-gray-400">When changing the machine, update or remove any conflicting <code>-machine</code> flag in the QEMU arguments.</p>
                </div>
                <label class="flex items-center gap-2 self-end py-2 text-xs text-gray-300" title={draft.machine === 'pc' ? 'PCIe hotplug requires q35.' : undefined}>
                  <input type="checkbox" bind:checked={draft.hotplug} disabled={draft.machine === 'pc'} />
                  Allow NIC hotplug
                </label>
              {/if}
            </div>
          </section>

          {#if template.extras_schema?.length}
            <section class="space-y-2 border-t border-gray-800 pt-4" aria-labelledby="template-engine-title">
              <h3 id="template-engine-title" class="text-sm font-semibold text-gray-100">Engine settings</h3>
              <div class="grid gap-3 sm:grid-cols-2">
                {#each template.extras_schema as field (field.key)}
                  <TemplateExtraField {field}
                    value={extraValue(field, template.extras ?? {}, extraChanges)}
                    canReset={field.key in extraChanges ? extraChanges[field.key] !== null : field.key in (template.extras ?? {})}
                    onChange={(value) => changeExtra(field, value)} onReset={() => resetExtra(field.key)} />
                {/each}
              </div>
            </section>
          {/if}
        </fieldset>
      </div>

      <footer class="shrink-0 space-y-2 border-t border-gray-800 px-5 py-3">
        {#if saveError}
          <p class="text-sm text-red-300" role="alert">{saveError}</p>
        {/if}
        <div class="flex justify-end gap-2">
          <button type="button" class="rounded-lg border border-gray-700 px-4 py-2 text-xs text-gray-300 transition hover:bg-gray-800 disabled:opacity-50"
            disabled={saving} on:click={() => dispatch('close')}>Cancel</button>
          <button type="submit" class="rounded-lg bg-blue-600 px-4 py-2 text-xs font-medium text-white transition hover:bg-blue-500 disabled:opacity-50"
            disabled={saving || Boolean(namingError) || !Object.keys(edit).length} data-testid="template-dialog-save">
            {saving ? 'Saving…' : 'Save template'}
          </button>
        </div>
      </footer>
    </form>
  </div>
</div>
