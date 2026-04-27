<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  import { createEventDispatcher, onDestroy, onMount } from 'svelte';
  import { dragLinkStore } from '$lib/stores/dragLink';
  import type { LinkStyle } from '$lib/types';

  const dispatch = createEventDispatcher<{
    confirm: { styleOverride: LinkStyle };
    cancel: void;
  }>();

  $: snapshot = $dragLinkStore;
  $: open = snapshot.state === 'confirming';
  let style: LinkStyle = 'orthogonal';
  let modalEl: HTMLDivElement | null = null;

  function endpointTitle(
    endpoint: typeof snapshot.source | typeof snapshot.target | null | undefined
  ): string {
    if (!endpoint) return '?';
    if (endpoint.kind === 'network') {
      return `Network: ${endpoint.networkName ?? `#${endpoint.networkId}`}`;
    }
    return endpoint.interfaceName ?? `port ${endpoint.interfaceIndex ?? '?'}`;
  }
  $: sourceTitle = endpointTitle(snapshot.source);
  $: targetTitle = endpointTitle(snapshot.target);

  function close() {
    dispatch('cancel');
  }

  function confirm() {
    dispatch('confirm', { styleOverride: style });
  }

  function onKey(event: KeyboardEvent) {
    if (!open) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      close();
    } else if (event.key === 'Enter') {
      event.preventDefault();
      confirm();
    }
  }

  function onBackdropMouseDown(event: MouseEvent) {
    if (!modalEl) return;
    const target = event.target as Node | null;
    if (target && modalEl.contains(target)) return;
    close();
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
      aria-label="Confirm new link"
      class="flex w-full max-w-md flex-col overflow-hidden rounded-[1.4rem] border border-slate-700 bg-slate-950/95 shadow-2xl shadow-black/40"
      data-testid="link-confirm-modal"
    >
      <div class="flex items-center justify-between border-b border-slate-800 px-5 py-4">
        <div>
          <div class="text-[10px] uppercase tracking-[0.05em] text-slate-500">Confirm link</div>
          <div class="mt-1 text-sm font-semibold text-slate-100">
            {sourceTitle}
            <span class="text-slate-400"> → </span>
            {targetTitle}
          </div>
        </div>
        <button
          type="button"
          class="inline-flex h-8 w-8 items-center justify-center rounded-full border border-slate-800 bg-slate-900 text-slate-300 transition hover:border-blue-500/40 hover:text-white"
          aria-label="Cancel new link"
          on:click={close}
        >
          ×
        </button>
      </div>

      <div class="space-y-4 px-5 py-4 text-sm text-slate-200">
        <div class="grid gap-3 md:grid-cols-2">
          <div class="rounded-xl border border-slate-800 bg-slate-900/60 p-3">
            <div class="text-[10px] uppercase tracking-[0.05em] text-slate-500">Source</div>
            {#if snapshot.source?.kind === 'network'}
              <div class="mt-1 text-sm font-medium">
                Network: {snapshot.source.networkName ?? `#${snapshot.source.networkId}`}
              </div>
            {:else if snapshot.source?.kind === 'interface'}
              <div class="mt-1 text-sm font-medium">
                node #{snapshot.source.nodeId} · {snapshot.source.interfaceName ?? `iface ${snapshot.source.interfaceIndex}`}
              </div>
              {#if snapshot.source.plannedMac}
                <div class="mt-1 font-mono text-[11px] text-slate-400">{snapshot.source.plannedMac}</div>
              {/if}
            {/if}
          </div>
          <div class="rounded-xl border border-slate-800 bg-slate-900/60 p-3">
            <div class="text-[10px] uppercase tracking-[0.05em] text-slate-500">Target</div>
            {#if snapshot.target?.kind === 'network'}
              <div class="mt-1 text-sm font-medium">
                Network: {snapshot.target.networkName ?? `#${snapshot.target.networkId}`}
              </div>
            {:else if snapshot.target?.kind === 'interface'}
              <div class="mt-1 text-sm font-medium">
                node #{snapshot.target.nodeId} · {snapshot.target.interfaceName ?? `iface ${snapshot.target.interfaceIndex}`}
              </div>
              {#if snapshot.target.plannedMac}
                <div class="mt-1 font-mono text-[11px] text-slate-400">{snapshot.target.plannedMac}</div>
              {/if}
            {/if}
          </div>
        </div>

        <fieldset class="space-y-2">
          <legend class="text-[10px] uppercase tracking-[0.05em] text-slate-500">Style override</legend>
          <div class="grid grid-cols-3 gap-2">
            {#each ['orthogonal', 'bezier', 'straight'] as option}
              <label
                class={`flex cursor-pointer items-center justify-center gap-1.5 rounded-xl border px-3 py-2 text-[11px] uppercase tracking-[0.05em] transition ${
                  style === option
                    ? 'border-blue-500/60 bg-blue-500/15 text-blue-100'
                    : 'border-slate-800 bg-slate-900/60 text-slate-300 hover:border-slate-600 hover:text-white'
                }`}
              >
                <input
                  type="radio"
                  name="link-style"
                  value={option}
                  bind:group={style}
                  class="sr-only"
                />
                <span>{option}</span>
              </label>
            {/each}
          </div>
        </fieldset>
      </div>

      <div class="flex justify-end gap-3 border-t border-slate-800 bg-slate-950/80 px-5 py-4">
        <button
          type="button"
          class="rounded-full border border-slate-800 px-4 py-2 text-sm text-slate-300 transition hover:border-blue-500/40 hover:text-white"
          on:click={close}
        >
          Cancel
        </button>
        <button
          type="button"
          class="rounded-full border border-blue-500/40 bg-blue-500/15 px-4 py-2 text-sm text-blue-100 transition hover:bg-blue-500/25"
          on:click={confirm}
          data-testid="link-confirm-button"
        >
          Confirm
        </button>
      </div>
    </div>
  </div>
{/if}
