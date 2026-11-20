<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<!-- System settings (#265). Static preview: grouped operational knobs. -->
<script lang="ts">
  import PageHeader from '$lib/components/shell/PageHeader.svelte';

  const SECTIONS = [
    {
      title: 'Authentication',
      fields: ['External auth backend (RADIUS)', 'External auth backend (LDAP)', 'Strict login control'],
    },
    {
      title: 'Consoles',
      fields: ['HTML5 terminal color scheme', 'Font family', 'Font size'],
    },
    {
      title: 'Resources',
      fields: ['Dedicated service CPUs', 'Minimum free disk (GB)', 'Node start concurrency'],
    },
    {
      title: 'Networking',
      fields: ['NAT network CIDR', 'Container network CIDR', 'Bridge MTU'],
    },
  ];
</script>

<div class="p-6">
  <div class="space-y-6">
    <PageHeader issue={265}>
      <span slot="kicker" class="text-[10px] uppercase tracking-[0.05em] text-gray-500">
        System · Preview data
      </span>
      <span slot="title">Settings</span>
      <span slot="actions">
        <button
          class="rounded-md border border-blue-500 px-3 py-2 text-[10px] uppercase tracking-[0.05em] text-blue-200 transition hover:bg-blue-500/10"
          title="Planned — persist settings"
          disabled
        >Apply</button>
      </span>
    </PageHeader>

    <section class="grid grid-cols-1 gap-4 lg:grid-cols-2" data-testid="settings-sections">
      {#each SECTIONS as section (section.title)}
        <div class="rounded-2xl border border-gray-800 bg-gray-900/95 p-5">
          <div class="text-[10px] uppercase tracking-[0.05em] text-gray-500">{section.title}</div>
          <div class="mt-3 space-y-3">
            {#each section.fields as field}
              <div class="flex items-center justify-between gap-3">
                <span class="text-[13px] text-gray-300">{field}</span>
                <input
                  class="w-44 rounded-lg border border-gray-700 bg-gray-950 px-2.5 py-1.5 text-[12px] text-gray-400 outline-none"
                  placeholder="planned"
                  disabled
                />
              </div>
            {/each}
          </div>
        </div>
      {/each}
    </section>
  </div>
</div>
