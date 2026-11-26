<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  import { onMount } from 'svelte';
  import { apiGetData } from '$lib/api';
  import InfoFields from './InfoFields.svelte';
  import { dhcpStatus, leaseRemaining, type DhcpInfo } from '$lib/services/dhcpStatus';
  import { dhcpEnabled } from '$lib/services/networkDhcp';
  import type { NetworkData, NetworkInfo } from '$lib/types';

  export let labId: string;
  export let network: NetworkData;

  type Field = { label: string; value: string };
  const REFRESH_MS = 10_000;

  let info: NetworkInfo | null = null;
  let error = '';
  let requestId = 0;
  let mounted = false;

  async function load() {
    const current = ++requestId;
    try {
      const next = await apiGetData<NetworkInfo>(`/labs/${labId}/networks/${network.id}/info`, {
        suppressToast: true,
        retry: 0
      });
      if (current !== requestId) return;
      info = next;
      error = '';
    } catch (err) {
      if (current !== requestId) return;
      error = err instanceof Error ? err.message : String(err);
    }
  }

  // Reload at once when something visible in the panel changes on the canvas.
  $: dhcpKey = ['cidr', 'dhcp', 'dhcp_server', 'dhcp_start', 'dhcp_end'].map((key) => String(network.config?.[key] ?? '')).join(',');
  $: changeKey = `${labId}|${network.id}|${network.name}|${network.count ?? 0}|${network.shared === true}|${dhcpKey}`;
  $: if (mounted && changeKey) void load();

  onMount(() => {
    mounted = true;
    const timer = setInterval(() => void load(), REFRESH_MS);
    return () => {
      requestId += 1;
      clearInterval(timer);
    };
  });

  function when(value: string | null): string {
    if (!value) return '';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
  }

  function compact(fields: Array<Field | null | false>): Field[] {
    return fields.filter((field): field is Field => Boolean(field) && Boolean((field as Field).value));
  }

  function range(start: string | null, end: string | null): string {
    return start && end ? `${start} – ${end}` : '';
  }

  function bridgeStatus(bridge: NetworkInfo['bridge']): string {
    if (!bridge.exists) return 'Not present on the host';
    if (bridge.admin_up === false) return 'Disabled';
    return bridge.oper_state === 'down' ? 'Ready, no active ports' : 'Up';
  }

  function generalFields(data: NetworkInfo): Field[] {
    return compact([
      { label: 'Type', value: data.type_label },
      { label: 'Network ID', value: String(data.id) },
      { label: 'Created', value: when(data.created_at) },
      !data.visible && { label: 'Visible', value: 'No' }
    ]);
  }

  function hostFields(data: NetworkInfo): Field[] {
    return compact([
      { label: data.host_bridge ? 'Host bridge' : 'Bridge', value: data.bridge.name ?? '' },
      { label: 'Status', value: bridgeStatus(data.bridge) },
      data.bridge.ports !== null && { label: 'Active ports', value: String(data.bridge.ports) },
      { label: 'MAC address', value: data.bridge.mac ?? '' },
      data.bridge.mtu !== null && { label: 'MTU', value: String(data.bridge.mtu) }
    ]);
  }

  function addressFields(ip: NonNullable<NetworkInfo['ip']>): Field[] {
    return compact([
      { label: 'Subnet', value: ip.cidr },
      { label: 'Gateway', value: ip.gateway ?? '' },
      ip.nat && { label: 'Internet via', value: ip.egress_interface ?? '' },
      { label: 'Static range', value: range(ip.static_start, ip.static_end) },
      ip.used_ips.length > 0 && { label: 'Static in use', value: ip.used_ips.join(', ') },
      // A bridge subnet without DHCP is only a label (#325).
      !ip.nat && !ip.dhcp && { label: 'DHCP', value: 'Off' }
    ]);
  }

  function dhcpFields(dhcp: DhcpInfo, nat: boolean): Field[] {
    if (!dhcp.enabled) return [];
    return compact([
      // A NAT-Cloud serves DHCP from its gateway, already shown above.
      !nat && { label: 'Server address', value: dhcp.server ?? '' },
      { label: 'Scope', value: range(dhcp.start, dhcp.end) },
      dhcp.size !== null && { label: 'Addresses', value: `${dhcp.lease_count} of ${dhcp.size} leased` },
      { label: 'Lease time', value: dhcp.lease_time ?? '' }
    ]);
  }

  // Shown at once, and kept if the details cannot be loaded.
  $: savedCidr = typeof network.config?.cidr === 'string' ? network.config.cidr : '';
  $: fallback = compact([
    { label: 'Type', value: network.type ?? 'linux_bridge' },
    { label: 'Network ID', value: String(network.id) },
    { label: 'Connections', value: String(network.count ?? 0) },
    { label: 'Bridge', value: String(network.runtime?.bridge_name ?? '') },
    { label: 'Subnet', value: savedCidr },
    savedCidr !== '' && { label: 'DHCP', value: dhcpEnabled(network) ? 'On' : 'Off' }
  ]);
  $: sections = info
    ? [
        { title: '', fields: generalFields(info) },
        ...(info.ip ? [{ title: 'Addressing', fields: addressFields(info.ip) }] : [])
      ]
    : [];
  $: dhcp = info?.ip?.dhcp ?? null;
  $: status = dhcp ? dhcpStatus(dhcp) : null;
</script>

<div class="space-y-3" data-testid="network-info-content">
  {#if error}
    <div class="rounded-md border border-amber-500/30 bg-amber-500/10 px-2 py-1 text-[11px] text-amber-200" role="status">
      {info ? 'Could not refresh' : 'Could not load details'}: {error}
    </div>
  {/if}

  {#if !info}
    <InfoFields fields={fallback} />
  {:else}
    {#if info.mac_conflicts.length > 0}
      <div class="rounded-md border border-amber-500/30 bg-amber-500/10 px-2 py-1.5 text-[11px] text-amber-200" role="alert">
        <div class="font-medium">Duplicate MAC addresses</div>
        {#each info.mac_conflicts as conflict}
          <div class="mt-1 tabular-nums">{conflict.mac}: {conflict.endpoints.join(', ')}</div>
        {/each}
      </div>
    {/if}

    {#each sections as section}
      <section>
        {#if section.title}<h3 class="section-title">{section.title}</h3>{/if}
        <InfoFields fields={section.fields} />
      </section>
    {/each}

    {#if dhcp && status}
      <section data-testid="network-info-dhcp">
        <div class="mb-1 flex items-center justify-between gap-3">
          <h3 class="section-title !mb-0">DHCP server</h3>
          <span class="flex items-center gap-1.5 text-gray-100" data-testid="network-info-dhcp-status">
            <span class={`h-2 w-2 rounded-full ${status.dot}`} aria-hidden="true"></span>
            <span>{status.label}</span>
          </span>
        </div>
        {#if status.problem}
          <p class="mb-1 rounded-md border border-amber-500/30 bg-amber-500/10 px-2 py-1 text-[11px] text-amber-200" role="alert">
            {status.problem} {status.fix}
          </p>
        {/if}
        <InfoFields fields={dhcpFields(dhcp, info.ip?.nat === true)} />
      </section>

      {#if dhcp.enabled || dhcp.leases.length > 0}
        <section data-testid="network-info-leases">
          <h3 class="section-title">Leases{dhcp.lease_count > 0 ? ` (${dhcp.lease_count})` : ''}</h3>
          {#if dhcp.leases.length === 0}
            <p class="text-gray-400">No addresses leased yet.</p>
          {:else}
            {#if !dhcp.enabled}
              <p class="mb-1 text-gray-400">Handed out before DHCP was disabled; valid until they expire.</p>
            {/if}
            <ul class="max-h-40 space-y-1.5 overflow-y-auto">
              {#each dhcp.leases as lease (lease.ip)}
                <li title={lease.expires_at ? `Expires ${when(lease.expires_at)}` : 'Does not expire'}>
                  <div class="flex items-baseline justify-between gap-3">
                    <span class="tabular-nums text-gray-100">{lease.ip}</span>
                    <span class="min-w-0 truncate text-right text-gray-100">
                      {lease.node_name ?? lease.hostname ?? 'Unknown device'}
                    </span>
                  </div>
                  <div class="flex items-baseline justify-between gap-3 text-[10px] tabular-nums text-gray-400">
                    <span>{lease.mac}</span>
                    <span>{leaseRemaining(lease.expires_at)}</span>
                  </div>
                </li>
              {/each}
            </ul>
            {#if dhcp.lease_count > dhcp.leases.length}
              <p class="mt-1 text-gray-400">Showing {dhcp.leases.length} of {dhcp.lease_count}.</p>
            {/if}
          {/if}
        </section>
      {/if}
    {/if}

    {#if info.sharing.role !== 'none'}
      <section>
        <h3 class="section-title">Sharing</h3>
        {#if info.sharing.owner_missing}
          <p class="text-amber-200">The lab that owns this network no longer exists.</p>
        {:else}
          <p class="mb-1 text-gray-300">
            {info.sharing.role === 'member'
              ? `Shared by ${info.sharing.owner_lab}`
              : 'Shared with other labs'}
          </p>
        {/if}
        <ul class="space-y-1">
          {#each info.sharing.labs as lab}
            <li class="flex items-baseline justify-between gap-3">
              <span class="min-w-0 text-gray-100 [overflow-wrap:anywhere]">
                {#if lab.lab_path && !lab.current_lab}
                  <!-- Full load: the canvas binds its save queue to the lab it was opened for. -->
                  <a data-sveltekit-reload class="underline decoration-gray-600 underline-offset-2 hover:decoration-gray-300" href={`/labs/${lab.lab_path}`}>{lab.lab_name}</a>
                {:else}
                  {lab.lab_name}
                {/if}
                <span class="text-gray-400">
                  / {lab.network_name}{lab.current_lab ? ' (this lab)' : lab.role === 'owner' ? ' (owner)' : ''}
                </span>
              </span>
              <span class="shrink-0 text-gray-400">
                {lab.connections} connection{lab.connections === 1 ? '' : 's'}
              </span>
            </li>
          {/each}
        </ul>
      </section>
    {/if}

    <section>
      <h3 class="section-title">Connections</h3>
      {#if info.endpoints.length === 0}
        <p class="text-gray-400">Nothing is connected.</p>
      {:else}
        <ul class="max-h-48 space-y-1 overflow-y-auto">
          {#each info.endpoints as end}
            <li class="flex items-baseline justify-between gap-3">
              <span class="min-w-0 truncate text-gray-100" title={`${end.node_name} ${end.interface}`}>
                {end.node_name} <span class="text-gray-400">{end.interface}</span>
              </span>
              <span class="shrink-0 tabular-nums text-gray-400" title={end.lab_name}>
                {end.current_lab ? (end.mac ?? '') : end.lab_name}
              </span>
            </li>
          {/each}
        </ul>
      {/if}
    </section>

    <section>
      <h3 class="section-title">Host bridge</h3>
      <InfoFields fields={hostFields(info)} />
    </section>
  {/if}
</div>

<style>
  .section-title {
    margin-bottom: 0.25rem;
    font-size: 11px;
    font-weight: 600;
    color: rgb(156 163 175);
  }
</style>
