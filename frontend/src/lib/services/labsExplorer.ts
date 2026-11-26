// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

// Pure helpers behind the file-explorer style Labs view (#322).

import type { FolderEntry, FolderListing, LabListItem } from '$lib/types';

export const SHARED_OWNER = '*';
export const SYSTEM_OWNER = 'system';
/** Virtual root folder that lists every lab with live nodes (#365). */
export const RUNNING_FOLDER = '/Running';

export interface ExplorerNode {
  name: string;
  path: string; // '' for the root
  owner: string;
  mtime?: string;
  /** Live nodes in this folder's subtree, direct labs included (#365). */
  running: number;
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

function byDisplayName(a: LabListItem, b: LabListItem): number {
  return labDisplayName(a).localeCompare(labDisplayName(b), undefined, { sensitivity: 'base' });
}

function isRunning(lab: LabListItem): boolean {
  return (lab.nodes_running ?? 0) > 0;
}

/** Labs with live nodes anywhere below `node` (#365). The Running folder is
 * skipped: it only re-lists what the real folders already hold. */
export function runningLabs(node: ExplorerNode): LabListItem[] {
  const own = node.labs.filter(isRunning);
  return node.folders.reduce(
    (all, child) => (child.path === RUNNING_FOLDER ? all : all.concat(runningLabs(child))),
    own
  );
}

function runningNodes(labs: LabListItem[]): number {
  return labs.reduce((sum, lab) => sum + (lab.nodes_running ?? 0), 0);
}

function tallyRunning(node: ExplorerNode): number {
  node.running = runningNodes(node.labs);
  for (const child of node.folders) {
    if (child.path !== RUNNING_FOLDER) node.running += tallyRunning(child);
  }
  return node.running;
}

/** Build a folder tree from the flat recursive `/folders/` listing. Labs that
 * live in no folder end up on the root node. */
export function buildTree(listing: FolderListing): ExplorerNode {
  const root: ExplorerNode = { name: 'Labs', path: '', owner: SYSTEM_OWNER, running: 0, folders: [], labs: [] };
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
      running: 0,
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
    node.labs.sort(byDisplayName);
  }
  tallyRunning(root);
  // The Running folder is a view, not a location (#365): the same lab
  // entries re-listed, so opening one still resolves to its real path.
  const running = index.get(RUNNING_FOLDER);
  if (running) {
    running.labs = runningLabs(root).sort(byDisplayName);
    running.running = runningNodes(running.labs);
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
