<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  import { createEventDispatcher, onDestroy, onMount, tick } from 'svelte';
  import type { NetworkType } from '$lib/types';

  export let open = false;
  export let defaultName = '';
  export let defaultType: NetworkType = 'linux_bridge';

  // Add new entries as more network types are productized; the wire value
  // must be a member of ``NetworkType`` so the backend POST accepts it.
  const TYPE_OPTIONS: { value: NetworkType; label: string }[] = [
    { value: 'linux_bridge', label: 'Bridge (linux_bridge)' }
  ];

  const dispatch = createEventDispatcher<{
    confirm: { name: string; type: NetworkType };
    cancel: void;
  }>();

  let name = defaultName;
  let type: NetworkType = defaultType;
  let modalEl: HTMLDivElement | null = null;
  let nameInputEl: HTMLInputElement | null = null;
  let wasOpen = false;

  // Single reactive block so the open-edge detection and the prev-state
  // update share one Svelte invalidation pass — the two-block form let
  // the trailing ``wasOpen = open`` race ahead of the edge check on
  // simultaneous prop transitions.
  $: {
    if (open && !wasOpen) {
      name = defaultName;
      type = defaultType;
      void focusName();
    }
    wasOpen = open;
  }

  $: trimmedName = name.trim();
  $: canSubmit = trimmedName.length > 0;

  async function focusName() {
    await tick();
    nameInputEl?.focus();
    nameInputEl?.select();
  }

  function cancel() {
    dispatch('cancel');
  }

  function confirm() {
    if (!canSubmit) return;
    dispatch('confirm', { name: trimmedName, type });
  }

  function onKey(event: KeyboardEvent) {
    if (!open) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      cancel();
    } else if (event.key === 'Enter') {
      event.preventDefault();
      confirm();
    }
  }

  function onBackdropMouseDown(event: MouseEvent) {
    if (!modalEl) return;
    const target = event.target as Node | null;
    if (target && modalEl.contains(target)) return;
    cancel();
  }

  onMount(() => {
    if (typeof window !== 'undefined') {
      window.addEventListener('keydown', onKey);
    }
  });

  onDestroy(() => {
    if (typeof window !== 'undefined') {
      window.removeEventListener('keydown', onKey);
    }
  });
</script>

{#if open}
  <div
    class="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/70 p-4 backdrop-blur-sm"
    role="presentation"
    on:mousedown={onBackdropMouseDown}
  >
    <div
      bind:this={modalEl}
      role="dialog"
      tabindex="-1"
      aria-modal="true"
      aria-label="Configure new network"
      class="flex w-full max-w-md flex-col overflow-hidden rounded-[1.4rem] border border-slate-700 bg-slate-950/95 shadow-2xl shadow-black/40"
      data-testid="network-config-modal"
    >
      <div class="flex items-center justify-between border-b border-slate-800 px-5 py-4">
        <div>
          <div class="text-[10px] uppercase tracking-[0.05em] text-slate-500">New network</div>
          <div class="mt-1 text-sm font-semibold text-slate-100">Configure bridge network</div>
        </div>
        <button
          type="button"
          class="inline-flex h-8 w-8 items-center justify-center rounded-full border border-slate-800 bg-slate-900 text-slate-300 transition hover:border-blue-500/40 hover:text-white"
          aria-label="Cancel new network"
          on:click={cancel}
        >
          ×
        </button>
      </div>

      <div class="space-y-4 px-5 py-4 text-sm text-slate-200">
        <label class="block space-y-1.5">
          <span class="text-[10px] uppercase tracking-[0.05em] text-slate-500">Name</span>
          <input
            bind:this={nameInputEl}
            bind:value={name}
            type="text"
            class="w-full rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 outline-none transition focus:border-blue-500/40 focus:ring-1 focus:ring-blue-500/30"
            placeholder="net-1"
            data-testid="network-name-input"
          />
        </label>

        <label class="block space-y-1.5">
          <span class="text-[10px] uppercase tracking-[0.05em] text-slate-500">Type</span>
          <select
            bind:value={type}
            class="w-full rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 outline-none transition focus:border-blue-500/40 focus:ring-1 focus:ring-blue-500/30"
            data-testid="network-type-select"
          >
            {#each TYPE_OPTIONS as option}
              <option value={option.value}>{option.label}</option>
            {/each}
          </select>
        </label>
      </div>

      <div class="flex justify-end gap-3 border-t border-slate-800 bg-slate-950/80 px-5 py-4">
        <button
          type="button"
          class="rounded-full border border-slate-800 px-4 py-2 text-sm text-slate-300 transition hover:border-blue-500/40 hover:text-white"
          on:click={cancel}
        >
          Cancel
        </button>
        <button
          type="button"
          class="rounded-full border border-blue-500/40 bg-blue-500/15 px-4 py-2 text-sm text-blue-100 transition hover:bg-blue-500/25 disabled:cursor-not-allowed disabled:opacity-50"
          on:click={confirm}
          disabled={!canSubmit}
          data-testid="network-confirm-button"
        >
          Create
        </button>
      </div>
    </div>
  </div>
{/if}
