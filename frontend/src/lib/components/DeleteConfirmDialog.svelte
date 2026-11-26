<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<!-- Shared destructive-confirm popup for node/lab deletes (#338). -->
<script lang="ts">
  import { createEventDispatcher, onMount } from 'svelte';

  export let title: string;
  export let message: string;
  export let confirmLabel = 'Delete';
  export let busy = false;
  export let testId = 'delete-confirm';

  const dispatch = createEventDispatcher<{ confirm: void; cancel: void }>();

  let cancelButton: HTMLButtonElement | null = null;

  onMount(() => {
    cancelButton?.focus();
  });

  function cancel() {
    if (busy) return;
    dispatch('cancel');
  }

  function handleKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape') {
      event.preventDefault();
      cancel();
    }
  }
</script>

<div
  class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
  role="presentation"
  on:click|self={cancel}
  on:keydown={handleKeydown}
>
  <div
    class="w-full max-w-md rounded-2xl border border-gray-700 bg-gray-900 p-5 shadow-2xl"
    role="dialog"
    aria-modal="true"
    aria-labelledby="{testId}-title"
    aria-describedby="{testId}-message"
    data-testid="{testId}-dialog"
  >
    <h2 id="{testId}-title" class="text-lg font-semibold text-gray-100">{title}</h2>
    <p id="{testId}-message" class="mt-2 text-sm text-gray-300">{message}</p>
    <div class="mt-5 flex justify-end gap-2">
      <button
        type="button"
        bind:this={cancelButton}
        class="rounded-md border border-gray-700 px-3 py-2 text-[10px] uppercase tracking-[0.05em] text-gray-300 transition hover:border-blue-500 hover:text-white disabled:opacity-40"
        disabled={busy}
        on:click={cancel}
        data-testid="{testId}-cancel">Cancel</button
      >
      <button
        type="button"
        class="rounded-md border border-red-500/60 px-3 py-2 text-[10px] uppercase tracking-[0.05em] text-red-200 transition hover:bg-red-500/10 disabled:opacity-40"
        disabled={busy}
        on:click={() => dispatch('confirm')}
        data-testid="{testId}-confirm">{confirmLabel}</button
      >
    </div>
  </div>
</div>
