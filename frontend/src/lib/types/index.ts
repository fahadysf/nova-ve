export interface ApiResponse<T> {
  code: number;
  status: 'success' | 'fail' | 'error' | 'unauthorized';
  message: string;
  data: T;
  /** #245: post-write concurrency revision on guarded PUT responses. */
  revision?: number;
  access_token?: string;
  token_type?: string;
  eve_uid?: string;
  eve_expire?: string;
}

export type UserRole = 'admin' | 'editor' | 'user';

export interface UserRead {
  username: string;
  email?: string | null;
  name?: string | null;
  role: UserRole;
  html5?: boolean;
  extauth?: string | null;
  online?: number | boolean;
  /** Source address of the most recent successful sign-in (#297). */
  ip?: string | null;
  lab?: string | null;
  /** Quotas (#261): -1 = unlimited. RAM in MB. */
  cpu?: number;
  ram?: number;
  sat?: number;
  /** Account lifecycle epoch seconds; -1 = unset. */
  expiration?: number;
  datestart?: number;
  diskusage?: number;
  /** Tenant folder root (#261): non-admins are scoped to this lab root. */
  folder?: string | null;
  /** Tenant (pod) id. */
  pod?: number;
  /** Epoch seconds of the last successful sign-in (#297); absent or null = never. */
  last_login?: number | null;
}

// A type alias (not an interface) so it satisfies Record<string, unknown>
// for apiRequest bodies without call-site spreads.
export type UserWritePayload = {
  email?: string;
  name?: string;
  role?: UserRole;
  html5?: boolean;
  password?: string;
  cpu?: number;
  ram?: number;
  sat?: number;
  expiration?: number;
  datestart?: number;
  folder?: string;
  pod?: number;
};

export type AuthLoginResponse = UserRead;

// --- Image & template library (#264) --------------------------------------

export interface LibraryImageRef {
  lab: string;
  lab_path: string;
  lab_name: string;
  node: string;
  template: string;
}

export interface LibraryImage {
  name: string;
  filename: string;
  type: string;
  path: string;
  files: string[];
  size: number | null;
  mtime: number | null;
  mtime_iso: string | null;
  sha256: string | null;
  state: string;
  referenced_by: LibraryImageRef[];
  templates: { type: string; key: string; name: string }[];
}

export interface LibraryTemplateImage {
  name: string;
  filename: string;
  state: string;
  size: number | null;
}

export type TemplateInterfaceNaming = { format: string } | { explicit: string[] };
export type LibraryTemplateType = 'qemu' | 'docker' | 'iol' | 'dynamips';
export type LibraryDiskType = Exclude<LibraryTemplateType, 'docker'>;

export interface LibraryDiskUpload {
  type: LibraryDiskType;
  imageName: string;
  file: File;
}

export interface LibraryTemplate {
  type: string;
  key: string;
  source: 'builtin' | 'user' | 'override';
  name: string;
  description: string;
  icon_type: string;
  cpu: number;
  ram: number;
  ethernet: number;
  console_type: string;
  second_console?: string | null;
  cpulimit: number;
  image_aliases: string[];
  interface_naming?: TemplateInterfaceNaming | null;
  capabilities?: TemplateCapabilities;
  extras?: Record<string, unknown>;
  extras_schema?: NodeCatalogExtraField[];
  paired_parent?: string | null;
  images: LibraryTemplateImage[];
}

export type LibraryTemplateEdit = {
  name?: string;
  description?: string;
  icon_type?: string;
  cpu?: number;
  ram?: number;
  ethernet?: number;
  cpulimit?: number;
  console_type?: string;
  second_console?: string | null;
  image_aliases?: string[];
  interface_naming?: TemplateInterfaceNaming | null;
  capabilities?: Partial<TemplateCapabilities>;
  extras?: Record<string, unknown>;
};

export type LibraryTemplateCreate = LibraryTemplateEdit & {
  key: string;
  type: LibraryTemplateType;
  name: string;
};

export interface FolderEntry {
  name: string;
  path: string;
  mtime?: string;
  umtime?: number;
  spy?: number;
  lock?: boolean;
  shared?: number;
  /** Username, '*' (all users) or 'system' (#322). */
  owner?: string;
}

export interface LabListItem {
  file: string;
  path: string;
  mtime: string;
  umtime?: number;
  spy?: number;
  lock?: boolean;
  shared?: number;
  owner?: string;
  /** Live nodes in this lab, from the runtime registry (#365). */
  nodes_running?: number;
}

export interface FolderListing {
  folders: FolderEntry[];
  labs: LabListItem[];
}

// --- v2 schema (US-058) ---------------------------------------------------

export type PortSide = 'top' | 'right' | 'bottom' | 'left';

export interface PortPosition {
  side: PortSide;
  /** 0..1 along the chosen side. */
  offset: number;
}

export interface NodeInterface {
  /** v2 fields. ``index`` and ``name`` are always present on the wire. */
  index?: number;
  name: string;
  planned_mac?: string | null;
  /**
   * Legacy manual placement from the removed ctrl+drag feature (#258).
   * Read for lab-JSON compat only — the canvas never honors it; connection
   * points are auto-positioned (facing side + min-gap spread).
   * @deprecated Auto-positioning is the only source of truth.
   */
  port_position?: PortPosition | null;
  /**
   * Synthesised legacy field — populated by the loader from links[]. Mutations
   * must go through links[]; this is a read-only compat shim retained until
   * the canvas migrates to v2 links.
   * @deprecated Use links[] for the source of truth.
   */
  network_id: number;
}

/**
 * Live-MAC tooltip state (US-072). Mirrors the backend ``read_live_mac`` payload.
 * Keys: ``${nodeId}:${interfaceIndex}`` in the WS-driven liveMacs store.
 */
export type LiveMacState = {
  state: 'confirmed' | 'mismatch' | 'unavailable';
  planned_mac: string;
  live_mac?: string;
  reason?: string;
  runtime_type?: 'qemu' | 'docker' | 'iol' | 'dynamips';
};

export type NetworkType =
  | 'linux_bridge'
  | 'ovs_bridge'
  | 'nat'
  | 'nat_cloud'
  | 'bridge_cloud'
  | 'cloud'
  | 'management'
  | 'pnet0' | 'pnet1' | 'pnet2' | 'pnet3' | 'pnet4'
  | 'pnet5' | 'pnet6' | 'pnet7' | 'pnet8' | 'pnet9'
  | 'internal' | 'internal2' | 'internal3'
  | 'private' | 'private2' | 'private3'
  | 'nat0';

export type LinkStyle = 'orthogonal' | 'bezier' | 'straight';

/**
 * v2 link endpoint. Either ``node_id`` + ``interface_index`` (node-side) or
 * ``network_id`` (network-side). The fields are optional individually so the
 * same shape covers both endpoint kinds; consumers must check which keys are
 * present at runtime (US-082).
 */
export interface LinkEndpoint {
  node_id?: number;
  interface_index?: number;
  network_id?: number;
}

export interface LinkEndpointPosition {
  mode: 'auto' | 'manual';
  position?: PortPosition | null;
}

export interface LinkEndpointPositions {
  from?: LinkEndpointPosition;
  to?: LinkEndpointPosition;
}

export interface LinkMetrics {
  delay_ms?: number;
  loss_pct?: number;
  bandwidth_kbps?: number;
  jitter_ms?: number;
}

export interface Link {
  id: string;
  from: LinkEndpoint;
  to: LinkEndpoint;
  style_override?: LinkStyle | null;
  /**
   * Legacy manual placements from the removed ctrl+drag feature (#258).
   * Read for lab-JSON compat only — the canvas ignores them and
   * auto-positions every connection point.
   * @deprecated Auto-positioning is the only source of truth.
   */
  endpoint_positions?: LinkEndpointPositions;
  label?: string;
  color?: string;
  width?: string;
  metrics?: LinkMetrics;
}

/**
 * Reconciliation state for a declared link, surfaced by the backend discovery
 * loop and drawn as a red overlay on the canvas (US-404).  This is
 * observation-only runtime state — it is never persisted to ``links[]`` or
 * ``lab.json``.  Keyed ``'link:<link_id>'``.
 */
export type LinkReconciliationKind = 'divergent';

export interface LinkReconciliation {
  kind: LinkReconciliationKind;
  /** Stable map key. */
  key: string;
  link_id?: string;
  /** ISO 8601 from backend ``link_divergent`` event. */
  last_checked?: string;
  reason?: string;
}

export interface Network {
  id: number;
  name: string;
  type: NetworkType;
  left: number;
  top: number;
  icon: string;
  width: number;
  style: string;
  linkstyle: string;
  color: string;
  label: string;
  visibility: boolean;
  implicit: boolean;
  smart: number;
  config: Record<string, unknown>;
  /** Derived from links[] on read; never persisted. */
  count?: number;
}

export interface NetworkCreateConfig {
  cidr?: string;
  gateway?: string;
  dhcp?: boolean;
  /** linux_bridge only (#325): the host's address on the bridge. */
  dhcp_server?: string;
  dhcp_start?: string;
  dhcp_end?: string;
  egress_interface?: string;
  /** NAT-Cloud only — ID of an existing owner returned by /api/system/clouds. */
  shared_cloud_id?: string;
  /** ID from /api/system/shared-networks — joins another lab's network (#319). */
  shared_network_id?: string;
  /** Bridge-Cloud only — name of the host-owned br-eth* bridge. */
  host_bridge?: string;
}

export interface LabViewport {
  x: number;
  y: number;
  zoom: number;
}

export interface LabDefaults {
  link_style: LinkStyle;
}

export interface LabData {
  schema: 2;
  id: string;
  meta: LabMeta;
  viewport: LabViewport;
  nodes: Record<string, NodeData>;
  networks: Record<string, Network>;
  links: Link[];
  defaults: LabDefaults;
  textobjects?: unknown[];
  lineobjects?: unknown[];
  pictures?: unknown[];
  tasks?: unknown[];
  configsets?: Record<string, unknown>;
}

// --- end v2 schema --------------------------------------------------------

export interface NodeData {
  id: number;
  name: string;
  type: 'qemu' | 'docker' | 'iol' | 'dynamips';
  template: string;
  image: string;
  console: 'telnet' | 'vnc' | 'rdp' | 'serial';
  /** Optional second console mode declared on the template (#268). */
  secondConsole?: 'telnet' | 'vnc' | 'rdp' | null;
  status: 0 | 2;
  transientStatus?: 'starting' | 'stopping';
  delay: number;
  cpu: number;
  ram: number;
  ethernet: number;
  left: number;
  top: number;
  icon: string;
  interfaces: NodeInterface[];
  width?: string;
  uuid?: string;
  firstmac?: string;
  cpulimit?: number;
  url?: string;
  cpu_usage?: number;
  ram_usage?: number;
  extras?: Record<string, unknown>;
  /** Optional runtime/template capabilities echoed by newer backend responses. */
  capabilities?: TemplateCapabilities;
  interface_naming_scheme?: string | null;
}

export interface NodeCatalogImage {
  image: string;
  files?: string[];
  path?: string;
  source?: string;
  vnc_port?: number;
}

export interface NodeCatalogDefaults {
  type: 'qemu' | 'docker' | 'iol' | 'dynamips';
  template: string;
  image: string;
  icon_type: string;
  icon: string;
  cpu: number;
  ram: number;
  ethernet: number;
  console_type: 'telnet' | 'vnc' | 'rdp' | 'serial';
  second_console?: string | null;
  delay: number;
  cpulimit: number;
  extras?: Record<string, unknown>;
}

export interface NodeCatalogExtraFieldOption {
  value: string;
  label?: string;
  ethernetPorts?: number;
}

export interface NodeCatalogExtraField {
  key: string;
  label: string;
  type: 'text' | 'number' | 'select' | 'textarea' | 'env' | 'readonly';
  default?: unknown;
  options?: NodeCatalogExtraFieldOption[];
  placeholder?: string;
  description?: string;
  stoppedOnly?: boolean;
  runtime?: boolean;
}

export interface TemplateCapabilities {
  hotplug: boolean;
  max_nics: number;
  /** QEMU machine type (q35 or pc). null for non-QEMU templates. */
  machine: 'q35' | 'pc' | null;
}

export interface NodeCatalogTemplate {
  key: string;
  type: 'qemu' | 'docker' | 'iol' | 'dynamips';
  name: string;
  description: string;
  defaults: NodeCatalogDefaults;
  images: NodeCatalogImage[];
  icon_options: string[];
  extras_schema?: NodeCatalogExtraField[];
  capabilities?: TemplateCapabilities;
  /**
   * #206 — when set, this template entry was synthesized from a paired
   * template's child block (key shape ``<paired_parent>__<child_id>``).
   * Frontends should hide these from the standalone create-flow type-tab
   * picker but still resolve them by key for edit-mode + capability lookups.
   */
  paired_parent?: string | null;
}

export interface NodeCatalogPairedChild {
  id: string;
  name: string;
  kind: string;
  image: string;
  cpu: number;
  ram: number;
  ethernet: number;
  /** #206 — synthetic template key (`<paired_parent>__<child_id>`) the child
   *  node carries on its `template` field once instantiated. Lets callers
   *  cross-reference the matching catalog entry for capabilities / extras. */
  template_key?: string;
}

export interface NodeCatalogPairedLink {
  from_node: string;
  from_iface: string;
  to_node: string;
  to_iface: string;
}

export interface NodeCatalogPairedTemplate {
  key: string;
  name: string;
  vendor: string;
  child_count: number;
  link_count: number;
  children: NodeCatalogPairedChild[];
  links: NodeCatalogPairedLink[];
  /** #207 — false when the template fails pre-flight (link references an
   *  interface name no child will expose, e.g. pre-#202 imports without
   *  ``interface_naming.explicit``). Endpoint returns 422 in that case. */
  valid?: boolean;
  /** #207 — human-readable reason when valid is false. */
  invalid_reason?: string | null;
}

export interface NodeCatalog {
  pending?: boolean;
  templates: NodeCatalogTemplate[];
  paired_templates?: NodeCatalogPairedTemplate[];
  dynamips_profiles?: Record<string, NodeCatalogExtraField[]>;
  icon_options: string[];
  icon_assets?: string[];
  create_fields: string[];
  edit_fields: string[];
  runtime_editability: {
    always: string[];
    stopped_only: string[];
    immutable: string[];
  };
}

export interface NodeBatchCreateResult {
  nodes: NodeData[];
}

/**
 * @deprecated v1 alias retained for the existing canvas implementation. Use
 * {@link Network} for the v2 schema. Will be removed once the canvas migrates
 * to links[].
 */
export interface NetworkData {
  id: number;
  name: string;
  type: string;
  left: number;
  top: number;
  icon: string;
  visibility: boolean | number;
  count?: number;
  style?: string;
  linkstyle?: string;
  color?: string;
  label?: string;
  smart?: number;
  width?: number;
  implicit?: boolean;
  /** Offered to other labs (#319). */
  shared?: boolean;
  config?: Record<string, unknown>;
  runtime?: Record<string, unknown>;
}

/** GET /system/shared-networks entry (#319). */
export interface SharedNetwork {
  id: string;
  type: NetworkType;
  name: string;
  lab_id: string;
  lab_name: string;
  cidr: string | null;
}

/** GET /labs/{path}/networks/{id}/info (#318). */
export interface NetworkInfo {
  id: number;
  name: string;
  type: string;
  type_label: string;
  visible: boolean;
  created_at: string | null;
  bridge: {
    name: string | null;
    driver: string | null;
    exists: boolean;
    admin_up: boolean | null;
    oper_state: string | null;
    mac: string | null;
    mtu: number | null;
    ports: number | null;
  };
  host_bridge: string | null;
  ip: {
    cidr: string;
    gateway: string | null;
    egress_interface: string | null;
    nat: boolean;
    static_start: string | null;
    static_end: string | null;
    used_ips: string[];
    dhcp: {
      enabled: boolean;
      /** Address the DHCP server answers from; bridges only (#325). */
      server?: string | null;
      start: string | null;
      end: string | null;
      size: number | null;
      lease_time: string | null;
      running: boolean;
      pid: number | null;
      lease_count: number;
      leases: Array<{
        ip: string;
        mac: string;
        hostname: string | null;
        node_name: string | null;
        expires_at: string | null;
      }>;
    } | null;
  } | null;
  endpoints: Array<{
    lab_name: string;
    current_lab: boolean;
    node_id: number;
    node_name: string;
    interface: string;
    mac: string | null;
  }>;
  mac_conflicts: Array<{ mac: string; endpoints: string[] }>;
  sharing: {
    role: 'none' | 'owner' | 'member';
    shareable: boolean;
    reason: string | null;
    owner_lab: string | null;
    owner_missing: boolean;
    labs: Array<{
      lab_name: string;
      lab_path: string | null;
      network_name: string;
      role: 'owner' | 'member';
      current_lab: boolean;
      connections: number;
    }>;
  };
}

/**
 * @deprecated v1 ``topology[]`` shape. The v2 source of truth is {@link Link};
 * this interface is retained only as a read-side compat shim while the canvas
 * still consumes ``/api/labs/{path}/topology``. Wave 1 will retire it.
 */
export interface TopologyLink {
  type: 'ethernet';
  source: string;
  source_node_name?: string;
  source_type: 'node' | 'network';
  source_label: string;
  source_interfaceId: number;
  source_suspend?: number;
  destination: string;
  destination_type: 'node' | 'network';
  destination_node_name?: string;
  destination_label: string;
  destination_interfaceId: number | string;
  destination_suspend?: number;
  network_id: number;
  style?: string;
  linkstyle?: string;
  label?: string;
  labelpos?: string;
  width?: string;
  color?: string;
  stub?: string;
  curviness?: string;
  beziercurviness?: string;
  round?: string;
  midpoint?: string;
  srcpos?: string;
  dstpos?: string;
  source_delay?: number;
  source_loss?: number;
  source_bandwidth?: number;
  source_jitter?: number;
  destination_delay?: number;
  destination_loss?: number;
  destination_bandwidth?: number;
  destination_jitter?: number;
}

export interface LabMeta {
  id: string;
  name: string;
  filename: string;
  path: string;
  owner: string;
  author: string;
  description: string;
  version: string;
  /**
   * #245: machine-owned optimistic-concurrency revision, bumped by the
   * backend on every lab.json write. Send as If-Match on PUT /layout and
   * PUT /topology; distinct from the user-authored `version` field.
   */
  revision?: number;
  defaults?: Partial<LabDefaults>;
  grid: boolean;
  lock: boolean;
  sat?: string;
  shared?: string[];
}
