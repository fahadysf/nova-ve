<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  import { createEventDispatcher, onDestroy, onMount, tick } from 'svelte';
  import { ApiError, apiGetData, apiRequest } from '$lib/api';
  import { Lock } from 'lucide-svelte';
  import { natDefaults, natFieldFromServer, validateNatForm, type NatField } from '$lib/services/natCloudForm';
  import {
    bridgeDefaults,
    bridgeFieldFromServer,
    dhcpUnavailableReason,
    validateBridgeForm,
    type BridgeDefaults,
    type BridgeField
  } from '$lib/services/bridgeForm';
  import { networkKind } from '$lib/services/networkDhcp';
  import { networkSharing, type NetworkModalType } from '$lib/services/networkSharing';
  import { dhcpStatus } from '$lib/services/dhcpStatus';
  import type { NetworkCreateConfig, NetworkData, NetworkInfo, NetworkType, SharedNetwork } from '$lib/types';

  type FieldErrors = Partial<Record<NatField | BridgeField, string>>;
  type ScopeKey = 'dhcp_server' | 'dhcp_start' | 'dhcp_end';

  export let open = false;
  export let suspended = false;
  export let defaultName = '';
  /** #319: shared networks this lab owns or already uses are not offered. */
  export let labId = '';
  export let defaultType: NetworkModalType = 'linux_bridge';
  /** Editing an existing NAT-Cloud (#320) or bridge (#325) instead of creating a network. */
  export let mode: 'create' | 'edit' = 'create';
  export let network: NetworkData | null = null;
  /** The parent is persisting the confirmed values. */
  export let saving = false;
  /** A rejection from the save request, shown on its field when known. */
  export let saveError: { message: string; field?: string } | null = null;

  // Add new entries as more network types are productized; the wire value
  // must be a member of ``NetworkType`` so the backend POST accepts it.
  // ``shared_network`` (#319) is the exception: it submits the owner's type.
  const TYPE_OPTIONS: { value: NetworkModalType; label: string }[] = [
    { value: 'linux_bridge', label: 'Bridge (linux_bridge)' },
    { value: 'nat_cloud', label: 'NAT-Cloud (nat_cloud)' },
    { value: 'bridge_cloud', label: 'Bridge-Cloud (host bridge)' },
    { value: 'shared_network', label: 'Shared network (from another lab)' }
  ];

  interface HostBridge {
    id: string;
    label: string;
    host_bridge: string;
    iface: string;
    carrier: boolean;
    addrs: string[];
  }

  interface NatCloud {
    id: string;
    lab_name: string;
    lab_path: string;
    network_name: string;
    cidr?: string | null;
    gateway?: string | null;
    bridge_name?: string | null;
    is_reference?: boolean;
    safe_for_reuse?: boolean;
    warning?: string | null;
  }

  const dispatch = createEventDispatcher<{
    confirm: { name: string; type: NetworkType; config?: NetworkCreateConfig };
    cancel: void;
  }>();

  let name = defaultName;
  let type: NetworkModalType = defaultType;
  let sharedNetworks: SharedNetwork[] = [];
  let selectedSharedId = '';
  let sharedLoading = false;
  let sharedLoaded = false;
  let sharedError: string | null = null;
  let cidr = '';
  let gateway = '';
  let dhcp = true;
  let dhcpStart = '';
  let dhcpEnd = '';
  let egressInterface = '';
  // linux_bridge (#325): DHCP is off by default and the scope is prefilled.
  let bridgeDhcp = false;
  let bridgeServer = '';
  let bridgeStart = '';
  let bridgeEnd = '';
  // Set once a field holds the user's (or a saved) value, so the defaults of
  // a newly typed subnet no longer replace it.
  let serverEdited = false;
  let startEdited = false;
  let endEdited = false;
  let natCloudMode: 'new' | 'existing' = 'new';
  let selectedNatCloudId = '';
  let natClouds: NatCloud[] = [];
  let selectedHostBridge = '';
  let hostBridges: HostBridge[] = [];
  let natCloudsLoading = false;
  let natCloudsLoaded = false;
  let natCloudsError: string | null = null;
  let hostBridgesLoading = false;
  let hostBridgesLoaded = false;
  let hostBridgesError: string | null = null;
  let modalEl: HTMLDivElement | null = null;
  let nameInputEl: HTMLInputElement | null = null;
  let wasOpen = false;
  /** Bumped on every open, so a late response cannot act on the next form. */
  let session = 0;
  let returnFocusEl: HTMLElement | null = null;
  let checking = false;
  /** Server rejection of the current values; cleared on the next change. */
  let rejection: { message: string; field?: string } | null = null;
  let staleSaveError: typeof saveError = null;
  /** Live network details (DHCP server state, owner of a shared network); null while unknown. */
  let liveInfo: NetworkInfo | null = null;
  let liveInfoError = false;

  async function loadLiveInfo(networkId: number) {
    const mine = session;
    try {
      const info = await apiGetData<NetworkInfo>(`/labs/${labId}/networks/${networkId}/info`, {
        suppressToast: true,
        retry: 0
      });
      if (open && mine === session) liveInfo = info ?? null;
    } catch {
      if (open && mine === session) liveInfoError = true;
    }
  }

  /** A new value makes the previous server verdict stale. */
  function dismissServerError() {
    rejection = null;
    staleSaveError = saveError;
  }

  function selectNatCloudMode(next: 'new' | 'existing') {
    natCloudMode = next;
    dismissServerError();
  }

  function describedBy(error: string | undefined, errorId: string, noteId = ''): string | undefined {
    const ids = [error ? errorId : '', noteId].filter(Boolean);
    return ids.length > 0 ? ids.join(' ') : undefined;
  }

  function text(value: unknown): string {
    return typeof value === 'string' ? value : '';
  }

  function configText(key: string): string {
    return text(network?.config?.[key]);
  }

  /** Prefill the scope fields the user has not edited from the subnet's defaults. */
  function applyBridgeDefaults(next: BridgeDefaults | null) {
    if (!serverEdited) bridgeServer = next?.server ?? '';
    if (!startEdited) bridgeStart = next?.start ?? '';
    if (!endEdited) bridgeEnd = next?.end ?? '';
  }

  // Single reactive block so the open-edge detection and the prev-state
  // update share one Svelte invalidation pass — the two-block form let
  // the trailing ``wasOpen = open`` race ahead of the edge check on
  // simultaneous prop transitions.
  $: {
    if (open && !wasOpen) {
      const editing = mode === 'edit' && network !== null;
      session += 1;
      returnFocusEl = typeof document !== 'undefined' ? (document.activeElement as HTMLElement | null) : null;
      const editedBridge = editing && network !== null && networkKind(network) !== 'nat_cloud';
      name = editing ? (network?.name ?? '') : defaultName;
      type = editing ? (editedBridge ? 'linux_bridge' : 'nat_cloud') : defaultType;
      cidr = editing ? configText('cidr') : '';
      gateway = editing ? configText('gateway') : '';
      dhcp = editing ? network?.config?.dhcp !== false : true;
      dhcpStart = editing ? configText('dhcp_start') : '';
      dhcpEnd = editing ? configText('dhcp_end') : '';
      egressInterface = editing
        ? String(network?.runtime?.egress_interface ?? configText('egress_interface'))
        : '';
      bridgeDhcp = editedBridge && network?.config?.dhcp === true;
      bridgeServer = editedBridge ? configText('dhcp_server') : '';
      bridgeStart = editedBridge ? configText('dhcp_start') : '';
      bridgeEnd = editedBridge ? configText('dhcp_end') : '';
      // Saved values are the user's; only empty fields follow the subnet.
      serverEdited = bridgeServer !== '';
      startEdited = bridgeStart !== '';
      endEdited = bridgeEnd !== '';
      applyBridgeDefaults(bridgeDefaults(cidr));
      checking = false;
      rejection = null;
      liveInfo = null;
      liveInfoError = false;
      if (editing && network) void loadLiveInfo(network.id);
      natCloudMode = 'new';
      selectedNatCloudId = '';
      natClouds = [];
      selectedHostBridge = '';
      hostBridges = [];
      natCloudsError = null;
      natCloudsLoaded = false;
      hostBridgesError = null;
      hostBridgesLoaded = false;
      sharedNetworks = [];
      selectedSharedId = '';
      sharedLoaded = false;
      sharedError = null;
      void focusName();
    } else if (!open && wasOpen) {
      returnFocusEl?.focus?.();
    }
    wasOpen = open;
  }

  // Fetch the admin cloud inventory for NAT-Cloud reuse. Bridge-Cloud keeps
  // its own fallback path so NAT inventory failures cannot poison host-bridge
  // selection later in the same modal session.
  $: needsNatCloudInventory = open && type === 'nat_cloud' && natCloudMode === 'existing';
  $: if (needsNatCloudInventory && !natCloudsLoaded && !natCloudsLoading) {
    void loadCloudInventory();
  }
  $: if (open && type === 'bridge_cloud' && !hostBridgesLoaded && !hostBridgesLoading) {
    void loadHostBridges();
  }

  $: if (open && type === 'shared_network' && !sharedLoaded && !sharedLoading) {
    void loadSharedNetworks();
  }
  $: selectedShared = sharedNetworks.find((entry) => entry.id === selectedSharedId) ?? null;

  $: reusableNatClouds = natClouds.filter((cloud) => !cloud.is_reference && cloud.safe_for_reuse !== false);
  $: if (type === 'nat_cloud' && natCloudMode === 'existing' && !selectedNatCloudId && reusableNatClouds.length > 0) {
    selectedNatCloudId = reusableNatClouds[0].id;
  }
  $: trimmedName = name.trim();
  $: editing = mode === 'edit';
  $: stored = (network?.config ?? {}) as Record<string, unknown>;
  // A network joined from another lab can be renamed here, nothing else (#319).
  $: member = editing && network !== null && networkSharing(network).role === 'member';
  $: natForm = !member && type === 'nat_cloud' && (editing || natCloudMode === 'new');
  $: bridgeForm = !member && type === 'linux_bridge';
  // A NAT-Cloud's subnet is fixed at creation; a bridge's once it has one.
  $: cidrLocked = editing && (type === 'nat_cloud' || text(stored.cidr) !== '');
  $: defaults = natDefaults(cidr);
  $: bridgeScopeDefaults = bridgeDefaults(cidr);
  $: applyBridgeDefaults(bridgeScopeDefaults);
  $: bridgeDhcpReason = dhcpUnavailableReason(cidr);
  // The checkbox keeps its state while the subnet is being typed; it only counts once the subnet qualifies.
  $: bridgeDhcpOn = bridgeDhcp && bridgeScopeDefaults !== null;
  $: storedBridgeDhcp = stored.dhcp === true;
  // Locked values come from the server, so only editable fields are checked.
  $: liveErrors = (natForm
    ? validateNatForm({ cidr, gateway, dhcp, dhcpStart, dhcpEnd })
    : bridgeForm
      ? validateBridgeForm({ cidr, dhcp: bridgeDhcp, dhcpServer: bridgeServer, dhcpStart: bridgeStart, dhcpEnd: bridgeEnd })
      : {}) as FieldErrors;
  $: serverError = saveError && saveError !== staleSaveError ? saveError : rejection;
  $: serverField = natForm
    ? natFieldFromServer(serverError?.field)
    : bridgeForm
      ? bridgeFieldFromServer(serverError?.field)
      : null;
  $: errors = (serverField && serverError
    ? { ...liveErrors, [serverField]: serverError.message }
    : liveErrors) as FieldErrors;
  $: formMessage = serverError && !serverField ? serverError.message : '';
  $: natDhcpChanged =
    editing &&
    natForm &&
    (dhcp !== (stored.dhcp !== false) ||
      (dhcp && (dhcpStart.trim() !== text(stored.dhcp_start) || dhcpEnd.trim() !== text(stored.dhcp_end))));
  $: cidrAdded = editing && bridgeForm && !cidrLocked && cidr.trim() !== '';
  $: bridgeScope = [
    ['dhcp_server', bridgeServer, serverEdited],
    ['dhcp_start', bridgeStart, startEdited],
    ['dhcp_end', bridgeEnd, endEdited]
  ] as Array<[ScopeKey, string, boolean]>;
  $: bridgeScopeChanged =
    bridgeDhcpOn && bridgeScope.some(([key, value, edited]) => edited && value.trim() !== text(stored[key]));
  $: bridgeChanged =
    editing && bridgeForm && (cidrAdded || bridgeDhcpOn !== storedBridgeDhcp || bridgeScopeChanged);
  $: dhcpChanged = natDhcpChanged || bridgeChanged;
  $: currentDhcp = natForm ? dhcp : bridgeDhcpOn;
  $: renamed = trimmedName !== (network?.name ?? '');
  $: turningDhcpOff =
    editing && (natForm ? !dhcp && stored.dhcp !== false : bridgeForm && !bridgeDhcpOn && storedBridgeDhcp);
  $: busy = saving || checking;
  $: liveDhcp = liveInfo?.ip?.dhcp ?? null;
  $: liveStatus = liveDhcp ? dhcpStatus(liveDhcp) : null;
  $: ownerLab = liveInfo?.sharing?.owner_lab || 'the lab that owns this network';
  $: kindLabel = type === 'nat_cloud' ? 'NAT-Cloud' : type === 'linux_bridge' ? 'bridge' : 'network';
  // The saved setting is right but the server is not in that state.
  // Never combined with a rename, which must not fail because DHCP cannot start.
  $: repairOnly =
    editing &&
    (natForm || bridgeForm) &&
    !dhcpChanged &&
    !renamed &&
    liveDhcp?.enabled === currentDhcp &&
    !!liveStatus?.problem;
  $: submitLabel = !editing
    ? 'Create'
    : repairOnly
      ? dhcp
        ? 'Start DHCP server'
        : 'Stop DHCP server'
      : 'Save changes';
  $: canSubmit =
    !busy &&
    (!editing || dhcpChanged || renamed || repairOnly) &&
    Object.keys(liveErrors).length === 0 &&
    trimmedName.length > 0 &&
    (type !== 'nat_cloud' || natCloudMode !== 'existing' || selectedNatCloudId.length > 0) &&
    (type !== 'bridge_cloud' || selectedHostBridge.length > 0) &&
    (type !== 'shared_network' || selectedShared !== null);

  async function loadCloudInventory() {
    natCloudsLoading = true;
    natCloudsError = null;
    try {
      const resp = await fetch('/api/system/clouds', { credentials: 'include' });
      if (!resp.ok) {
        throw new Error(`HTTP ${resp.status}`);
      }
      const payload = await resp.json();
      const data = payload?.data ?? {};
      natClouds = (data.nat_clouds ?? []) as NatCloud[];
      hostBridges = (data.bridge_clouds ?? []) as HostBridge[];
      if (hostBridges.length > 0 && !selectedHostBridge) {
        selectedHostBridge = hostBridges[0].host_bridge;
      }
    } catch (err) {
      natCloudsError = err instanceof Error ? err.message : String(err);
      natClouds = [];
    } finally {
      natCloudsLoading = false;
      // Terminal state — empty results and errors both count as
      // "loaded" so the reactive fetch trigger does not refire.
      natCloudsLoaded = true;
    }
  }

  async function loadSharedNetworks() {
    sharedLoading = true;
    sharedError = null;
    try {
      sharedNetworks = await apiGetData<SharedNetwork[]>(
        `/system/shared-networks?for_lab=${encodeURIComponent(labId)}`,
        { suppressToast: true }
      );
      selectedSharedId = sharedNetworks[0]?.id ?? '';
    } catch (err) {
      sharedError = err instanceof Error ? err.message : String(err);
      sharedNetworks = [];
    } finally {
      sharedLoading = false;
      sharedLoaded = true;
    }
  }

  async function loadHostBridges() {
    hostBridgesLoading = true;
    hostBridgesError = null;
    try {
      const resp = await fetch('/api/system/bridge-clouds', { credentials: 'include' });
      if (!resp.ok) {
        throw new Error(`HTTP ${resp.status}`);
      }
      const payload = await resp.json();
      hostBridges = (payload?.data ?? []) as HostBridge[];
      if (hostBridges.length > 0 && !selectedHostBridge) {
        selectedHostBridge = hostBridges[0].host_bridge;
      }
    } catch (err) {
      hostBridgesError = err instanceof Error ? err.message : String(err);
      hostBridges = [];
    } finally {
      hostBridgesLoading = false;
      hostBridgesLoaded = true;
    }
  }

  function fieldClass(error: string | undefined, locked: boolean): string {
    const base = 'w-full rounded-xl border bg-slate-900/60 px-3 py-2 text-sm text-slate-100 outline-none transition focus:ring-1';
    if (error) return `${base} border-red-500/60 focus:ring-red-500/30`;
    if (locked) return `${base} border-dashed border-slate-800 text-slate-400 focus:ring-slate-600/40`;
    return `${base} border-slate-800 focus:border-blue-500/40 focus:ring-blue-500/30`;
  }

  async function focusName() {
    await tick();
    if (suspended) return;
    nameInputEl?.focus();
    nameInputEl?.select();
  }

  function cancel() {
    if (saving) return;
    dispatch('cancel');
  }

  /** Side-effect-free server check (subnet conflicts, scope rules) before saving. */
  async function preflight(kind: 'nat_cloud' | 'linux_bridge', config: NetworkCreateConfig): Promise<boolean> {
    const mine = session;
    checking = true;
    rejection = null;
    const target = editing ? { network_id: network?.id } : {};
    try {
      await apiRequest(
        kind === 'nat_cloud'
          ? `/labs/${labId}/networks/validate-nat-cloud`
          : `/labs/${labId}/networks/validate-config`,
        {
          method: 'POST',
          body:
            kind === 'nat_cloud'
              ? { config: { ...config }, ...target }
              : { type: kind, config: { ...config }, ...target },
          suppressToast: true
        }
      );
      return open && mine === session;
    } catch (err) {
      if (mine !== session) return false;
      rejection = {
        message: err instanceof Error ? err.message : String(err),
        field: err instanceof ApiError ? err.field : undefined
      };
      return false;
    } finally {
      if (mine === session) checking = false;
    }
  }

  /** What a bridge form submits: on create everything set, on edit only what changed. */
  function bridgeConfig(): NetworkCreateConfig {
    const config: NetworkCreateConfig = {};
    if (!editing) {
      if (cidr.trim()) config.cidr = cidr.trim();
      if (bridgeDhcpOn) {
        config.dhcp = true;
        // Untouched prefilled values are the server's defaults; let it fill them.
        for (const [key, value, edited] of bridgeScope) if (edited && value.trim()) config[key] = value.trim();
      }
      return config;
    }
    if (cidrAdded) config.cidr = cidr.trim();
    if (repairOnly || bridgeDhcpOn !== storedBridgeDhcp) config.dhcp = bridgeDhcpOn;
    if (bridgeDhcpOn) {
      // Blank means "back to the default", so it is sent too.
      for (const [key, value, edited] of bridgeScope) {
        if (edited && value.trim() !== text(stored[key])) config[key] = value.trim();
      }
    }
    return config;
  }

  async function confirm() {
    if (!canSubmit) return;
    dismissServerError();
    const config: NetworkCreateConfig = {};
    // Everything submitted is read before the check: the fields, the type
    // included, stay editable while it runs.
    const name = trimmedName;
    const submitType = type;
    if (editing) {
      const editType = submitType === 'nat_cloud' ? 'nat_cloud' : 'linux_bridge';
      if (member) {
        // Settings belong to the owning lab; a rename is all this lab can do.
      } else if (natForm && (dhcpChanged || repairOnly)) {
        // Only what changed: a stale form must not undo someone else's edit.
        if (repairOnly || dhcp !== (network?.config?.dhcp !== false)) config.dhcp = dhcp;
        // Blank means "back to the default", so it is sent too.
        if (dhcp && dhcpStart.trim() !== configText('dhcp_start')) config.dhcp_start = dhcpStart.trim();
        if (dhcp && dhcpEnd.trim() !== configText('dhcp_end')) config.dhcp_end = dhcpEnd.trim();
        if (!(await preflight('nat_cloud', config))) return;
      } else if (bridgeForm && (dhcpChanged || repairOnly)) {
        Object.assign(config, bridgeConfig());
        if (!(await preflight('linux_bridge', config))) return;
      }
      dispatch('confirm', { name, type: editType, config });
      return;
    }
    if (submitType === 'linux_bridge') {
      Object.assign(config, bridgeConfig());
      if (Object.keys(config).length > 0 && !(await preflight('linux_bridge', config))) return;
    } else if (submitType === 'nat_cloud') {
      if (natCloudMode === 'existing') {
        config.shared_cloud_id = selectedNatCloudId;
      } else {
        if (cidr.trim()) config.cidr = cidr.trim();
        if (gateway.trim()) config.gateway = gateway.trim();
        config.dhcp = dhcp;
        if (dhcp && dhcpStart.trim()) config.dhcp_start = dhcpStart.trim();
        if (dhcp && dhcpEnd.trim()) config.dhcp_end = dhcpEnd.trim();
        if (egressInterface.trim()) config.egress_interface = egressInterface.trim();
        if (!(await preflight('nat_cloud', config))) return;
      }
    } else if (submitType === 'bridge_cloud') {
      config.host_bridge = selectedHostBridge;
    } else if (submitType === 'shared_network') {
      if (!selectedShared) return;
      config.shared_network_id = selectedShared.id;
      dispatch('confirm', { name, type: selectedShared.type, config });
      return;
    }
    dispatch('confirm', { name, type: submitType, config });
  }

  function onKey(event: KeyboardEvent) {
    if (!open || suspended || event.defaultPrevented) return;
    if (event.target instanceof HTMLElement && event.target !== document.body && !modalEl?.contains(event.target)) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      cancel();
    } else if (event.key === 'Enter') {
      // A focused button keeps its own action (Enter on Cancel must cancel).
      if (event.target instanceof HTMLButtonElement) return;
      event.preventDefault();
      void confirm();
    } else if (event.key === 'Tab') {
      trapFocus(event);
    }
  }

  function trapFocus(event: KeyboardEvent) {
    const items = Array.from(
      modalEl?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled)') ?? []
    );
    if (items.length === 0) return;
    const first = items[0];
    const last = items[items.length - 1];
    const active = document.activeElement;
    const outside = !modalEl?.contains(active);
    if (event.shiftKey && (active === first || outside)) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && (active === last || outside)) {
      event.preventDefault();
      first.focus();
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
      aria-label={editing ? `Configure ${kindLabel}` : 'Configure new network'}
      class="flex w-full max-w-md flex-col overflow-hidden rounded-[1.4rem] border border-slate-700 bg-slate-950/95 shadow-2xl shadow-black/40"
      data-testid="network-config-modal"
    >
      <div class="flex items-center justify-between border-b border-slate-800 px-5 py-4">
        <div>
          <div class="text-[10px] uppercase tracking-[0.05em] text-slate-500">
            {editing ? 'Existing network' : 'New network'}
          </div>
          <div class="mt-1 text-sm font-semibold text-slate-100">
            Configure {kindLabel}
          </div>
        </div>
        <button
          type="button"
          class="inline-flex h-8 w-8 items-center justify-center rounded-full border border-slate-800 bg-slate-900 text-slate-300 transition hover:border-blue-500/40 hover:text-white"
          aria-label={editing ? 'Close' : 'Cancel new network'}
          on:click={cancel}
        >
          ×
        </button>
      </div>

      <div
        class="max-h-[70vh] space-y-4 overflow-y-auto px-5 py-4 text-sm text-slate-200"
        role="presentation"
        on:input={dismissServerError}
      >
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

        {#if !editing}
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
        {/if}

        {#if type === 'nat_cloud' && !member}
          <div class="space-y-3 rounded-xl border border-slate-800 bg-slate-900/30 p-3">
            {#if !editing}
            <div class="grid grid-cols-2 rounded-xl border border-slate-800 bg-slate-950/60 p-1">
              <button
                type="button"
                class={`rounded-lg px-3 py-1.5 text-xs transition ${natCloudMode === 'new' ? 'bg-blue-500/20 text-blue-100' : 'text-slate-400 hover:text-slate-100'}`}
                on:click={() => selectNatCloudMode('new')}
              >
                New
              </button>
              <button
                type="button"
                class={`rounded-lg px-3 py-1.5 text-xs transition ${natCloudMode === 'existing' ? 'bg-blue-500/20 text-blue-100' : 'text-slate-400 hover:text-slate-100'}`}
                on:click={() => selectNatCloudMode('existing')}
              >
                Existing
              </button>
            </div>
            {/if}

            {#if !editing && natCloudMode === 'existing'}
              <label class="block space-y-1.5">
                <span class="text-[10px] uppercase tracking-[0.05em] text-slate-500">NAT-Cloud</span>
                {#if natCloudsLoading}
                  <div class="rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-400">
                    Loading NAT-Clouds...
                  </div>
                {:else if natCloudsError}
                  <div class="rounded-xl border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-200">
                    Failed to list NAT-Clouds: {natCloudsError}
                  </div>
                {:else if reusableNatClouds.length === 0}
                  <div class="rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-xs text-slate-400">
                    No reusable NAT-Clouds found.
                  </div>
                {:else}
                  <select
                    bind:value={selectedNatCloudId}
                    class="w-full rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 outline-none transition focus:border-blue-500/40 focus:ring-1 focus:ring-blue-500/30"
                    data-testid="network-nat-cloud-select"
                  >
                    {#each reusableNatClouds as cloud}
                      <option value={cloud.id}>
                        {cloud.network_name} - {cloud.cidr ?? 'no CIDR'} - {cloud.lab_name}
                      </option>
                    {/each}
                  </select>
                {/if}
              </label>
            {:else}
              <div class="grid gap-3 sm:grid-cols-2">
                <div class="space-y-1.5 sm:col-span-2">
                  <label for="nat-cidr" class="flex items-center gap-1 text-[10px] uppercase tracking-[0.05em] text-slate-500">
                    CIDR
                    {#if editing}<Lock class="h-3 w-3" aria-hidden="true" />{/if}
                  </label>
                  <input
                    id="nat-cidr"
                    bind:value={cidr}
                    type="text"
                    autocomplete="off"
                    spellcheck="false"
                    class={fieldClass(errors.cidr, editing)}
                    placeholder={'Auto from NAT-Cloud pool'}
                    aria-invalid={errors.cidr ? 'true' : undefined}
                    readonly={editing}
                    aria-describedby={describedBy(errors.cidr, 'nat-cidr-error', editing ? 'nat-locked-note' : '')}
                  />
                  {#if errors.cidr}
                    <p id="nat-cidr-error" class="text-xs text-red-300" role="alert">{errors.cidr}</p>
                  {/if}
                </div>
                <div class="space-y-1.5">
                  <label for="nat-gateway" class="flex items-center gap-1 text-[10px] uppercase tracking-[0.05em] text-slate-500">
                    Gateway
                    {#if editing}<Lock class="h-3 w-3" aria-hidden="true" />{/if}
                  </label>
                  <input
                    id="nat-gateway"
                    bind:value={gateway}
                    type="text"
                    autocomplete="off"
                    spellcheck="false"
                    class={fieldClass(errors.gateway, editing)}
                    placeholder={defaults?.gateway ?? 'Automatic'}
                    aria-invalid={errors.gateway ? 'true' : undefined}
                    readonly={editing}
                    aria-describedby={describedBy(errors.gateway, 'nat-gateway-error', editing ? 'nat-locked-note' : '')}
                  />
                  {#if errors.gateway}
                    <p id="nat-gateway-error" class="text-xs text-red-300" role="alert">{errors.gateway}</p>
                  {/if}
                </div>
                <div class="space-y-1.5">
                  <label for="nat-egress" class="flex items-center gap-1 text-[10px] uppercase tracking-[0.05em] text-slate-500">
                    Egress
                    {#if editing}<Lock class="h-3 w-3" aria-hidden="true" />{/if}
                  </label>
                  <input
                    id="nat-egress"
                    bind:value={egressInterface}
                    type="text"
                    autocomplete="off"
                    spellcheck="false"
                    class={fieldClass(errors.egress_interface, editing)}
                    placeholder={'Default route'}
                    aria-invalid={errors.egress_interface ? 'true' : undefined}
                    readonly={editing}
                    aria-describedby={describedBy(errors.egress_interface, 'nat-egress-error', editing ? 'nat-locked-note' : '')}
                  />
                  {#if errors.egress_interface}
                    <p id="nat-egress-error" class="text-xs text-red-300" role="alert">{errors.egress_interface}</p>
                  {/if}
                </div>
                {#if editing}
                  <p id="nat-locked-note" class="text-xs text-slate-400 sm:col-span-2">
                    To change the subnet, delete this NAT-Cloud and create a new one with the correct subnet.
                    Gateway and egress are fixed too.
                  </p>
                {/if}
                <div class="flex items-center justify-between gap-3 border-t border-slate-800 pt-3 sm:col-span-2">
                  <label class="flex items-center gap-2">
                    <input bind:checked={dhcp} type="checkbox" class="h-4 w-4 rounded border-slate-700 bg-slate-900" />
                    <span class="text-xs text-slate-300">Enable DHCP</span>
                  </label>
                  {#if editing}
                    <span class="flex items-center gap-1.5 text-xs text-slate-400" data-testid="nat-dhcp-status">
                      {#if liveStatus}
                        <span class={`h-2 w-2 rounded-full ${liveStatus.dot}`} aria-hidden="true"></span>
                        Server: {liveStatus.label}{liveDhcp && liveDhcp.lease_count > 0
                          ? ` · ${liveDhcp.lease_count} leased`
                          : ''}
                      {:else}
                        {liveInfoError ? 'Server: status unavailable' : 'Server: checking…'}
                      {/if}
                    </span>
                  {/if}
                </div>
                {#if editing && liveStatus?.problem}
                  <p class="rounded-xl border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-xs text-amber-200 sm:col-span-2">
                    {liveStatus.problem}
                  </p>
                {/if}
                {#if dhcp}
                <div class="space-y-1.5">
                  <label for="nat-dhcp-start" class="flex items-center gap-1 text-[10px] uppercase tracking-[0.05em] text-slate-500">
                    DHCP start
                  </label>
                  <input
                    id="nat-dhcp-start"
                    bind:value={dhcpStart}
                    type="text"
                    autocomplete="off"
                    spellcheck="false"
                    class={fieldClass(errors.dhcp_start, false)}
                    placeholder={defaults?.dhcpStart ?? 'Automatic'}
                    aria-invalid={errors.dhcp_start ? 'true' : undefined}
                    aria-describedby={errors.dhcp_start ? 'nat-dhcp-start-error' : undefined}
                  />
                  {#if errors.dhcp_start}
                    <p id="nat-dhcp-start-error" class="text-xs text-red-300" role="alert">{errors.dhcp_start}</p>
                  {/if}
                </div>
                <div class="space-y-1.5">
                  <label for="nat-dhcp-end" class="flex items-center gap-1 text-[10px] uppercase tracking-[0.05em] text-slate-500">
                    DHCP end
                  </label>
                  <input
                    id="nat-dhcp-end"
                    bind:value={dhcpEnd}
                    type="text"
                    autocomplete="off"
                    spellcheck="false"
                    class={fieldClass(errors.dhcp_end, false)}
                    placeholder={defaults?.dhcpEnd ?? 'Automatic'}
                    aria-invalid={errors.dhcp_end ? 'true' : undefined}
                    aria-describedby={errors.dhcp_end ? 'nat-dhcp-end-error' : undefined}
                  />
                  {#if errors.dhcp_end}
                    <p id="nat-dhcp-end-error" class="text-xs text-red-300" role="alert">{errors.dhcp_end}</p>
                  {/if}
                </div>
                {:else if turningDhcpOff}
                  <p class="rounded-xl border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-xs text-amber-200 sm:col-span-2">
                    Nodes keep their current addresses until the lease expires (12&nbsp;h) or they renew.
                  </p>
                {:else}
                  <p class="text-xs text-slate-400 sm:col-span-2">Nodes on this network need static addresses.</p>
                {/if}
              </div>
            {/if}
          </div>
        {/if}

        {#if bridgeForm}
          <div class="space-y-3 rounded-xl border border-slate-800 bg-slate-900/30 p-3" data-testid="bridge-settings">
            <div class="grid gap-3 sm:grid-cols-2">
              <div class="space-y-1.5 sm:col-span-2">
                <label for="bridge-cidr" class="flex items-center gap-1 text-[10px] uppercase tracking-[0.05em] text-slate-500">
                  CIDR
                  {#if cidrLocked}<Lock class="h-3 w-3" aria-hidden="true" />{/if}
                </label>
                <input
                  id="bridge-cidr"
                  bind:value={cidr}
                  type="text"
                  autocomplete="off"
                  spellcheck="false"
                  class={fieldClass(errors.cidr, cidrLocked)}
                  placeholder="Optional, e.g. 10.10.0.0/24"
                  aria-invalid={errors.cidr ? 'true' : undefined}
                  readonly={cidrLocked}
                  aria-describedby={describedBy(errors.cidr, 'bridge-cidr-error', cidrLocked ? 'bridge-locked-note' : 'bridge-cidr-note')}
                />
                {#if errors.cidr}
                  <p id="bridge-cidr-error" class="text-xs text-red-300" role="alert">{errors.cidr}</p>
                {/if}
                {#if cidrLocked}
                  <p id="bridge-locked-note" class="text-xs text-slate-400">
                    The subnet cannot be changed. To use another one, delete and recreate the network.
                  </p>
                {:else}
                  <p id="bridge-cidr-note" class="text-xs text-slate-400">
                    Optional. Without DHCP it is only a label: the host takes no address on this network.
                  </p>
                {/if}
              </div>

              <div class="flex items-center justify-between gap-3 border-t border-slate-800 pt-3 sm:col-span-2">
                {#if bridgeScopeDefaults}
                  <label class="flex items-center gap-2">
                    <input
                      bind:checked={bridgeDhcp}
                      type="checkbox"
                      class="h-4 w-4 rounded border-slate-700 bg-slate-900"
                      aria-invalid={errors.dhcp ? 'true' : undefined}
                      aria-describedby={errors.dhcp ? 'bridge-dhcp-error' : undefined}
                    />
                    <span class="text-xs text-slate-300">Enable DHCP</span>
                  </label>
                {:else}
                  <p class="text-xs text-slate-400" data-testid="bridge-dhcp-unavailable">{bridgeDhcpReason}</p>
                {/if}
                {#if editing && (liveStatus || (cidrLocked && bridgeScopeDefaults && !liveInfo))}
                  <span class="flex shrink-0 items-center gap-1.5 text-xs text-slate-400" data-testid="bridge-dhcp-status">
                    {#if liveStatus}
                      <span class={`h-2 w-2 rounded-full ${liveStatus.dot}`} aria-hidden="true"></span>
                      Server: {liveStatus.label}{liveDhcp && liveDhcp.lease_count > 0
                        ? ` · ${liveDhcp.lease_count} leased`
                        : ''}
                    {:else}
                      {liveInfoError ? 'Server: status unavailable' : 'Server: checking…'}
                    {/if}
                  </span>
                {/if}
              </div>
              {#if errors.dhcp}
                <p id="bridge-dhcp-error" class="text-xs text-red-300 sm:col-span-2" role="alert">{errors.dhcp}</p>
              {/if}
              {#if editing && liveStatus?.problem}
                <p class="rounded-xl border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-xs text-amber-200 sm:col-span-2">
                  {liveStatus.problem}
                </p>
              {/if}

              {#if bridgeDhcpOn}
                <div class="space-y-1.5 sm:col-span-2">
                  <label for="bridge-dhcp-server" class="flex items-center gap-1 text-[10px] uppercase tracking-[0.05em] text-slate-500">
                    DHCP server
                  </label>
                  <input
                    id="bridge-dhcp-server"
                    bind:value={bridgeServer}
                    on:input={() => (serverEdited = true)}
                    type="text"
                    autocomplete="off"
                    spellcheck="false"
                    class={fieldClass(errors.dhcp_server, false)}
                    placeholder={bridgeScopeDefaults?.server ?? ''}
                    aria-invalid={errors.dhcp_server ? 'true' : undefined}
                    aria-describedby={describedBy(errors.dhcp_server, 'bridge-dhcp-server-error', 'bridge-dhcp-server-note')}
                  />
                  {#if errors.dhcp_server}
                    <p id="bridge-dhcp-server-error" class="text-xs text-red-300" role="alert">{errors.dhcp_server}</p>
                  {/if}
                  <p id="bridge-dhcp-server-note" class="text-xs text-slate-400">
                    The host's address on this network, outside the range. It answers DHCP only.
                  </p>
                </div>
                <div class="space-y-1.5">
                  <label for="bridge-dhcp-start" class="flex items-center gap-1 text-[10px] uppercase tracking-[0.05em] text-slate-500">
                    DHCP start
                  </label>
                  <input
                    id="bridge-dhcp-start"
                    bind:value={bridgeStart}
                    on:input={() => (startEdited = true)}
                    type="text"
                    autocomplete="off"
                    spellcheck="false"
                    class={fieldClass(errors.dhcp_start, false)}
                    placeholder={bridgeScopeDefaults?.start ?? ''}
                    aria-invalid={errors.dhcp_start ? 'true' : undefined}
                    aria-describedby={errors.dhcp_start ? 'bridge-dhcp-start-error' : undefined}
                  />
                  {#if errors.dhcp_start}
                    <p id="bridge-dhcp-start-error" class="text-xs text-red-300" role="alert">{errors.dhcp_start}</p>
                  {/if}
                </div>
                <div class="space-y-1.5">
                  <label for="bridge-dhcp-end" class="flex items-center gap-1 text-[10px] uppercase tracking-[0.05em] text-slate-500">
                    DHCP end
                  </label>
                  <input
                    id="bridge-dhcp-end"
                    bind:value={bridgeEnd}
                    on:input={() => (endEdited = true)}
                    type="text"
                    autocomplete="off"
                    spellcheck="false"
                    class={fieldClass(errors.dhcp_end, false)}
                    placeholder={bridgeScopeDefaults?.end ?? ''}
                    aria-invalid={errors.dhcp_end ? 'true' : undefined}
                    aria-describedby={errors.dhcp_end ? 'bridge-dhcp-end-error' : undefined}
                  />
                  {#if errors.dhcp_end}
                    <p id="bridge-dhcp-end-error" class="text-xs text-red-300" role="alert">{errors.dhcp_end}</p>
                  {/if}
                </div>
                <p
                  class="rounded-xl border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-xs text-amber-200 sm:col-span-2"
                  data-testid="bridge-dhcp-warning"
                >
                  Run only one DHCP server on this network: turn off DHCP on any node attached here, such as a lab
                  router. Leases carry no gateway or DNS server.
                </p>
              {:else if turningDhcpOff}
                <p class="rounded-xl border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-xs text-amber-200 sm:col-span-2">
                  Nodes keep their current addresses until the lease expires (12&nbsp;h) or they renew.
                </p>
              {/if}
            </div>
          </div>
        {/if}

        {#if member}
          <div
            class="space-y-3 rounded-xl border border-dashed border-slate-800 bg-slate-900/30 p-3"
            data-testid="network-member-settings"
          >
            <p class="flex items-start gap-1.5 text-xs text-slate-300">
              <Lock class="mt-0.5 h-3 w-3 shrink-0" aria-hidden="true" />
              <span>
                Settings are managed by <strong class="font-medium text-slate-100">{ownerLab}</strong>. Change them in
                that lab; only the name can be changed here.
              </span>
            </p>
            <dl class="grid grid-cols-[auto,1fr] gap-x-4 gap-y-1 text-xs">
              <dt class="text-slate-500">Subnet</dt>
              <dd class="text-slate-300">
                {liveInfo ? (liveInfo.ip?.cidr ?? 'None') : liveInfoError ? 'Unavailable' : 'Loading…'}
              </dd>
              <dt class="text-slate-500">DHCP</dt>
              <dd class="text-slate-300">
                {#if liveDhcp?.enabled}
                  On{liveDhcp.start && liveDhcp.end ? ` · ${liveDhcp.start} – ${liveDhcp.end}` : ''}
                {:else}
                  {liveInfo ? 'Off' : liveInfoError ? 'Unavailable' : 'Loading…'}
                {/if}
              </dd>
            </dl>
          </div>
        {/if}

        {#if type === 'shared_network'}
          <div class="space-y-3 rounded-xl border border-slate-800 bg-slate-900/30 p-3">
            <div class="text-xs text-slate-400">
              Nodes attached here share one layer 2 segment with nodes in the other lab.
            </div>
            <label class="block space-y-1.5">
              <span class="text-[10px] uppercase tracking-[0.05em] text-slate-500">Network</span>
              {#if sharedLoading}
                <div class="rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-400">
                  Loading shared networks…
                </div>
              {:else if sharedError}
                <div class="rounded-xl border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-200">
                  Failed to list shared networks: {sharedError}
                </div>
              {:else if sharedNetworks.length === 0}
                <div class="rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-xs text-slate-400">
                  No other lab offers a network this lab can join. Right-click a network in that lab and choose
                  <strong>Make shared</strong>.
                </div>
              {:else}
                <select
                  bind:value={selectedSharedId}
                  class="w-full rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 outline-none transition focus:border-blue-500/40 focus:ring-1 focus:ring-blue-500/30"
                  data-testid="network-shared-select"
                >
                  {#each sharedNetworks as entry}
                    <option value={entry.id}>
                      {entry.lab_name} / {entry.name}{entry.cidr ? ` (${entry.cidr})` : ''}
                    </option>
                  {/each}
                </select>
              {/if}
            </label>
          </div>
        {/if}

        {#if type === 'bridge_cloud'}
          <div class="space-y-3 rounded-xl border border-amber-500/30 bg-amber-500/5 p-3">
            <div class="text-xs text-amber-200">
              Lab nodes attached to a Bridge-Cloud network bridge directly onto
              the host's physical LAN.  Do <strong>not</strong> run
              unauthorized DHCP servers on those nodes — they will leak onto
              the upstream network.
            </div>
            <label class="block space-y-1.5">
              <span class="text-[10px] uppercase tracking-[0.05em] text-slate-500">Host bridge</span>
              {#if hostBridgesLoading}
                <div class="rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-400">
                  Loading host bridges…
                </div>
              {:else if hostBridgesError}
                <div class="rounded-xl border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-200">
                  Failed to list host bridges: {hostBridgesError}
                </div>
              {:else if hostBridges.length === 0}
                <div class="rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-xs text-slate-400">
                  No <code>br-eth*</code> bridges found.  Run
                  <code>deploy/scripts/provision-ubuntu-2604.sh</code> on the
                  host first.
                </div>
              {:else}
                <select
                  bind:value={selectedHostBridge}
                  class="w-full rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 outline-none transition focus:border-blue-500/40 focus:ring-1 focus:ring-blue-500/30"
                  data-testid="network-host-bridge-select"
                >
                  {#each hostBridges as bridge}
                    <option value={bridge.host_bridge}>
                      {bridge.label}
                      {#if bridge.addrs.length > 0}
                        ({bridge.addrs.join(', ')})
                      {/if}
                      {bridge.carrier ? '' : '(link down)'}
                    </option>
                  {/each}
                </select>
              {/if}
            </label>
          </div>
        {/if}
        {#if formMessage}
          <p class="rounded-xl border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-200" role="alert">
            {formMessage}
          </p>
        {/if}
      </div>

      <div class="flex justify-end gap-3 border-t border-slate-800 bg-slate-950/80 px-5 py-4">
        <button
          type="button"
          class="rounded-full border border-slate-800 px-4 py-2 text-sm text-slate-300 transition hover:border-blue-500/40 hover:text-white disabled:cursor-not-allowed disabled:opacity-50"
          disabled={saving}
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
          aria-busy={busy}
        >
          {saving ? (editing ? 'Saving…' : 'Creating…') : checking ? 'Validating…' : submitLabel}
        </button>
      </div>
    </div>
  </div>
{/if}
