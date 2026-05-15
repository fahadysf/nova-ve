<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  import { createEventDispatcher, onDestroy, onMount, tick } from 'svelte';
  import type { NetworkCreateConfig, NetworkType } from '$lib/types';

  export let open = false;
  export let defaultName = '';
  export let defaultType: NetworkType = 'linux_bridge';

  // Add new entries as more network types are productized; the wire value
  // must be a member of ``NetworkType`` so the backend POST accepts it.
  const TYPE_OPTIONS: { value: NetworkType; label: string }[] = [
    { value: 'linux_bridge', label: 'Bridge (linux_bridge)' },
    { value: 'nat_cloud', label: 'NAT-Cloud (nat_cloud)' }
  ];

  const dispatch = createEventDispatcher<{
    confirm: { name: string; type: NetworkType; config?: NetworkCreateConfig };
    cancel: void;
  }>();

  let name = defaultName;
  let type: NetworkType = defaultType;
  let cidr = '';
  let gateway = '';
  let dhcp = true;
  let dhcpStart = '';
  let dhcpEnd = '';
  let egressInterface = '';
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
      cidr = '';
      gateway = '';
      dhcp = true;
      dhcpStart = '';
      dhcpEnd = '';
      egressInterface = '';
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
    const config: NetworkCreateConfig = {};
    if (type === 'nat_cloud') {
      if (cidr.trim()) config.cidr = cidr.trim();
      if (gateway.trim()) config.gateway = gateway.trim();
      config.dhcp = dhcp;
      if (dhcpStart.trim()) config.dhcp_start = dhcpStart.trim();
      if (dhcpEnd.trim()) config.dhcp_end = dhcpEnd.trim();
      if (egressInterface.trim()) config.egress_interface = egressInterface.trim();
    }
    dispatch('confirm', { name: trimmedName, type, config });
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

        {#if type === 'nat_cloud'}
          <div class="grid gap-3 rounded-xl border border-slate-800 bg-slate-900/30 p-3 sm:grid-cols-2">
            <label class="block space-y-1.5 sm:col-span-2">
              <span class="text-[10px] uppercase tracking-[0.05em] text-slate-500">CIDR</span>
              <input
                bind:value={cidr}
                type="text"
                class="w-full rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 outline-none transition focus:border-blue-500/40 focus:ring-1 focus:ring-blue-500/30"
                placeholder="Auto from NAT-Cloud pool"
              />
            </label>
            <label class="block space-y-1.5">
              <span class="text-[10px] uppercase tracking-[0.05em] text-slate-500">Gateway</span>
              <input
                bind:value={gateway}
                type="text"
                class="w-full rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 outline-none transition focus:border-blue-500/40 focus:ring-1 focus:ring-blue-500/30"
                placeholder=".1"
              />
            </label>
            <label class="block space-y-1.5">
              <span class="text-[10px] uppercase tracking-[0.05em] text-slate-500">Egress</span>
              <input
                bind:value={egressInterface}
                type="text"
                class="w-full rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 outline-none transition focus:border-blue-500/40 focus:ring-1 focus:ring-blue-500/30"
                placeholder="Default route"
              />
            </label>
            <label class="flex items-center gap-2 sm:col-span-2">
              <input bind:checked={dhcp} type="checkbox" class="h-4 w-4 rounded border-slate-700 bg-slate-900" />
              <span class="text-xs text-slate-300">Enable DHCP</span>
            </label>
            <label class="block space-y-1.5">
              <span class="text-[10px] uppercase tracking-[0.05em] text-slate-500">DHCP start</span>
              <input
                bind:value={dhcpStart}
                type="text"
                class="w-full rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 outline-none transition focus:border-blue-500/40 focus:ring-1 focus:ring-blue-500/30"
                placeholder=".100"
                disabled={!dhcp}
              />
            </label>
            <label class="block space-y-1.5">
              <span class="text-[10px] uppercase tracking-[0.05em] text-slate-500">DHCP end</span>
              <input
                bind:value={dhcpEnd}
                type="text"
                class="w-full rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 outline-none transition focus:border-blue-500/40 focus:ring-1 focus:ring-blue-500/30"
                placeholder=".254"
                disabled={!dhcp}
              />
            </label>
          </div>
        {/if}
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
