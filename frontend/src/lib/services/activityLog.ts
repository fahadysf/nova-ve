/*
 * Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
 * SPDX-License-Identifier: Apache-2.0
 */

/** Activity log wording, change summaries and CSV export (#297). */

export type ActivityCategory =
  | 'auth'
  | 'user'
  | 'lab'
  | 'node'
  | 'network'
  | 'link'
  | 'folder'
  | 'console'
  | 'recording'
  | 'image'
  | 'template'
  | 'system';

/** One row of the server's activity trail, as `GET /system/logs/activity` returns it. */
export interface ActivityEntry {
  id: number;
  /** Epoch seconds (UTC). */
  time: number;
  /** Username; null when nobody is signed in (failed sign-in, first-user bootstrap). */
  actor: string | null;
  /** `<category>.<action>`, e.g. `node.start`. */
  event: string;
  category: ActivityCategory | string;
  action: string;
  outcome: 'success' | 'failure' | string;
  target_type: string | null;
  target_id: string | null;
  target_name: string | null;
  lab_path: string | null;
  lab_name: string | null;
  ip: string | null;
  detail: Record<string, unknown> | null;
}

export interface ActivityPage {
  entries: ActivityEntry[];
  total: number;
  limit: number;
  offset: number;
  actors: string[];
}

export interface ActivityFilters {
  q?: string;
  category?: string;
  action?: string;
  actor?: string;
  /** Epoch-second bounds (inclusive). */
  from?: number;
  to?: number;
}

/** The server caps a page at 1000 rows. */
export const ACTIVITY_MAX_PAGE_SIZE = 1000;
export const ACTIVITY_PAGE_SIZE = 100;
/** Upper bound on how many rows one CSV export will pull. */
export const ACTIVITY_EXPORT_CAP = 20000;

export const ACTIVITY_CATEGORIES: ReadonlyArray<{ value: string; label: string }> = [
  { value: 'auth', label: 'Authentication' },
  { value: 'user', label: 'Users' },
  { value: 'lab', label: 'Labs' },
  { value: 'node', label: 'Nodes' },
  { value: 'network', label: 'Networks' },
  { value: 'link', label: 'Links' },
  { value: 'folder', label: 'Folders' },
  { value: 'console', label: 'Consoles' },
  { value: 'recording', label: 'Recordings' },
  { value: 'image', label: 'Images' },
  { value: 'template', label: 'Templates' },
  { value: 'system', label: 'System' }
];

export const ACTIVITY_ACTIONS: ReadonlyArray<{ value: string; label: string }> = [
  { value: 'login', label: 'Sign-in' },
  { value: 'login_failed', label: 'Failed sign-in' },
  { value: 'logout', label: 'Sign-out' },
  { value: 'create', label: 'Created' },
  { value: 'update', label: 'Updated' },
  { value: 'delete', label: 'Deleted' },
  { value: 'start', label: 'Started' },
  { value: 'stop', label: 'Stopped' },
  { value: 'wipe', label: 'Wiped' },
  { value: 'move', label: 'Moved' },
  { value: 'rename', label: 'Renamed' },
  { value: 'open', label: 'Console opened' },
  { value: 'close', label: 'Console closed' },
  { value: 'mark', label: 'Marked' },
  { value: 'unmark', label: 'Unmarked' },
  { value: 'pull', label: 'Pulled' },
  { value: 'upload', label: 'Uploaded' },
  { value: 'calibrate', label: 'Calibrated' },
  { value: 'settings', label: 'Settings changed' },
  { value: 'iourc', label: 'License replaced' }
];

function text(value: unknown): string {
  return value === null || value === undefined ? '' : String(value);
}

function count(value: unknown): number {
  return Array.isArray(value) ? value.length : 0;
}

/** `YYYY-MM-DD HH:MM:SS` in the viewer's local time zone. */
export function formatActivityTime(epochSeconds: number): string {
  const pad = (value: number, length = 2) => String(value).padStart(length, '0');
  const date = new Date(epochSeconds * 1000);
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  );
}

/** The name shown for whatever the action was about. */
export function activityTarget(entry: ActivityEntry): string {
  return text(entry.target_name) || text(entry.target_id) || 'unknown';
}

function labOf(entry: ActivityEntry): string {
  return text(entry.lab_name) || text(entry.lab_path);
}

function fieldLabel(field: string): string {
  // An interface field arrives as "<interface>.planned_mac".
  const interfaceField = /^(.*)\.planned_mac$/.exec(field);
  return interfaceField ? `MAC address of ${interfaceField[1]}` : field;
}

/** `role: user → admin`, one line per changed field. */
export function summarizeChanges(changes: unknown): string[] {
  if (!changes || typeof changes !== 'object' || Array.isArray(changes)) return [];
  return Object.entries(changes as Record<string, unknown>)
    .filter(([, change]) => change && typeof change === 'object' && 'to' in (change as object))
    .map(([field, change]) => {
      const { from, to } = change as { from: unknown; to: unknown };
      const before = from === null || from === undefined || from === '' ? '—' : text(from);
      const after = to === null || to === undefined || to === '' ? '—' : text(to);
      return `${fieldLabel(field)}: ${before} → ${after}`;
    });
}

/** A span of seconds in the largest units that fit: `42s`, `14m`, `2h 05m`. */
export function formatDuration(seconds: unknown): string {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) return '';
  const total = Math.round(seconds);
  if (total < 60) return `${total}s`;
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (hours === 0) return `${minutes}m`;
  return `${hours}h ${String(minutes).padStart(2, '0')}m`;
}

/** "the console" or "the second console", per the event's console mode. */
function consoleWords(detail: Record<string, unknown>): string {
  return detail.console_mode === 'second' ? 'the second console' : 'the console';
}

function openFor(detail: Record<string, unknown>): string {
  const duration = formatDuration(detail.duration);
  return duration ? ` (open ${duration})` : '';
}

function lasting(detail: Record<string, unknown>): string {
  const duration = formatDuration(detail.duration);
  return duration ? ` (${duration})` : '';
}

function inBrackets(value: unknown): string {
  const label = text(value);
  return label ? ` (${label})` : '';
}

function imageKind(detail: Record<string, unknown>): string {
  return text(detail.image_type) || text(detail.source) || 'disk';
}

function joinParts(parts: string[]): string {
  return parts.filter((part) => part !== '').join(', ');
}

function fromAddress(ip: string | null): string {
  return ip ? ` from ${ip}` : '';
}

function inLab(entry: ActivityEntry): string {
  const lab = labOf(entry);
  return lab ? ` in lab ${lab}` : '';
}

function endpoints(detail: Record<string, unknown> | null): string {
  const from = text(detail?.from);
  const to = text(detail?.to);
  return from || to ? `${from || '?'} ↔ ${to || '?'}` : '';
}

/**
 * What happened, as one readable phrase — everything after the actor.
 * Unknown events (added by a newer backend) fall back to their raw parts
 * rather than disappearing from the view.
 */
export function activityPredicate(entry: ActivityEntry): string {
  const detail = entry.detail ?? {};
  const target = activityTarget(entry);
  const changes = summarizeChanges(detail.changes);
  const type = text(detail.node_type || detail.network_type);

  switch (entry.event) {
    case 'auth.login':
      return `signed in${fromAddress(entry.ip)}`;
    case 'auth.login_failed':
      return `failed to sign in as ${target}${fromAddress(entry.ip)}`;
    case 'auth.logout':
      return `signed out${fromAddress(entry.ip)}`;

    case 'user.create':
      if (detail.bootstrap) return `created the first administrator account ${target}`;
      return `created user ${target}${detail.role ? ` (role ${text(detail.role)})` : ''}`;
    case 'user.update': {
      const parts = [...changes];
      if (detail.password_changed) parts.push('password changed');
      return `updated user ${target}${parts.length ? `: ${joinParts(parts)}` : ''}`;
    }
    case 'user.delete':
      return `deleted user ${target}${detail.role ? ` (role ${text(detail.role)})` : ''}`;

    case 'lab.create':
      return `created lab ${target}`;
    case 'lab.update':
      return `updated lab ${target}${changes.length ? `: ${joinParts(changes)}` : ''}`;
    case 'lab.delete':
      return `deleted lab ${target}${detail.orphan ? ' (a file without a database row)' : ''}`;
    case 'lab.move':
      return `moved lab ${target} from ${text(detail.from)} to ${text(detail.to)}`;
    case 'lab.stop':
    case 'lab.wipe': {
      const verb = entry.event === 'lab.stop' ? 'stopped' : 'wiped';
      const acted = Math.max(count(detail.stopped), count(detail.wiped));
      const parts = [`${acted} node${acted === 1 ? '' : 's'}`];
      if (count(detail.failed)) parts.push(`${count(detail.failed)} failed`);
      return `${verb} lab ${target} (${joinParts(parts)})`;
    }

    case 'node.create':
      return `created node ${target}${type ? ` (${type})` : ''}${inLab(entry)}`;
    case 'node.update':
      return `updated node ${target}${changes.length ? `: ${joinParts(changes)}` : ''}${inLab(entry)}${detail.via === 'topology' ? ' through a topology save' : ''}`;
    case 'node.delete':
      return `deleted node ${target}${inLab(entry)}`;
    case 'node.start':
      return `started node ${target}${inLab(entry)}`;
    case 'node.stop':
      return `stopped node ${target}${inLab(entry)}`;
    case 'node.wipe':
      return `wiped node ${target}${inLab(entry)}`;

    case 'network.create':
      return `created network ${target}${type ? ` (${type})` : ''}${inLab(entry)}`;
    case 'network.update':
      return `updated network ${target}${changes.length ? `: ${joinParts(changes)}` : ''}${inLab(entry)}`;
    case 'network.delete':
      return `deleted network ${target}${inLab(entry)}`;

    case 'link.create':
      return `connected ${endpoints(detail)}${inLab(entry)}`;
    case 'link.update':
      return `updated link ${endpoints(detail)}${changes.length ? `: ${joinParts(changes)}` : ''}${inLab(entry)}`;
    case 'link.delete':
      return `disconnected ${endpoints(detail)}${inLab(entry)}`;

    case 'folder.create':
      return `created folder ${text(entry.target_id)}`;
    case 'folder.rename':
      return `renamed folder ${text(detail.from)} to ${text(detail.to)}`;
    case 'folder.delete':
      return `deleted folder ${text(entry.target_id)}`;

    case 'console.open': {
      const kind = inBrackets(detail.protocol);
      if (detail.via === 'external') return `launched an external console for ${target}${kind}${inLab(entry)}`;
      if (detail.via === 'native') return `downloaded a console launcher for ${target}${kind}${inLab(entry)}`;
      return `opened ${consoleWords(detail)} of ${target}${kind}${inLab(entry)}`;
    }
    case 'console.close':
      return `closed ${consoleWords(detail)} of ${target}${inLab(entry)}${openFor(detail)}`;

    case 'recording.start':
      return `started recording ${consoleWords(detail)} of ${target}${inLab(entry)}`;
    case 'recording.stop':
      return `stopped recording ${consoleWords(detail)} of ${target}${inLab(entry)}${lasting(detail)}`;
    case 'recording.delete': {
      const recordedBy = text(detail.recorded_by);
      // Who made the recording matters when it is someone other than the
      // person deleting it.
      const facts = [
        formatDuration(detail.duration) ? `lasting ${formatDuration(detail.duration)}` : '',
        recordedBy && recordedBy !== activityActor(entry) ? `recorded by ${recordedBy}` : ''
      ].filter(Boolean);
      return `deleted a console recording of ${target}${inLab(entry)}${facts.length ? ` (${joinParts(facts)})` : ''}`;
    }

    case 'image.delete':
      return `deleted the ${imageKind(detail)} image ${target}`;
    case 'image.upload':
      return `uploaded the ${imageKind(detail)} image ${target} for the ${text(detail.template)} template`;
    case 'image.pull':
      return `pulled the docker image ${target}`;
    case 'image.mark':
      return `made the docker image ${target} available to labs`;
    case 'image.unmark':
      return `removed the docker image ${target} from lab use`;
    case 'image.calibrate': {
      const idlePc = text(detail.idle_pc);
      const seconds = typeof detail.seconds === 'number' ? `, took ${detail.seconds}s` : '';
      return `calibrated the Dynamips image ${target}${idlePc ? ` (idle-PC ${idlePc}${seconds})` : ''}`;
    }

    case 'template.update': {
      const submitted = Array.isArray(detail.submitted) ? detail.submitted.map(String) : [];
      const parts = changes.length ? changes : submitted;
      return `updated the ${text(detail.template_type)} template ${target}${parts.length ? `: ${joinParts(parts)}` : ''}`;
    }

    case 'system.settings':
      return `changed system settings${changes.length ? `: ${joinParts(changes)}` : ''}${detail.restart_required ? ' (takes effect after a restart)' : ''}`;
    case 'system.iourc':
      return 'replaced the IOL license file';

    default:
      return `${entry.action} ${text(entry.target_type)} ${target}`.trim();
  }
}

/** Who is shown as having acted. */
export function activityActor(entry: ActivityEntry): string {
  return entry.actor || 'unknown user';
}

/** Full sentence, as used by the CSV export and accessible labels. */
export function describeActivity(entry: ActivityEntry): string {
  return `${activityActor(entry)} ${activityPredicate(entry)}`;
}

/**
 * Quote a value for CSV and neutralize spreadsheet formulas: a cell that
 * starts with `=`, `+`, `-` or `@` is data typed by a user (a username, a
 * lab name), and must not execute when the file is opened in a spreadsheet.
 */
export function csvCell(value: unknown): string {
  let cell = value === null || value === undefined ? '' : String(value);
  if (/^[=+\-@\t\r]/.test(cell)) cell = `'${cell}`;
  if (/[",\r\n]/.test(cell)) cell = `"${cell.replace(/"/g, '""')}"`;
  return cell;
}

const CSV_COLUMNS: ReadonlyArray<[string, (entry: ActivityEntry) => unknown]> = [
  ['id', (entry) => entry.id],
  ['time_utc', (entry) => new Date(entry.time * 1000).toISOString()],
  ['user', (entry) => entry.actor],
  ['event', (entry) => entry.event],
  ['outcome', (entry) => entry.outcome],
  ['description', describeActivity],
  ['target_type', (entry) => entry.target_type],
  ['target_id', (entry) => entry.target_id],
  ['target_name', (entry) => entry.target_name],
  ['lab_path', (entry) => entry.lab_path],
  ['lab_name', (entry) => entry.lab_name],
  ['source_ip', (entry) => entry.ip],
  ['detail', (entry) => (entry.detail ? JSON.stringify(entry.detail) : null)]
];

export function activityToCsv(entries: ActivityEntry[]): string {
  const header = CSV_COLUMNS.map(([name]) => name).join(',');
  const rows = entries.map((entry) =>
    CSV_COLUMNS.map(([, value]) => csvCell(value(entry))).join(',')
  );
  return [header, ...rows].join('\r\n');
}

/** Turn `<input type="datetime-local">` values into inclusive epoch-second bounds. */
export function rangeToEpoch(from: string, to: string): { from?: number; to?: number } {
  const bounds: { from?: number; to?: number } = {};
  if (from) {
    const start = new Date(from).getTime();
    if (Number.isFinite(start)) bounds.from = Math.floor(start / 1000);
  }
  if (to) {
    const end = new Date(to).getTime();
    if (Number.isFinite(end)) bounds.to = Math.floor(end / 1000) + 59;
  }
  return bounds;
}
