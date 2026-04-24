/*
 * Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

import { browser } from '$app/environment';
import { goto } from '$app/navigation';
import { authStore } from '$lib/stores/session';
import { toastStore } from '$lib/stores/toasts';
import type { ApiResponse } from '$lib/types';

const API_BASE = '/api';

export class ApiError extends Error {
  statusCode: number;
  responseCode?: number;
  unauthorized: boolean;

  constructor(message: string, options?: { statusCode?: number; responseCode?: number; unauthorized?: boolean }) {
    super(message);
    this.name = 'ApiError';
    this.statusCode = options?.statusCode ?? 0;
    this.responseCode = options?.responseCode;
    this.unauthorized = options?.unauthorized ?? false;
  }
}

interface ApiRequestOptions extends Omit<RequestInit, 'body'> {
  body?: BodyInit | Record<string, unknown> | null;
  retry?: number;
  suppressToast?: boolean;
}

function buildUrl(path: string): string {
  if (path.startsWith('/api/')) {
    return path;
  }

  if (path.startsWith('/')) {
    return `${API_BASE}${path}`;
  }

  return `${API_BASE}/${path}`;
}

function normalizeBody(body: ApiRequestOptions['body'], headers: Headers): BodyInit | null | undefined {
  if (body == null || body instanceof FormData || typeof body === 'string' || body instanceof URLSearchParams || body instanceof Blob) {
    return body as BodyInit | null | undefined;
  }

  if (!headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }

  return JSON.stringify(body);
}

async function redirectToLogin() {
  authStore.clear();
  if (browser && window.location.pathname !== '/login') {
    await goto('/login');
  }
}

export async function apiRequest<T>(path: string, options: ApiRequestOptions = {}): Promise<ApiResponse<T>> {
  const method = options.method ?? 'GET';
  const retry = options.retry ?? (method === 'GET' ? 1 : 0);
  let attempt = 0;

  while (attempt <= retry) {
    const headers = new Headers(options.headers ?? {});

    try {
      const response = await fetch(buildUrl(path), {
        ...options,
        method,
        headers,
        credentials: 'include',
        body: normalizeBody(options.body, headers)
      });

      const payload = (await response.json()) as ApiResponse<T>;
      const unauthorized = response.status === 401 || payload.code === 401 || payload.status === 'unauthorized';

      if (unauthorized) {
        await redirectToLogin();
        throw new ApiError(payload.message || 'Your session has expired.', {
          statusCode: response.status,
          responseCode: payload.code,
          unauthorized: true
        });
      }

      if (!response.ok || payload.code >= 400 || payload.status === 'fail' || payload.status === 'error') {
        throw new ApiError(payload.message || 'Request failed.', {
          statusCode: response.status,
          responseCode: payload.code
        });
      }

      return payload;
    } catch (error) {
      const isApiError = error instanceof ApiError;
      const canRetry = !isApiError && attempt < retry;
      attempt += 1;

      if (canRetry) {
        await new Promise((resolve) => setTimeout(resolve, 250));
        continue;
      }

      const message = isApiError ? error.message : 'Unable to reach the API. Check the backend connection and try again.';
      if (!options.suppressToast) {
        toastStore.push(message, 'error');
      }
      throw isApiError ? error : new ApiError(message);
    }
  }

  throw new ApiError('Request failed.');
}

export async function apiGetData<T>(path: string, options: ApiRequestOptions = {}): Promise<T> {
  const response = await apiRequest<T>(path, { ...options, method: options.method ?? 'GET' });
  return response.data;
}
