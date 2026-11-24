<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  import { createEventDispatcher, onMount } from 'svelte';
  import type { LibraryTemplate, LibraryTemplateCreate, LibraryTemplateType } from '$lib/types';

  export let action: { kind: 'create' | 'upload' } | { kind: 'remove'; template: LibraryTemplate };
  export let templateTypes: readonly LibraryTemplateType[];
  export let busy = false;
  export let error = '';

  const dispatch = createEventDispatcher<{
    create: LibraryTemplateCreate;
    upload: File;
    remove: LibraryTemplate;
    close: void;
  }>();
  const control = 'mt-1 block w-full rounded-lg border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-100 focus:border-blue-500 focus:outline-none disabled:opacity-60';

  let type: LibraryTemplateType = 'qemu';
  let key = '';
  let name = '';
  let description = '';
  let aliases = '';
  let file: File | undefined;
  let dialog: HTMLDivElement;

  $: revert = action.kind === 'remove' && action.template.source === 'override';
  $: confirmLabel = action.kind === 'create'
    ? 'Create template'
    : action.kind === 'upload'
      ? 'Upload YAML'
      : revert ? 'Revert override' : 'Delete template';
  $: valid = action.kind === 'create'
    ? /^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$/.test(key.trim()) && Boolean(name.trim())
    : action.kind !== 'upload' || Boolean(file);

  onMount(() => {
    const opener = document.activeElement;
    dialog.focus();
    return () => {
      if (opener instanceof HTMLElement && opener.isConnected) opener.focus();
    };
  });

  function handleDialogKey(event: KeyboardEvent) {
    if (event.key === 'Escape' && !busy) {
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

  function submit() {
    if (busy || !valid) return;
    if (action.kind === 'create') {
      const body: LibraryTemplateCreate = { type, key: key.trim(), name: name.trim() };
      if (description.trim()) body.description = description.trim();
      const imageAliases = aliases.split(/[,\r\n]/).map((alias) => alias.trim()).filter(Boolean);
      if (imageAliases.length) body.image_aliases = imageAliases;
      dispatch('create', body);
    } else if (action.kind === 'upload') {
      if (file) dispatch('upload', file);
    } else if (action.kind === 'remove') {
      dispatch('remove', action.template);
    }
  }
</script>

<div class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-3 sm:p-6" role="presentation">
  <div
    class="flex max-h-full w-full max-w-lg flex-col rounded-2xl border border-gray-800 bg-gray-900 shadow-2xl"
    role="dialog"
    aria-modal="true"
    aria-busy={busy}
    aria-labelledby="template-action-title"
    tabindex="-1"
    bind:this={dialog}
    on:keydown={handleDialogKey}
  >
    <h2 id="template-action-title" class="border-b border-gray-800 px-5 py-4 text-sm font-semibold text-gray-100">
      {confirmLabel}{action.kind === 'remove' ? ` for ${action.template.name}?` : ''}
    </h2>
    <form class="flex min-h-0 flex-col" on:submit|preventDefault={submit}>
      <div class="space-y-3 overflow-y-auto px-5 py-4 text-xs text-gray-300">
        {#if action.kind === 'create'}
          <div class="grid gap-3 sm:grid-cols-2">
            <label>
              Template type
              <select class={control} bind:value={type} disabled={busy}>
                {#each templateTypes as option (option)}
                  <option value={option}>{option}</option>
                {/each}
              </select>
            </label>
            <label>
              Template key
              <input
                class={control}
                bind:value={key}
                aria-describedby="template-create-key-help"
                spellcheck="false"
                maxlength="120"
                required
                disabled={busy}
              />
            </label>
          </div>
          <p id="template-create-key-help" class="text-gray-400">
            Start the key with a letter or number. You can also use dots, underscores and hyphens.
          </p>
          <label class="block">
            Display name
            <input class={control} bind:value={name} required disabled={busy} />
          </label>
          <label class="block">
            Description
            <textarea class={control} rows="2" bind:value={description} disabled={busy}></textarea>
          </label>
          <label class="block">
            Image aliases
            <textarea
              class={control}
              rows="2"
              bind:value={aliases}
              placeholder="my-appliance-*"
              aria-describedby="template-create-aliases-help"
              disabled={busy}
            ></textarea>
          </label>
          <p id="template-create-aliases-help" class="text-gray-400">
            Enter one alias per line. Use <code>*</code> as a wildcard.
          </p>
        {:else if action.kind === 'upload'}
          <label class="block">
            Template YAML file
            <input
              class={control}
              type="file"
              accept=".yml,.yaml"
              aria-describedby="template-upload-help"
              disabled={busy}
              on:change={(event) => (file = event.currentTarget.files?.[0])}
            />
          </label>
          <p id="template-upload-help" class="text-gray-400">
            Choose a .yml or .yaml file with a template definition. The filename sets the template key.
          </p>
        {:else if revert}
          <p>Remove the saved override and restore the shipped template settings.</p>
        {:else}
          <p>Remove this custom template from the library.</p>
        {/if}
        {#if error}
          <p class="rounded-lg border border-red-800 bg-red-950/60 px-3 py-2 text-red-200" role="alert">{error}</p>
        {/if}
      </div>
      <div class="flex justify-end gap-2 border-t border-gray-800 px-5 py-3">
        <button
          type="button"
          class="rounded-lg border border-gray-700 px-4 py-2 text-xs text-gray-300 hover:bg-gray-800 disabled:opacity-60"
          on:click={() => dispatch('close')}
          disabled={busy}
        >Cancel</button>
        <button
          type="submit"
          class={`rounded-lg px-4 py-2 text-xs font-medium text-white disabled:opacity-60 ${action.kind === 'remove' ? 'bg-red-600 hover:bg-red-500' : 'bg-blue-600 hover:bg-blue-500'}`}
          disabled={busy || !valid}
        >{busy ? 'Working…' : confirmLabel}</button>
      </div>
    </form>
  </div>
</div>
