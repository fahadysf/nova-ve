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
  path: string;
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

export interface NodeData {
  id: number;
  name: string;
  type: 'qemu' | 'docker' | 'iol' | 'dynamips';
  template: string;
  image: string;
  console: 'telnet' | 'vnc' | 'rdp';
  status: 0 | 2;
  cpu: number;
  ram: number;
  ethernet: number;
  left: number;
  top: number;
  icon: string;
  width?: string;
  uuid?: string;
  firstmac?: string;
  url?: string;
  cpu_usage?: number;
  ram_usage?: number;
}

export interface NetworkData {
  id: number;
  name: string;
  type: string;
  left: number;
  top: number;
  icon: string;
  visibility: boolean;
  width?: number;
}

export interface TopologyLink {
  type: 'ethernet';
  source: string;
  source_type: 'node' | 'network';
  source_label: string;
  source_interfaceId: number;
  destination: string;
  destination_type: 'node' | 'network';
  destination_label: string;
  destination_interfaceId: number | string;
  network_id: number;
  width?: string;
  color?: string;
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
