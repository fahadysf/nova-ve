<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<!--
  Template edit dialog (#264). Persists editable defaults to the backend
  template definitions — builtin templates are shadowed under the user
  template directory, user templates are edited in place.
-->
<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { X } from 'lucide-svelte';
  import type { LibraryTemplate, LibraryTemplateEdit } from '$lib/types';

  const dispatch = createEventDispatcher<{
    save: { template: LibraryTemplate; edit: LibraryTemplateEdit };
    close: void;
  }>();

  export let template: LibraryTemplate;

  let name = '';
  let cpu = 1;
  let ram = 1024;
  let ethernet = 1;
  let consoleType = '';
  let aliases = '';

  $: {
    name = template.name;
    cpu = template.cpu;
    ram = template.ram;
    ethernet = template.ethernet;
    consoleType = template.console_type;
    aliases = template.image_aliases.join(', ');
  }

  function submit() {
    dispatch('save', {
      template,
      edit: {
        name: name.trim(),
        cpu: Math.trunc(cpu) || 1,
        ram: Math.trunc(ram) || 1,
        ethernet: Math.trunc(ethernet) || 1,
        console_type: consoleType.trim(),
        image_aliases: aliases
          .split(',')
          .map((item) => item.trim())
          .filter(Boolean)
      }
    });
  }
</script>

<div
  class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
  role="presentation"
  data-testid="template-dialog"
>
  <div class="w-full max-w-lg rounded-2xl border border-gray-800 bg-gray-900 shadow-2xl">
    <div class="flex items-center justify-between border-b border-gray-800 px-5 py-3">
      <h2 class="text-sm font-semibold text-gray-100">
        Edit template <span class="font-mono text-blue-300">{template.key}</span>
      </h2>
      <button
        class="rounded-md p-1 text-gray-500 transition hover:bg-gray-800 hover:text-gray-200"
        on:click={() => dispatch('close')}
        aria-label="Close"
        data-testid="template-dialog-close"
      >
        <X class="h-4 w-4" />
      </button>
    </div>

    <form class="space-y-4 px-5 py-4" on:submit|preventDefault={submit}>
      <div class="grid grid-cols-2 gap-3">
        <label class="col-span-2 block text-[11px] uppercase tracking-[0.05em] text-gray-500">
          Display name
          <input
            class="mt-1 w-full rounded-lg border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-100 focus:border-blue-500 focus:outline-none"
            bind:value={name}
            data-testid="template-dialog-name"
          />
        </label>
        <label class="block text-[11px] uppercase tracking-[0.05em] text-gray-500">
          CPU cores
          <input
            type="number"
            min="1"
            class="mt-1 w-full rounded-lg border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-100 focus:border-blue-500 focus:outline-none"
            bind:value={cpu}
            data-testid="template-dialog-cpu"
          />
        </label>
        <label class="block text-[11px] uppercase tracking-[0.05em] text-gray-500">
          RAM (MB)
          <input
            type="number"
            min="1"
            class="mt-1 w-full rounded-lg border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-100 focus:border-blue-500 focus:outline-none"
            bind:value={ram}
            data-testid="template-dialog-ram"
          />
        </label>
        <label class="block text-[11px] uppercase tracking-[0.05em] text-gray-500">
          Interfaces
          <input
            type="number"
            min="1"
            class="mt-1 w-full rounded-lg border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-100 focus:border-blue-500 focus:outline-none"
            bind:value={ethernet}
            data-testid="template-dialog-ethernet"
          />
        </label>
        <label class="block text-[11px] uppercase tracking-[0.05em] text-gray-500">
          Console
          <input
            class="mt-1 w-full rounded-lg border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-100 focus:border-blue-500 focus:outline-none"
            bind:value={consoleType}
            placeholder="telnet"
          />
        </label>
        <label class="col-span-2 block text-[11px] uppercase tracking-[0.05em] text-gray-500">
          Image aliases (comma-separated)
          <input
            class="mt-1 w-full rounded-lg border border-gray-700 bg-gray-950 px-3 py-2 text-sm font-mono text-gray-100 focus:border-blue-500 focus:outline-none"
            bind:value={aliases}
            placeholder="veos, arista"
            data-testid="template-dialog-aliases"
          />
          <span class="mt-1 block text-[10px] normal-case text-gray-600">
            An on-box image matches this template when its name contains one of these
            aliases (or the display name).
          </span>
        </label>
      </div>

      <div class="flex justify-end gap-2 border-t border-gray-800 pt-3">
        <button
          type="button"
          class="rounded-lg border border-gray-700 px-4 py-2 text-[12px] text-gray-300 transition hover:bg-gray-800"
          on:click={() => dispatch('close')}
        >Cancel</button>
        <button
          type="submit"
          class="rounded-lg bg-blue-600 px-4 py-2 text-[12px] font-medium text-white transition hover:bg-blue-500"
          data-testid="template-dialog-save"
        >Save template</button>
      </div>
    </form>
  </div>
</div>
