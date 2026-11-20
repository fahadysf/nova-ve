<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<!--
  System settings (#265). Effective operational config from
  /api/system/settings; the editable subset (discovery cadence, NAT cloud
  pool, session lifetime) persists overrides that apply on backend restart.
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import PageHeader from '$lib/components/shell/PageHeader.svelte';
  import { apiRequest } from '$lib/api';
  import { toastStore } from '$lib/stores/toasts';

  interface SettingsData {
    auth: {
      session_max_age: number;
      session_max_age_effective: number;
      session_cookie: string;
      cookie_secure: boolean;
      access_token_expire_minutes: number;
    };
    networking: {
      nat_cloud_pool: string;
      nat_cloud_pool_effective: string;
      docker_host: string;
      external_console_host: string;
      external_console_host_effective: string;
    };
    reconciliation: { discovery_cadence_seconds: number; discovery_cadence_seconds_effective: number };
    console: { guacamole_public_path: string; terminal_font_name: string; terminal_font_size: number };
    paths: Record<string, string>;
    overrides: string[];
    restart_required: boolean;
  }

  let settings: SettingsData | null = null;
  let discoveryCadence = 30;
  let natPool = '';
  let sessionMaxAge = 14400;
  let externalConsoleHost = '';
  let saving = false;

  function readOnlyGroups(data: SettingsData): { title: string; rows: [string, string][] }[] {
    return [
      {
        title: 'Auth',
        rows: [
          ['Session cookie', data.auth.session_cookie],
          ['Token lifetime', `${data.auth.access_token_expire_minutes} min`],
          ['Cookie secure', data.auth.cookie_secure ? 'yes' : 'no']
        ]
      },
      {
        title: 'Console',
        rows: [
          ['Gateway path', data.console.guacamole_public_path],
          ['Terminal font', data.console.terminal_font_name],
          ['Terminal size', String(data.console.terminal_font_size)]
        ]
      },
      {
        title: 'Paths',
        rows: Object.entries(data.paths).map(([key, value]) => [key.replace(/_/g, ' '), value] as [string, string])
      }
    ];
  }

  async function load() {
    const data = await apiRequest<SettingsData>('/system/settings');
    settings = data.data;
    discoveryCadence = data.data.reconciliation.discovery_cadence_seconds;
    natPool = data.data.networking.nat_cloud_pool;
    sessionMaxAge = data.data.auth.session_max_age;
    externalConsoleHost = data.data.networking.external_console_host;
  }

  async function save() {
    saving = true;
    try {
      await apiRequest('/system/settings', {
        method: 'PUT',
        body: {
          DISCOVERY_CADENCE_SECONDS: discoveryCadence,
          NAT_CLOUD_POOL: natPool,
          SESSION_MAX_AGE: sessionMaxAge,
          EXTERNAL_CONSOLE_HOST: externalConsoleHost
        }
      });
      toastStore.push('Settings persisted. Restart the backend to apply.', 'success');
      await load();
    } catch {
      /* toasted */
    } finally {
      saving = false;
    }
  }

  onMount(() => {
    void load();
  });
</script>

<div class="p-6">
  <div class="space-y-6">
    <PageHeader issue={265}>
      <span slot="kicker" class="text-[10px] uppercase tracking-[0.05em] text-gray-500">
        System
      </span>
      <span slot="title">Settings</span>
    </PageHeader>

    {#if settings}
      {#if settings.restart_required}
        <div class="rounded-xl border border-amber-500/40 bg-amber-500/10 px-4 py-3 text-[12px] text-amber-200" data-testid="settings-restart-banner">
          Persisted overrides are in effect after the next backend restart — the
          effective column still shows the running values.
        </div>
      {/if}

      <div class="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <form class="rounded-2xl border border-gray-800 bg-gray-900/95 p-5" on:submit|preventDefault={save}>
          <div class="text-[10px] uppercase tracking-[0.05em] text-gray-500">Editable — applies on restart</div>
          <div class="mt-3 space-y-3">
            <label class="block text-[11px] uppercase tracking-[0.05em] text-gray-500">
              Discovery cadence (seconds, 5–300)
              <input
                type="number"
                min="5"
                max="300"
                class="mt-1 w-full rounded-lg border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-100 focus:border-blue-500 focus:outline-none"
                bind:value={discoveryCadence}
                data-testid="settings-discovery"
              />
            </label>
            <label class="block text-[11px] uppercase tracking-[0.05em] text-gray-500">
              NAT cloud pool (CIDR)
              <input
                class="mt-1 w-full rounded-lg border border-gray-700 bg-gray-950 px-3 py-2 font-mono text-sm text-gray-100 focus:border-blue-500 focus:outline-none"
                bind:value={natPool}
                data-testid="settings-nat-pool"
              />
            </label>
            <div>
              <label class="block text-[11px] uppercase tracking-[0.05em] text-gray-500">
                External console host (hostname or IPv4, empty disables)
                <input
                  class="mt-1 w-full rounded-lg border border-gray-700 bg-gray-950 px-3 py-2 font-mono text-sm text-gray-100 focus:border-blue-500 focus:outline-none"
                  bind:value={externalConsoleHost}
                  placeholder="192.168.7.9"
                  data-testid="settings-external-console-host"
                />
              </label>
              <p class="mt-1 text-[10px] leading-relaxed text-gray-500">
                Advertised verbatim in telnet://, vnc:// and rdp:// console links.
                When set, console listeners on newly started nodes become reachable
                beyond loopback (telnet/VNC/proxy ports bind the wildcard address) —
                scope them with a firewall. Nodes already running keep their previous
                binding until restarted.
              </p>
            </div>
            <label class="block text-[11px] uppercase tracking-[0.05em] text-gray-500">
              Session lifetime (seconds, min 300)
              <input
                type="number"
                min="300"
                class="mt-1 w-full rounded-lg border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-100 focus:border-blue-500 focus:outline-none"
                bind:value={sessionMaxAge}
                data-testid="settings-session-max-age"
              />
            </label>
          </div>
          <div class="mt-4 flex justify-end">
            <button
              type="submit"
              disabled={saving}
              class="rounded-lg bg-blue-600 px-4 py-2 text-[12px] font-medium text-white transition hover:bg-blue-500 disabled:opacity-50"
              data-testid="settings-save"
            >
              {saving ? 'Saving…' : 'Apply settings'}
            </button>
          </div>
        </form>

        <div class="space-y-4">
          {#each readOnlyGroups(settings) as group (group.title)}
            <div class="rounded-2xl border border-gray-800 bg-gray-900/95 p-5" data-testid={`settings-group-${group.title.toLowerCase()}`}>
              <div class="text-[10px] uppercase tracking-[0.05em] text-gray-500">{group.title} (read-only)</div>
              <dl class="mt-3 space-y-2">
                {#each group.rows as [key, value] (key)}
                  <div class="flex items-center justify-between gap-4 text-sm">
                    <dt class="shrink-0 capitalize text-gray-500">{key}</dt>
                    <dd class="truncate font-mono text-[12px] text-gray-300" title={value}>{value}</dd>
                  </div>
                {/each}
              </dl>
            </div>
          {/each}
        </div>
      </div>
    {:else}
      <div class="rounded-2xl border border-gray-800 bg-gray-900/95 px-5 py-10 text-center text-sm text-gray-500">
        Loading settings…
      </div>
    {/if}
  </div>
</div>
