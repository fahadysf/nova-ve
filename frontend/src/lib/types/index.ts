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

export interface NodeInterface {
  name: string;
  network_id: number;
}

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

export interface NodeCatalogTemplate {
  key: string;
  type: 'qemu' | 'docker' | 'iol' | 'dynamips';
  name: string;
  description: string;
  defaults: NodeCatalogDefaults;
  images: NodeCatalogImage[];
  icon_options: string[];
  extras_schema?: NodeCatalogExtraField[];
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
}

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
