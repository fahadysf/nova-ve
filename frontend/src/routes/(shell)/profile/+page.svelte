<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<!--
  Self-service profile (#261): every signed-in user may edit their own
  email, display name, password, and console preference. Role/quotas/
  tenancy are admin-managed (the backend PATCH guard rejects them here).
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import PageHeader from '$lib/components/shell/PageHeader.svelte';
  import { apiRequest } from '$lib/api';
  import { authStore } from '$lib/stores/session';
  import { toastStore } from '$lib/stores/toasts';

  let email = '';
  let name = '';
  let password = '';
  let html5 = true;
  let saving = false;
  let loaded = false;

  onMount(async () => {
    const username = $authStore.user?.username;
    if (!username) return;
    try {
      const data = await apiRequest<typeof $authStore.user>(`/users/${encodeURIComponent(username)}`);
      const me = data.data;
      email = me?.email ?? '';
      name = me?.name ?? '';
      html5 = me?.html5 !== false;
      loaded = true;
    } catch {
      /* toasted */
    }
  });

  async function save() {
    const username = $authStore.user?.username;
    if (!username) return;
    saving = true;
    try {
      const payload: Record<string, unknown> = { email: email.trim(), name: name.trim(), html5 };
      if (password) payload.password = password;
      await apiRequest(`/users/${encodeURIComponent(username)}`, {
        method: 'PATCH',
        body: payload
      });
      authStore.setUser({
        ...$authStore.user!,
        email: email.trim(),
        name: name.trim(),
        html5
      });
      password = '';
      toastStore.push('Profile updated.', 'success');
    } catch {
      /* toasted */
    } finally {
      saving = false;
    }
  }
</script>

<div class="p-6">
  <div class="space-y-6">
    <PageHeader issue={261}>
      <span slot="kicker" class="text-[10px] uppercase tracking-[0.05em] text-gray-500">
        Account
      </span>
      <span slot="title">Profile</span>
    </PageHeader>

    {#if loaded}
      <div class="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <form class="rounded-2xl border border-gray-800 bg-gray-900/95 p-5" on:submit|preventDefault={save}>
          <div class="text-[10px] uppercase tracking-[0.05em] text-gray-500">Editable</div>
          <div class="mt-3 space-y-3">
            <label class="block text-[11px] uppercase tracking-[0.05em] text-gray-500">
              Full name
              <input
                class="mt-1 w-full rounded-lg border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-100 focus:border-blue-500 focus:outline-none"
                bind:value={name}
                data-testid="profile-name"
              />
            </label>
            <label class="block text-[11px] uppercase tracking-[0.05em] text-gray-500">
              Email
              <input
                type="email"
                class="mt-1 w-full rounded-lg border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-100 focus:border-blue-500 focus:outline-none"
                bind:value={email}
                data-testid="profile-email"
              />
            </label>
            <label class="block text-[11px] uppercase tracking-[0.05em] text-gray-500">
              New password (blank = unchanged)
              <input
                type="password"
                class="mt-1 w-full rounded-lg border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-100 focus:border-blue-500 focus:outline-none"
                bind:value={password}
                data-testid="profile-password"
              />
            </label>
            <label class="flex items-center gap-2 text-[12px] text-gray-400">
              <input type="checkbox" bind:checked={html5} class="h-4 w-4 rounded border-gray-700 bg-gray-950" />
              HTML5 console
            </label>
          </div>
          <div class="mt-4 flex justify-end">
            <button
              type="submit"
              disabled={saving}
              class="rounded-lg bg-blue-600 px-4 py-2 text-[12px] font-medium text-white transition hover:bg-blue-500 disabled:opacity-50"
              data-testid="profile-save"
            >
              {saving ? 'Saving…' : 'Save profile'}
            </button>
          </div>
        </form>

        <div class="rounded-2xl border border-gray-800 bg-gray-900/95 p-5" data-testid="profile-info">
          <div class="text-[10px] uppercase tracking-[0.05em] text-gray-500">Managed by administrators</div>
          <dl class="mt-3 space-y-2 text-sm">
            <div class="flex items-center justify-between">
              <dt class="text-gray-500">Username</dt>
              <dd class="font-mono text-[12px] text-gray-300">{$authStore.user?.username}</dd>
            </div>
            <div class="flex items-center justify-between">
              <dt class="text-gray-500">Role</dt>
              <dd class="font-mono text-[12px] text-gray-300">{$authStore.user?.role}</dd>
            </div>
            <div class="flex items-center justify-between">
              <dt class="text-gray-500">Tenant (pod)</dt>
              <dd class="font-mono text-[12px] text-gray-300">{$authStore.user?.pod ?? 0}</dd>
            </div>
            <div class="flex items-center justify-between">
              <dt class="text-gray-500">Lab root</dt>
              <dd class="font-mono text-[12px] text-gray-300">{$authStore.user?.folder || '/'}</dd>
            </div>
            <div class="flex items-center justify-between">
              <dt class="text-gray-500">CPU quota</dt>
              <dd class="font-mono text-[12px] text-gray-300">
                {($authStore.user?.cpu ?? -1) < 0 ? 'unlimited' : $authStore.user?.cpu}
              </dd>
            </div>
            <div class="flex items-center justify-between">
              <dt class="text-gray-500">RAM quota</dt>
              <dd class="font-mono text-[12px] text-gray-300">
                {($authStore.user?.ram ?? -1) < 0 ? 'unlimited' : `${$authStore.user?.ram} MB`}
              </dd>
            </div>
          </dl>
          <p class="mt-4 text-[11px] text-gray-600">
            Quota and tenancy changes are applied by an administrator from the Users view.
          </p>
        </div>
      </div>
    {:else}
      <div class="rounded-2xl border border-gray-800 bg-gray-900/95 px-5 py-10 text-center text-sm text-gray-500">
        Loading profile…
      </div>
    {/if}
  </div>
</div>
