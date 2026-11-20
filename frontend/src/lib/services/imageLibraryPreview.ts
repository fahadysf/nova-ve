// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

/**
 * Static preview dataset for the Images & Templates library (#264).
 *
 * This is deliberately a typed mock: the page renders the full intended
 * information architecture (template ↔ image matching, checksums, lab
 * references) while the backend endpoints to serve it are still planned.
 * Field shapes mirror what the real services already model so wiring this
 * up later is a data-source swap, not a redesign.
 */

export type TemplateType = 'qemu' | 'docker' | 'iol' | 'dynamips';

export interface TemplatePreview {
  key: string;
  name: string;
  type: TemplateType;
  description: string;
  ramMb: number;
  ethernet: number;
  console: string;
  /** Glob-ish pattern an on-box image must satisfy to serve this template. */
  imagePattern: string;
}

export interface ImagePreview {
  id: string;
  filename: string;
  type: TemplateType;
  sizeBytes: number;
  sha256: string;
  modified: string;
  referencedByLabs: string[];
  state: 'available' | 'missing';
}

export const TEMPLATE_PREVIEWS: readonly TemplatePreview[] = [
  {
    key: 'veos',
    name: 'vEOS-QEMU',
    type: 'qemu',
    description: 'Arista vEOS 4.x QEMU guest (q35, virtio)',
    ramMb: 2048,
    ethernet: 4,
    console: 'telnet',
    imagePattern: 'veos-*.qcow2',
  },
  {
    key: 'vyos',
    name: 'VyOS',
    type: 'qemu',
    description: 'VyOS rolling QEMU guest',
    ramMb: 1024,
    ethernet: 2,
    console: 'telnet',
    imagePattern: 'vyos-*.qcow2',
  },
  {
    key: 'alpine',
    name: 'Alpine',
    type: 'docker',
    description: 'Alpine Linux container node',
    ramMb: 256,
    ethernet: 1,
    console: 'telnet',
    imagePattern: 'alpine:*',
  },
  {
    key: 'iol',
    name: 'IOL L2',
    type: 'iol',
    description: 'Cisco IOS on Linux L2 image',
    ramMb: 512,
    ethernet: 4,
    console: 'telnet',
    imagePattern: 'i86bi-linux-l2-*.bin',
  },
  {
    key: 'c7200',
    name: 'c7200',
    type: 'dynamips',
    description: 'Cisco 7200 router image',
    ramMb: 512,
    ethernet: 4,
    console: 'telnet',
    imagePattern: 'c7200-*.image',
  },
  {
    key: 'vqfx',
    name: 'vQFX',
    type: 'qemu',
    description: 'Juniper vQFX QEMU guest (no image present on this host)',
    ramMb: 4096,
    ethernet: 6,
    console: 'telnet',
    imagePattern: 'vqfx-*.qcow2',
  },
];

export const IMAGE_PREVIEWS: readonly ImagePreview[] = [
  {
    id: 'img-veos-431f',
    filename: 'veos-4.31.3F.qcow2',
    type: 'qemu',
    sizeBytes: 1_288_490_188,
    sha256: '3f2b9c41a7d8e6305c1f9ab27e4d8f15b6c3a0912e7f4d8b1c5a6e3f9d0b7c24',
    modified: '2026-11-14 09:22',
    referencedByLabs: ['/st-demo/ST-Demo.json', '/edge/arc-lab.json'],
    state: 'available',
  },
  {
    id: 'img-veos-empty',
    filename: 'veos-4.31.3F-empty.qcow2',
    type: 'qemu',
    sizeBytes: 197_632,
    sha256: '9a1c5e7b3d0f4a28e6c9b1d7f3a5e8c2b6d4f0a9e8c3b5d1f7a2e9c4b6d8f03',
    modified: '2026-11-14 09:24',
    referencedByLabs: [],
    state: 'available',
  },
  {
    id: 'img-vyos',
    filename: 'vyos-1.5-rolling.qcow2',
    type: 'qemu',
    sizeBytes: 889_192_448,
    sha256: 'c7e2a9401b5d6f8e3a1c9b7d5f0e2a4c8b6d1f3a9e7c5b0d2f4a8e6c1b3d9f75',
    modified: '2026-10-30 16:40',
    referencedByLabs: ['/edge/arc-lab.json'],
    state: 'available',
  },
  {
    id: 'img-alpine',
    filename: 'alpine:3.20',
    type: 'docker',
    sizeBytes: 7_864_320,
    sha256: 'e5b8d1a3f7c90e2b4d6f8a1c3e5b7d9f0a2c4e6b8d0f2a4c6e8b0d2f4a6c8e91',
    modified: '2026-11-02 11:05',
    referencedByLabs: ['/st-demo/ST-Demo.json'],
    state: 'available',
  },
  {
    id: 'img-iol-l2',
    filename: 'i86bi-linux-l2-adventerprisek9-ms.SSA.high_iron_20230710.bin',
    type: 'iol',
    sizeBytes: 150_994_944,
    sha256: 'a1f3e5d7c9b0f2a4e6c8d0b2f4a6e8c0d2b4f6a8e0c2d4b6f8a0e2c4d6b8f13',
    modified: '2026-09-18 13:12',
    referencedByLabs: ['/st-demo/ST-Demo.json', '/training/switch-lab.json'],
    state: 'available',
  },
  {
    id: 'img-c7200',
    filename: 'c7200-adventerprisek9-mz.152-4.S5.image',
    type: 'dynamips',
    sizeBytes: 264_241_152,
    sha256: 'b2a4c6e8d0f2a4c6e8b0d2f4a6c8e0d2b4f6a8c0e2d4b6f8a0c2e4d6f8b024',
    modified: '2026-08-05 10:47',
    referencedByLabs: ['/training/switch-lab.json'],
    state: 'missing',
  },
];

/** Glob-style match used by the preview (prefix* / *suffix / *infix*). */
export function imageMatchesPattern(filename: string, pattern: string): boolean {
  const star = pattern.indexOf('*');
  if (star < 0) return filename === pattern;
  const prefix = pattern.slice(0, star);
  const suffix = pattern.slice(star + 1);
  return filename.startsWith(prefix) && filename.endsWith(suffix);
}

export function matchesTemplateType(
  image: ImagePreview,
  type: TemplateType
): boolean {
  return image.type === type;
}
