<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<!--
  App shell (#260): compact top bar + left navigation shared by every
  non-editor view. The lab editor stays chrome-less and fullscreen.
-->
<script lang="ts">
  import { goto } from '$app/navigation';
  import { LogOut, Waypoints } from 'lucide-svelte';
  import { authStore, logout } from '$lib/stores/auth';
  import { NAV_SECTIONS, isNavActive } from './nav';

  /** Current route pathname (drives the active-nav state). */
  export let pathname = '/';

  $: sections = NAV_SECTIONS.filter(
    (section) => !section.adminOnly || $authStore.user?.role === 'admin'
  );
  $: userLabel = $authStore.user?.name || $authStore.user?.username || '';

  async function handleLogout() {
    await logout();
    goto('/login');
  }
</script>

<div class="flex h-full min-h-0 flex-1 bg-gray-900 text-gray-200">
  <!-- Left navigation (#260) -->
  <nav
    class="flex w-52 shrink-0 flex-col border-r border-gray-800 bg-gray-900/95"
    aria-label="Primary"
    data-testid="app-nav"
  >
    {#each sections as section (section.title ?? 'root')}
      <div class={section.title ? 'mt-5 px-4 pb-1' : 'mt-2 px-2'}>
        {#if section.title}
          <div class="px-2 text-[10px] uppercase tracking-[0.08em] text-gray-600">
            {section.title}
          </div>
        {/if}
        <ul class="mt-1 space-y-0.5">
          {#each section.items as item (item.href)}
            {@const active = isNavActive(pathname, item.href)}
            <li>
              <a
                href={item.href}
                class={`group flex items-center gap-2.5 rounded-lg px-3 py-2 text-[13px] transition ${
                  active
                    ? 'bg-blue-500/15 text-blue-100'
                    : 'text-gray-400 hover:bg-gray-800/70 hover:text-gray-100'
                }`}
                aria-current={active ? 'page' : undefined}
                data-testid={`nav-${item.label.toLowerCase().replace(/[^a-z]+/g, '-')}`}
              >
                <svelte:component this={item.icon} class={`h-4 w-4 shrink-0 ${active ? 'text-blue-300' : ''}`} />
                <span class="flex-1 truncate">{item.label}</span>
                {#if item.plannedIssue}
                  <span
                    class="rounded-full border border-gray-700 px-1.5 py-px text-[9px] uppercase tracking-[0.05em] text-gray-500"
                    title={`Planned — tracked in #${item.plannedIssue}`}
                  >planned</span>
                {/if}
              </a>
            </li>
          {/each}
        </ul>
      </div>
    {/each}
  </nav>

  <div class="flex min-w-0 flex-1 flex-col">
    <!-- Compact top bar (#260) -->
    <header
      class="flex h-12 shrink-0 items-center justify-between border-b border-gray-800 bg-gray-900/95 px-4"
      data-testid="app-topbar"
    >
      <div class="flex items-center gap-2.5">
        <div class="flex h-6 w-6 items-center justify-center rounded-md bg-blue-500/15 text-blue-300">
          <Waypoints class="h-3.5 w-3.5" />
        </div>
        <span class="text-sm font-semibold tracking-tight text-gray-100">nova-ve</span>
        <span class="hidden rounded-full border border-gray-700 px-2 py-px text-[9px] uppercase tracking-[0.08em] text-gray-500 sm:inline">
          canvas lab platform
        </span>
      </div>
      <div class="flex items-center gap-3">
        {#if $authStore.user?.role}
          <span class="rounded-full border border-gray-700 px-2 py-px text-[9px] uppercase tracking-[0.08em] text-gray-400">
            {$authStore.user.role}
          </span>
        {/if}
        <span class="text-[13px] text-gray-400">{userLabel}</span>
        <button
          class="inline-flex items-center gap-1.5 rounded-lg px-2 py-1.5 text-[12px] text-gray-400 transition hover:bg-gray-800/70 hover:text-red-300"
          on:click={handleLogout}
          data-testid="app-logout"
        >
          <LogOut class="h-3.5 w-3.5" />
          Sign out
        </button>
      </div>
    </header>

    <main class="min-h-0 flex-1 overflow-y-auto">
      <slot />
    </main>
  </div>
</div>
