// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { ApiError } from '$lib/api';

export interface FieldError {
  message: string;
  /** Request field the server blames, e.g. ``config.cidr``. */
  field?: string;
}

/** A failed request as a form shows it; a subnet conflict always names the subnet (#325). */
export function fieldError(error: unknown): FieldError {
  const message = error instanceof Error ? error.message : String(error);
  if (!(error instanceof ApiError)) return { message };
  const cidr = error.conflictingCidr;
  return {
    message: cidr && !message.includes(cidr) ? `${message} It overlaps ${error.source ?? 'another network'} (${cidr}).` : message,
    field: error.field
  };
}
