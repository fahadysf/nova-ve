<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<!--
  User management (#261). Static preview table — roles, quotas, tenant ids,
  lifecycle dates. Wired CRUD arrives with the backend endpoints.
-->
<script lang="ts">
  import PageHeader from '$lib/components/shell/PageHeader.svelte';

  const USERS = [
    {
      username: 'admin',
      online: true,
      auth: 'internal',
      email: 'admin@nova-ve.local',
      name: 'Administrator',
      role: 'admin',
      tenant: 0,
      ramGb: -1,
      cpu: -1,
      expires: '—',
      started: '2026-04-02',
    },
    {
      username: 'uicheck',
      online: true,
      auth: 'internal',
      email: 'uicheck@nova-ve.local',
      name: 'UI Check',
      role: 'user',
      tenant: 1,
      ramGb: 8,
      cpu: 4,
      expires: '—',
      started: '2026-11-19',
    },
    {
      username: 'nettrain',
      online: false,
      auth: 'internal',
      email: 'trainee@example.com',
      name: 'Network Trainee',
      role: 'editor',
      tenant: 2,
      ramGb: 16,
      cpu: 8,
      expires: '2026-12-31',
      started: '2026-11-01',
    },
  ];

  const roleChip: Record<string, string> = {
    admin: 'border-blue-500/40 text-blue-300',
    editor: 'border-violet-500/40 text-violet-300',
    user: 'border-gray-600 text-gray-400',
  };
</script>

<div class="p-6">
  <div class="space-y-6">
    <PageHeader issue={261}>
      <span slot="kicker" class="text-[10px] uppercase tracking-[0.05em] text-gray-500">
        Management · Preview data
      </span>
      <span slot="title">Users</span>
      <span slot="actions">
        <button
          class="rounded-md border border-blue-500 px-3 py-2 text-[10px] uppercase tracking-[0.05em] text-blue-200 transition hover:bg-blue-500/10"
          title="Planned — create a user account"
          disabled
        >Add user</button>
      </span>
    </PageHeader>

    <section class="overflow-hidden rounded-2xl border border-gray-800 bg-gray-900/95" data-testid="users-table">
      <table class="w-full text-left text-sm">
        <thead>
          <tr class="border-b border-gray-800 text-[10px] uppercase tracking-[0.05em] text-gray-500">
            <th class="px-5 py-2.5">User</th>
            <th class="px-3 py-2.5">Status</th>
            <th class="px-3 py-2.5">Auth</th>
            <th class="px-3 py-2.5">Role</th>
            <th class="px-3 py-2.5">Tenant</th>
            <th class="px-3 py-2.5">Quota (RAM / vCPU)</th>
            <th class="px-3 py-2.5">Starts</th>
            <th class="px-3 py-2.5">Expires</th>
            <th class="px-5 py-2.5 text-right">Actions</th>
          </tr>
        </thead>
        <tbody>
          {#each USERS as user (user.username)}
            <tr class="border-b border-gray-800/60 last:border-0 hover:bg-gray-800/40">
              <td class="px-5 py-3">
                <div class="font-medium text-gray-100">{user.username}</div>
                <div class="text-[11px] text-gray-500">{user.name} · {user.email}</div>
              </td>
              <td class="px-3 py-3">
                <span class="inline-flex items-center gap-1.5 text-[12px]">
                  <span class={`h-1.5 w-1.5 rounded-full ${user.online ? 'bg-emerald-400' : 'bg-gray-600'}`}></span>
                  <span class={user.online ? 'text-gray-300' : 'text-gray-500'}>{user.online ? 'online' : 'offline'}</span>
                </span>
              </td>
              <td class="px-3 py-3 text-[12px] text-gray-400">{user.auth}</td>
              <td class="px-3 py-3">
                <span class={`rounded-full border px-1.5 py-px text-[9px] uppercase tracking-[0.05em] ${roleChip[user.role]}`}>{user.role}</span>
              </td>
              <td class="px-3 py-3 font-mono text-[12px] text-gray-400">{user.tenant}</td>
              <td class="px-3 py-3 text-[12px] text-gray-300">
                {user.ramGb < 0 ? 'unlimited' : `${user.ramGb} GB`} / {user.cpu < 0 ? 'unlimited' : user.cpu}
              </td>
              <td class="px-3 py-3 text-[12px] text-gray-400">{user.started}</td>
              <td class="px-3 py-3 text-[12px] text-gray-400">{user.expires}</td>
              <td class="px-5 py-3 text-right">
                <button class="rounded-md border border-gray-700 px-2.5 py-1.5 text-[10px] uppercase tracking-[0.05em] text-gray-300 transition hover:border-blue-500 hover:text-white" title="Planned" disabled>Edit</button>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </section>
  </div>
</div>
