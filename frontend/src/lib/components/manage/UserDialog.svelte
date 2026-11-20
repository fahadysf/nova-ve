<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<!--
  Create/Edit dialog for the user-management view (#261).
  Create mode requires username + password; edit mode treats a blank
  password as "unchanged". Quotas use -1 = unlimited; lifecycle dates are
  day-precision and stored as UTC epoch seconds.
-->
<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { X } from 'lucide-svelte';
  import type { UserRead, UserWritePayload, UserRole } from '$lib/types';

  const dispatch = createEventDispatcher<{
    save: { username: string; payload: UserWritePayload };
    close: void;
  }>();

  export let mode: 'create' | 'edit' = 'create';
  export let user: UserRead | null = null;

  const ROLES: UserRole[] = ['user', 'editor', 'admin'];

  let username = '';
  let password = '';
  let email = '';
  let name = '';
  let role: UserRole = 'user';
  let pod = 0;
  let cpu = -1;
  let ram = -1;
  let folder = '/';
  let html5 = true;
  let datestart = ''; // yyyy-mm-dd or ''
  let expiration = '';

  function epochFromInput(value: string): number {
    if (!value) return -1;
    return Math.floor(Date.parse(`${value}T00:00:00Z`) / 1000);
  }

  function inputFromEpoch(value: number | undefined | null): string {
    if (!value || value < 0) return '';
    return new Date(value * 1000).toISOString().slice(0, 10);
  }

  $: if (user && mode === 'edit') {
    username = user.username;
    email = user.email ?? '';
    name = user.name ?? '';
    role = user.role;
    pod = user.pod ?? 0;
    cpu = user.cpu ?? -1;
    ram = user.ram ?? -1;
    folder = user.folder ?? '/';
    html5 = user.html5 !== false;
    datestart = inputFromEpoch(user.datestart);
    expiration = inputFromEpoch(user.expiration);
  }

  function submit() {
    const payload: UserWritePayload = {
      email: email.trim(),
      name: name.trim(),
      role,
      html5,
      cpu: Number.isFinite(cpu) ? Math.trunc(cpu) : -1,
      ram: Number.isFinite(ram) ? Math.trunc(ram) : -1,
      pod: Math.trunc(pod) || 0,
      folder: folder.trim() || '/',
      datestart: epochFromInput(datestart),
      expiration: epochFromInput(expiration)
    };
    if (password) {
      payload.password = password;
    }
    dispatch('save', { username: username.trim(), payload });
  }
</script>

<div
  class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
  role="presentation"
  data-testid="user-dialog"
>
  <div class="max-h-full w-full max-w-xl overflow-y-auto rounded-2xl border border-gray-800 bg-gray-900 shadow-2xl">
    <div class="flex items-center justify-between border-b border-gray-800 px-5 py-3">
      <h2 class="text-sm font-semibold text-gray-100">
        {mode === 'create' ? 'Add user' : `Edit ${username}`}
      </h2>
      <button
        class="rounded-md p-1 text-gray-500 transition hover:bg-gray-800 hover:text-gray-200"
        on:click={() => dispatch('close')}
        aria-label="Close"
        data-testid="user-dialog-close"
      >
        <X class="h-4 w-4" />
      </button>
    </div>

    <form class="space-y-4 px-5 py-4" on:submit|preventDefault={submit}>
      <div class="grid grid-cols-2 gap-3">
        <label class="block text-[11px] uppercase tracking-[0.05em] text-gray-500">
          Username
          <input
            class="mt-1 w-full rounded-lg border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-100 focus:border-blue-500 focus:outline-none disabled:opacity-50"
            bind:value={username}
            disabled={mode === 'edit'}
            required
            data-testid="user-dialog-username"
          />
        </label>
        <label class="block text-[11px] uppercase tracking-[0.05em] text-gray-500">
          Password {mode === 'edit' ? '(blank = unchanged)' : ''}
          <input
            type="password"
            class="mt-1 w-full rounded-lg border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-100 focus:border-blue-500 focus:outline-none"
            bind:value={password}
            required={mode === 'create'}
            minlength={6}
            data-testid="user-dialog-password"
          />
        </label>
        <label class="block text-[11px] uppercase tracking-[0.05em] text-gray-500">
          Full name
          <input
            class="mt-1 w-full rounded-lg border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-100 focus:border-blue-500 focus:outline-none"
            bind:value={name}
          />
        </label>
        <label class="block text-[11px] uppercase tracking-[0.05em] text-gray-500">
          Email
          <input
            type="email"
            class="mt-1 w-full rounded-lg border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-100 focus:border-blue-500 focus:outline-none"
            bind:value={email}
          />
        </label>
        <label class="block text-[11px] uppercase tracking-[0.05em] text-gray-500">
          Role
          <select
            class="mt-1 w-full rounded-lg border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-100 focus:border-blue-500 focus:outline-none"
            bind:value={role}
            data-testid="user-dialog-role"
          >
            {#each ROLES as option (option)}
              <option value={option}>{option}</option>
            {/each}
          </select>
        </label>
        <label class="block text-[11px] uppercase tracking-[0.05em] text-gray-500">
          Tenant (pod)
          <input
            type="number"
            class="mt-1 w-full rounded-lg border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-100 focus:border-blue-500 focus:outline-none"
            bind:value={pod}
            data-testid="user-dialog-pod"
          />
        </label>
        <label class="block text-[11px] uppercase tracking-[0.05em] text-gray-500">
          CPU quota (-1 = unlimited)
          <input
            type="number"
            class="mt-1 w-full rounded-lg border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-100 focus:border-blue-500 focus:outline-none"
            bind:value={cpu}
            data-testid="user-dialog-cpu"
          />
        </label>
        <label class="block text-[11px] uppercase tracking-[0.05em] text-gray-500">
          RAM quota MB (-1 = unlimited)
          <input
            type="number"
            class="mt-1 w-full rounded-lg border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-100 focus:border-blue-500 focus:outline-none"
            bind:value={ram}
            data-testid="user-dialog-ram"
          />
        </label>
        <label class="block text-[11px] uppercase tracking-[0.05em] text-gray-500">
          Lab root folder
          <input
            class="mt-1 w-full rounded-lg border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-100 focus:border-blue-500 focus:outline-none"
            bind:value={folder}
            placeholder="/"
          />
        </label>
        <label class="mt-6 flex items-center gap-2 text-[12px] text-gray-400">
          <input type="checkbox" bind:checked={html5} class="h-4 w-4 rounded border-gray-700 bg-gray-950" />
          HTML5 console
        </label>
        <label class="block text-[11px] uppercase tracking-[0.05em] text-gray-500">
          Start date (blank = none)
          <input
            type="date"
            class="mt-1 w-full rounded-lg border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-100 focus:border-blue-500 focus:outline-none"
            bind:value={datestart}
          />
        </label>
        <label class="block text-[11px] uppercase tracking-[0.05em] text-gray-500">
          Expiration date (blank = none)
          <input
            type="date"
            class="mt-1 w-full rounded-lg border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-100 focus:border-blue-500 focus:outline-none"
            bind:value={expiration}
            data-testid="user-dialog-expiration"
          />
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
          data-testid="user-dialog-save"
        >
          {mode === 'create' ? 'Create user' : 'Save changes'}
        </button>
      </div>
    </form>
  </div>
</div>
