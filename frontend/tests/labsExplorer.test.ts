// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from 'vitest';
import {
  RUNNING_FOLDER,
  buildTree,
  crumbs,
  findNode,
  ownerLabel,
  parentPath,
  runningLabs
} from '$lib/services/labsExplorer';

const lab = (path: string, owner?: string) => ({ file: path.split('/').pop()!, path, mtime: 'now', owner });

describe('labs explorer tree (#322)', () => {
  const tree = buildTree({
    folders: [
      { name: 'Shared', path: '/Shared', owner: '*' },
      { name: 'alice', path: '/alice', owner: 'alice' },
      { name: 'sub', path: '/alice/sub' }
    ],
    labs: [lab('/root.json', 'admin'), lab('/alice/a.json', 'alice'), lab('/alice/sub/deep.json'), lab('/Shared/team.json', 'bob')]
  });

  it('puts folderless labs at the root', () => {
    expect(tree.labs.map((l) => l.file)).toEqual(['root.json']);
    expect(tree.folders.map((f) => f.name)).toEqual(['alice', 'Shared']);
  });

  it('nests folders and inherits the parent owner', () => {
    expect(findNode(tree, '/alice/sub')?.owner).toBe('alice');
    expect(findNode(tree, '/alice/sub')?.labs).toHaveLength(1);
  });

  it('creates missing intermediate folders', () => {
    const sparse = buildTree({ folders: [], labs: [lab('/x/y/z.json')] });
    expect(findNode(sparse, '/x/y')?.labs).toHaveLength(1);
  });

  it('labels owners', () => {
    expect(ownerLabel('*')).toBe('All users');
    expect(ownerLabel('system')).toBe('System');
    expect(ownerLabel('alice')).toBe('alice');
  });

  it('computes parents and crumbs', () => {
    expect(parentPath('/a/b')).toBe('/a');
    expect(parentPath('/a')).toBe('');
    expect(crumbs('/a/b').map((c) => c.path)).toEqual(['', '/a', '/a/b']);
  });
});

describe('running labs in the explorer tree (#365)', () => {
  const running = (path: string, nodes_running: number) => ({ ...lab(path), nodes_running });
  const folders = [
    { name: 'Running', path: '/Running', owner: 'system' },
    { name: 'Shared', path: '/Shared', owner: '*' },
    { name: 'alice', path: '/alice', owner: 'alice' },
    { name: 'sub', path: '/alice/sub' }
  ];
  const tree = buildTree({
    folders,
    labs: [running('/root.json', 1), lab('/alice/idle.json'), running('/alice/sub/deep.json', 3), running('/Shared/team.json', 2)]
  });

  it('lists exactly the labs with live nodes under Running, at their real paths', () => {
    const runningFolder = findNode(tree, RUNNING_FOLDER)!;
    expect(runningFolder.labs.map((l) => l.path)).toEqual(['/alice/sub/deep.json', '/root.json', '/Shared/team.json']);
    // The same entry, not a copy: opening it resolves to where the lab lives.
    expect(runningFolder.labs[0]).toBe(findNode(tree, '/alice/sub')!.labs[0]);
    expect(runningFolder.running).toBe(6);
  });

  it('totals live nodes up the tree without counting Running twice', () => {
    expect(findNode(tree, '/alice/sub')?.running).toBe(3);
    expect(findNode(tree, '/alice')?.running).toBe(3); // nested counts; the idle lab adds nothing
    expect(findNode(tree, '/Shared')?.running).toBe(2);
    expect(tree.running).toBe(6);
    expect(runningLabs(tree).map((l) => l.path)).toHaveLength(3);
  });

  it('is empty once every node is down', () => {
    const stopped = buildTree({ folders, labs: [running('/root.json', 0), lab('/alice/idle.json')] });
    expect(findNode(stopped, RUNNING_FOLDER)?.labs).toEqual([]);
    expect([stopped, ...stopped.folders].every((node) => node.running === 0)).toBe(true);
  });

  it('changes nothing for listings without a Running folder (folder-scoped users)', () => {
    const scoped = buildTree({ folders: [{ name: 'Shared', path: '/Shared' }], labs: [running('/alice/own.json', 1)] });
    expect(findNode(scoped, RUNNING_FOLDER)).toBeNull();
    expect(runningLabs(scoped).map((l) => l.path)).toEqual(['/alice/own.json']);
  });
});
