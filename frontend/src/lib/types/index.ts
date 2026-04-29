export interface ApiResponse<T> {
  code: number;
  status: 'success' | 'fail' | 'error' | 'unauthorized';
  message: string;
  data: T;
  access_token?: string;
  token_type?: string;
  eve_uid?: string;
  eve_expire?: string;
}

export interface UserRead {
  username: string;
  email?: string | null;
  name?: string | null;
  role: 'admin' | 'user';
  html5?: boolean;
  extauth?: string | null;
  online?: number | boolean;
  ip?: string | null;
  lab?: string | null;
}

export type AuthLoginResponse = UserRead;

export interface FolderEntry {
  name: string;
  path: string;
  mtime?: string;
  umtime?: number;
  spy?: number;
  lock?: boolean;
  shared?: number;
}

export interface LabListItem {
  file: string;
  path: string;
  mtime: string;
  umtime?: number;
  spy?: number;
  lock?: boolean;
  shared?: number;
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
  label?: string;
  color?: string;
  width?: string;
  metrics?: LinkMetrics;
}

/**
 * Reconciliation state for a single link or discovered iface, surfaced by the
 * backend discovery loop (US-402) and consumed as a unified visual overlay on
 * the canvas (US-403/US-404).  This is observation-only runtime state — it is
 * never persisted to ``links[]`` or ``lab.json``.
 *
 * Key scheme:
 *  - ``'iface:<iface>'``   for kind === 'discovered' (kernel-only iface)
 *  - ``'link:<link_id>'``  for kind === 'divergent'  (declared, no kernel veth/TAP)
 */
export type LinkReconciliationKind = 'discovered' | 'divergent';

export interface LinkReconciliation {
  kind: LinkReconciliationKind;
  /** Stable map key. */
  key: string;
  /** Present when kind === 'divergent'. */
  link_id?: string;
  /** Present when kind === 'discovered'. */
  iface?: string;
  network_id?: number;
  bridge_name?: string;
  peer_node_id?: number | null;
  peer_interface_index?: number | null;
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
  console: 'telnet' | 'vnc' | 'rdp';
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
  console_type: 'telnet' | 'vnc' | 'rdp';
  delay: number;
  cpulimit: number;
  extras?: Record<string, unknown>;
}

export interface NodeCatalogExtraFieldOption {
  value: string;
  label?: string;
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
}

export interface NodeCatalog {
  templates: NodeCatalogTemplate[];
  icon_options: string[];
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
  config?: Record<string, unknown>;
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
  grid: boolean;
  lock: boolean;
  sat?: string;
  shared?: string[];
}
