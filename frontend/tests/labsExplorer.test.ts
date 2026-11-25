// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from 'vitest';
import { buildTree, crumbs, findNode, ownerLabel, parentPath } from '$lib/services/labsExplorer';

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
