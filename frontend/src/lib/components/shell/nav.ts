// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

/**
 * Left-navigation model for the app shell (#260).
 *
 * Static preview pages carry a `plannedIssue` reference — the row renders a
 * "planned" badge and links to the tracking issue until the real view lands.
 */
import {
  Activity,
  Boxes,
  FlaskConical,
  Gauge,
  HardDrive,
  ScrollText,
  Settings,
  Users,
} from 'lucide-svelte';

/** Icon constructor type (mirrors the `as const` map pattern in CustomNode). */
export type NavIcon = typeof FlaskConical;

export interface NavItem {
  label: string;
  href: string;
  icon: NavIcon;
  /** Tracking issue for views that are static previews so far. */
  plannedIssue?: number;
}

export interface NavSection {
  title: string | null;
  adminOnly: boolean;
  items: NavItem[];
}

export const NAV_SECTIONS: readonly NavSection[] = [
  {
    // Labs grouping (#260): the browser plus lab-instance management sit
    // under their own superheading instead of an un-sectioned first entry.
    title: 'Labs',
    adminOnly: false,
    items: [
      { label: 'Labs', href: '/labs', icon: FlaskConical },
      { label: 'Lab Management', href: '/manage/labs', icon: Activity, plannedIssue: 262 },
    ],
  },
  {
    title: 'Management',
    adminOnly: true,
    items: [
      { label: 'Users', href: '/manage/users', icon: Users, plannedIssue: 261 },
      { label: 'Running Nodes', href: '/manage/nodes', icon: Boxes, plannedIssue: 263 },
    ],
  },
  {
    title: 'Library',
    adminOnly: true,
    items: [
      {
        label: 'Images & Templates',
        href: '/library/images',
        icon: HardDrive,
        plannedIssue: 264,
      },
    ],
  },
  {
    title: 'System',
    adminOnly: true,
    items: [
      { label: 'Status', href: '/system/status', icon: Gauge, plannedIssue: 265 },
      { label: 'Logs', href: '/system/logs', icon: ScrollText, plannedIssue: 265 },
      { label: 'Settings', href: '/system/settings', icon: Settings, plannedIssue: 265 },
    ],
  },
];

/**
 * Active-link test: an item is active when the pathname equals its href or
 * lives underneath it (editor-deep routes keep the Labs entry lit).
 */
export function isNavActive(pathname: string, href: string): boolean {
  if (href === '/labs') {
    return pathname === '/labs' || pathname.startsWith('/labs/');
  }
  return pathname === href || pathname.startsWith(`${href}/`);
}
