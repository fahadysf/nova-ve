<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  import { createEventDispatcher, onMount } from 'svelte';
  import type { LibraryDiskType, LibraryDiskUpload } from '$lib/types';

  export let initialType: LibraryDiskType;
  export let busy = false;
  export let error = '';

  const dispatch = createEventDispatcher<{ upload: LibraryDiskUpload; close: void }>();
  const control = 'mt-1 block w-full rounded-lg border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-100 focus:border-blue-500 focus:outline-none disabled:opacity-60';
  let type: LibraryDiskType = initialType;
  let imageName = '';
  let file: File | undefined;
  let dialog: HTMLDivElement;

  onMount(() => {
    const opener = document.activeElement;
    dialog.focus();
    return () => {
      if (opener instanceof HTMLElement && opener.isConnected) opener.focus();
    };
  });

  function handleKey(event: KeyboardEvent) {
    if (event.key === 'Escape' && !busy) {
      event.preventDefault();
      dispatch('close');
    }
    if (event.key !== 'Tab') return;
    const controls = dialog.querySelectorAll<HTMLElement>('button:enabled, input:enabled, select:enabled');
    const first = controls[0];
    const last = controls[controls.length - 1];
    const active = document.activeElement;
    if (!first || active === dialog || (event.shiftKey ? active === first : active === last)) {
      event.preventDefault();
      (event.shiftKey ? last : first)?.focus();
    }
  }

  function submit() {
    if (busy || !file?.size) return;
    dialog.focus();
    dispatch('upload', { type, imageName: imageName.trim(), file });
  }
</script>

<div class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-3 sm:p-6" role="presentation">
  <div
    class="flex max-h-full w-full max-w-lg flex-col rounded-2xl border border-gray-800 bg-gray-900 shadow-2xl"
    role="dialog"
    aria-modal="true"
    aria-busy={busy}
    aria-labelledby="disk-upload-title"
    tabindex="-1"
    bind:this={dialog}
    on:keydown={handleKey}
  >
    <h2 id="disk-upload-title" class="border-b border-gray-800 px-5 py-4 text-sm font-semibold text-gray-100">
      Upload disk image
    </h2>
    <form class="flex min-h-0 flex-col" on:submit|preventDefault={submit}>
      <div class="space-y-3 overflow-y-auto px-5 py-4 text-xs text-gray-300">
        <div class="grid gap-3 sm:grid-cols-2">
          <label>
            Image type
            <select class={control} bind:value={type} disabled={busy}>
              <option value="qemu">QEMU</option>
              <option value="iol">IOL</option>
              <option value="dynamips">Dynamips</option>
            </select>
          </label>
          <label>
            Image name
            <input
              class={control}
              bind:value={imageName}
              placeholder="Uses the disk filename"
              aria-describedby="disk-upload-name-help"
              spellcheck="false"
              disabled={busy}
            />
          </label>
        </div>
        <p id="disk-upload-name-help" class="text-gray-400">
          Leave the name blank to use the filename without its extension. Review lab and template references before adding a file to an existing image.
        </p>
        <label class="block">
          Disk file
          <input
            class={control}
            type="file"
            disabled={busy}
            on:change={(event) => (file = event.currentTarget.files?.[0])}
          />
        </label>
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
          class="rounded-lg bg-blue-600 px-4 py-2 text-xs font-medium text-white hover:bg-blue-500 disabled:opacity-60"
          disabled={busy || !file?.size}
        >{busy ? 'Uploading…' : 'Upload disk image'}</button>
      </div>
    </form>
  </div>
</div>
