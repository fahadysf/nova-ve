<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<!--
  User management (#261): admin view over every account — role, quotas,
  tenant (pod) assignment, lifecycle dates, last sign-in — with
  create/edit/delete. Self-service profile editing lives at /profile.
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import { Plus, RefreshCw } from 'lucide-svelte';
  import PageHeader from '$lib/components/shell/PageHeader.svelte';
  import UserDialog from '$lib/components/manage/UserDialog.svelte';
  import { apiRequest } from '$lib/api';
  import { authStore } from '$lib/stores/session';
  import { toastStore } from '$lib/stores/toasts';
  import type { UserRead, UserWritePayload } from '$lib/types';

  let users: UserRead[] = [];
  let loading = false;

  let dialogOpen = false;
  let dialogMode: 'create' | 'edit' = 'create';
  let selected: UserRead | null = null;
  let confirmTarget: UserRead | null = null;

  const roleChip: Record<string, string> = {
    admin: 'border-blue-500/40 text-blue-300',
    editor: 'border-violet-500/40 text-violet-300',
    user: 'border-gray-600 text-gray-400'
  };

  function fmtDate(epochSeconds: number | undefined | null): string {
    if (!epochSeconds || epochSeconds < 0) return '—';
    return new Date(epochSeconds * 1000).toISOString().slice(0, 10);
  }

  /** #297: when an account last signed in, in the admin's local time
   *  (unlike the lifecycle dates above, a sign-in time is read against
   *  "now", so UTC would mislead). Accounts that never have show
   *  "never" — not a blank that could be mistaken for a failed load. */
  function fmtLastLogin(epochSeconds: number | undefined | null): string {
    if (!epochSeconds || epochSeconds <= 0) return 'never';
    const date = new Date(epochSeconds * 1000);
    const pad = (value: number) => String(value).padStart(2, '0');
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }

  function fmtQuota(value: number | undefined): string {
    if (value === undefined || value < 0) return 'unlimited';
    return String(value);
  }

  function isOnline(user: UserRead): boolean {
    return !!user.online && user.online !== 0;
  }

  async function refresh() {
    loading = true;
    try {
      const data = await apiRequest<Record<string, UserRead>>('/users/');
      users = Object.values(data.data ?? {}).sort((a, b) =>
        a.username.localeCompare(b.username)
      );
    } finally {
      loading = false;
    }
  }

  function openCreate() {
    dialogMode = 'create';
    selected = null;
    dialogOpen = true;
  }

  function openEdit(user: UserRead) {
    dialogMode = 'edit';
    selected = user;
    dialogOpen = true;
  }

  async function handleSave(event: CustomEvent<{ username: string; payload: UserWritePayload }>) {
    const { username, payload } = event.detail;
    try {
      if (dialogMode === 'create') {
        await apiRequest('/users/', {
          method: 'POST',
          body: { username, password: payload.password ?? '', ...payload }
        });
        toastStore.push(`User ${username} created.`, 'success');
      } else {
        await apiRequest(`/users/${encodeURIComponent(username)}`, {
          method: 'PATCH',
          body: payload
        });
        toastStore.push(`User ${username} updated.`, 'success');
      }
      dialogOpen = false;
      await refresh();
    } catch {
      /* apiRequest already toasted the failure */
    }
  }

  async function handleDelete() {
    if (!confirmTarget) return;
    const username = confirmTarget.username;
    confirmTarget = null;
    try {
      await apiRequest(`/users/${encodeURIComponent(username)}`, { method: 'DELETE' });
      toastStore.push(`User ${username} deleted.`, 'success');
      await refresh();
    } catch {
      /* toasted */
    }
  }

  onMount(() => {
    void refresh();
  });
</script>

<div class="p-6">
  <div class="space-y-6">
    <PageHeader issue={261}>
      <span slot="kicker" class="text-[10px] uppercase tracking-[0.05em] text-gray-500">
        Management
      </span>
      <span slot="title">Users</span>
      <span slot="actions">
        <button
          class="inline-flex items-center gap-1.5 rounded-md border border-gray-700 px-3 py-2 text-[10px] uppercase tracking-[0.05em] text-gray-300 transition hover:border-blue-500 hover:text-white"
          on:click={() => void refresh()}
          data-testid="users-refresh"
        >
          <RefreshCw class="h-3 w-3" />
          Refresh
        </button>
        <button
          class="inline-flex items-center gap-1.5 rounded-md border border-blue-500 px-3 py-2 text-[10px] uppercase tracking-[0.05em] text-blue-200 transition hover:bg-blue-500/10"
          on:click={openCreate}
          data-testid="users-add"
        >
          <Plus class="h-3 w-3" />
          Add user
        </button>
      </span>
    </PageHeader>

    {#if loading && !users.length}
      <div class="rounded-2xl border border-gray-800 bg-gray-900/95 px-5 py-10 text-center text-sm text-gray-500">
        Loading users…
      </div>
    {:else}
      <section class="overflow-hidden rounded-2xl border border-gray-800 bg-gray-900/95" data-testid="users-table">
        <table class="w-full text-left text-sm">
          <thead>
            <tr class="border-b border-gray-800 text-[10px] uppercase tracking-[0.05em] text-gray-500">
              <th class="px-5 py-2.5">User</th>
              <th class="px-3 py-2.5">Status</th>
              <th class="px-3 py-2.5">Last login</th>
              <th class="px-3 py-2.5">Auth</th>
              <th class="px-3 py-2.5">Role</th>
              <th class="px-3 py-2.5">Tenant</th>
              <th class="px-3 py-2.5">Quota (RAM / vCPU)</th>
              <th class="px-3 py-2.5">Lab root</th>
              <th class="px-3 py-2.5">Starts</th>
              <th class="px-3 py-2.5">Expires</th>
              <th class="px-5 py-2.5 text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {#each users as user (user.username)}
              <tr class="border-b border-gray-800/60 last:border-0 hover:bg-gray-800/40" data-testid={`users-row-${user.username}`}>
                <td class="px-5 py-3">
                  <div class="font-medium text-gray-100">{user.username}</div>
                  <div class="text-[11px] text-gray-500">{user.name || '—'} · {user.email || '—'}</div>
                </td>
                <td class="px-3 py-3">
                  <span class="inline-flex items-center gap-1.5 text-[12px]">
                    <span class={`h-1.5 w-1.5 rounded-full ${isOnline(user) ? 'bg-emerald-400' : 'bg-gray-600'}`}></span>
                    <span class={isOnline(user) ? 'text-gray-300' : 'text-gray-500'}>{isOnline(user) ? 'online' : 'offline'}</span>
                  </span>
                </td>
                <td
                  class="px-3 py-3 text-[12px] text-gray-400"
                  data-testid={`users-last-login-${user.username}`}
                >
                  {#if user.last_login}
                    <div class="font-mono text-gray-300">{fmtLastLogin(user.last_login)}</div>
                    {#if user.ip}
                      <div class="text-[11px] text-gray-600">from {user.ip}</div>
                    {/if}
                  {:else}
                    <span class="text-gray-600">never</span>
                  {/if}
                </td>
                <td class="px-3 py-3 text-[12px] text-gray-400">{user.extauth || 'internal'}</td>
                <td class="px-3 py-3">
                  <span class={`rounded-full border px-1.5 py-px text-[9px] uppercase tracking-[0.05em] ${roleChip[user.role] || roleChip.user}`}>{user.role}</span>
                </td>
                <td class="px-3 py-3 font-mono text-[12px] text-gray-400">{user.pod ?? 0}</td>
                <td class="px-3 py-3 text-[12px] text-gray-300">
                  {user.ram !== undefined && user.ram < 0 ? 'unlimited' : `${user.ram ?? -1} MB`}
                  /
                  {fmtQuota(user.cpu)}
                </td>
                <td class="px-3 py-3 font-mono text-[12px] text-gray-400">{user.folder || '/'}</td>
                <td class="px-3 py-3 text-[12px] text-gray-400">{fmtDate(user.datestart)}</td>
                <td class="px-3 py-3 text-[12px] text-gray-400">{fmtDate(user.expiration)}</td>
                <td class="px-5 py-3 text-right">
                  <span class="inline-flex justify-end gap-1.5">
                    <button
                      class="rounded-md border border-gray-700 px-2.5 py-1.5 text-[10px] uppercase tracking-[0.05em] text-gray-300 transition hover:border-blue-500 hover:text-white"
                      on:click={() => openEdit(user)}
                      data-testid={`users-edit-${user.username}`}
                    >Edit</button>
                    {#if user.username !== $authStore.user?.username}
                      <button
                        class="rounded-md border border-gray-700 px-2.5 py-1.5 text-[10px] uppercase tracking-[0.05em] text-gray-400 transition hover:border-red-500 hover:text-red-300"
                        on:click={() => (confirmTarget = user)}
                        data-testid={`users-delete-${user.username}`}
                      >Delete</button>
                    {/if}
                  </span>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </section>
    {/if}
  </div>
</div>

{#if dialogOpen}
  <UserDialog
    mode={dialogMode}
    user={selected}
    on:save={handleSave}
    on:close={() => (dialogOpen = false)}
  />
{/if}

{#if confirmTarget}
  <div class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" role="presentation" data-testid="user-delete-confirm">
    <div class="w-full max-w-sm rounded-2xl border border-gray-800 bg-gray-900 p-5 shadow-2xl">
      <h2 class="text-sm font-semibold text-gray-100">Delete {confirmTarget.username}?</h2>
      <p class="mt-2 text-[12px] text-gray-400">
        This removes the account permanently. Labs on disk are kept.
      </p>
      <div class="mt-4 flex justify-end gap-2">
        <button
          class="rounded-lg border border-gray-700 px-4 py-2 text-[12px] text-gray-300 transition hover:bg-gray-800"
          on:click={() => (confirmTarget = null)}
          data-testid="user-delete-cancel"
        >Cancel</button>
        <button
          class="rounded-lg bg-red-600 px-4 py-2 text-[12px] font-medium text-white transition hover:bg-red-500"
          on:click={handleDelete}
          data-testid="user-delete-confirm-button"
        >Delete user</button>
      </div>
    </div>
  </div>
{/if}
