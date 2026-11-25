// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

// Pure helpers behind the file-explorer style Labs view (#322).

import type { FolderEntry, FolderListing, LabListItem } from '$lib/types';

export const SHARED_OWNER = '*';
export const SYSTEM_OWNER = 'system';

export interface ExplorerNode {
  name: string;
  path: string; // '' for the root
  owner: string;
  mtime?: string;
  folders: ExplorerNode[];
  labs: LabListItem[];
}

export function normalizePath(path: string): string {
  const cleaned = path.trim().replace(/\\/g, '/').replace(/^\/+|\/+$/g, '');
  return cleaned ? `/${cleaned}` : '';
}

export function parentPath(path: string): string {
  const parts = normalizePath(path).split('/').filter(Boolean);
  return parts.length > 1 ? `/${parts.slice(0, -1).join('/')}` : '';
}

export function ownerLabel(owner: string | undefined): string {
  if (owner === SHARED_OWNER) return 'All users';
  if (owner === SYSTEM_OWNER) return 'System';
  return owner || '—';
}

export function labDisplayName(lab: LabListItem): string {
  return lab.file.replace(/\.(json|unl)$/, '');
}

function byName(a: { name: string }, b: { name: string }): number {
  return a.name.localeCompare(b.name, undefined, { sensitivity: 'base' });
}

/** Build a folder tree from the flat recursive `/folders/` listing. Labs that
 * live in no folder end up on the root node. */
export function buildTree(listing: FolderListing): ExplorerNode {
  const root: ExplorerNode = { name: 'Labs', path: '', owner: SYSTEM_OWNER, folders: [], labs: [] };
  const index = new Map<string, ExplorerNode>([['', root]]);

  const ensure = (path: string, entry?: FolderEntry): ExplorerNode => {
    const existing = index.get(path);
    if (existing) {
      if (entry) {
        existing.owner = entry.owner ?? existing.owner;
        existing.mtime = entry.mtime ?? existing.mtime;
      }
      return existing;
    }
    const parent = ensure(parentPath(path));
    const node: ExplorerNode = {
      name: path.split('/').filter(Boolean).pop() || path,
      path,
      owner: entry?.owner ?? parent.owner,
      mtime: entry?.mtime,
      folders: [],
      labs: []
    };
    index.set(path, node);
    parent.folders.push(node);
    return node;
  };

  for (const folder of listing.folders || []) ensure(normalizePath(folder.path), folder);
  for (const lab of listing.labs || []) {
    ensure(parentPath(lab.path)).labs.push({ ...lab, path: normalizePath(lab.path) });
  }

  for (const node of index.values()) {
    node.folders.sort(byName);
    node.labs.sort((a, b) => labDisplayName(a).localeCompare(labDisplayName(b), undefined, { sensitivity: 'base' }));
  }
  return root;
}

export function findNode(root: ExplorerNode, path: string): ExplorerNode | null {
  const target = normalizePath(path);
  if (!target) return root;
  let node: ExplorerNode | undefined = root;
  let walked = '';
  for (const segment of target.split('/').filter(Boolean)) {
    walked += `/${segment}`;
    node = node.folders.find((child) => child.path === walked);
    if (!node) return null;
  }
  return node;
}

/** Ancestor chain from the root to `path`, for breadcrumbs. */
export function crumbs(path: string): { name: string; path: string }[] {
  const out = [{ name: 'Labs', path: '' }];
  let walked = '';
  for (const segment of normalizePath(path).split('/').filter(Boolean)) {
    walked += `/${segment}`;
    out.push({ name: segment, path: walked });
  }
  return out;
}

export function countLabs(node: ExplorerNode): number {
  return node.labs.length + node.folders.reduce((sum, child) => sum + countLabs(child), 0);
}
