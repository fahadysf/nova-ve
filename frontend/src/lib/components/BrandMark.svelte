<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  /** Edge length in px; the 44px login chip uses 24, the 28px sidebar chip uses 15. */
  export let size = 24;
</script>

<svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden="true">
  <line x1="5" y1="5" x2="12" y2="12" stroke="#6b7280" stroke-width="1.25" />
  <line x1="19" y1="5" x2="12" y2="12" stroke="#6b7280" stroke-width="1.25" />
  <line x1="5" y1="19" x2="12" y2="12" stroke="#6b7280" stroke-width="1.25" />
  <line x1="19" y1="19" x2="12" y2="12" stroke="#6b7280" stroke-width="1.25" />
  <rect x="2" y="2" width="6" height="6" rx="1" fill="#1f2937" stroke="#10b981" stroke-width="1.25" />
  <rect x="16" y="2" width="6" height="6" rx="1" fill="#1f2937" stroke="#9ca3af" stroke-width="1.25" />
  <rect x="2" y="16" width="6" height="6" rx="1" fill="#1f2937" stroke="#9ca3af" stroke-width="1.25" />
  <rect x="16" y="16" width="6" height="6" rx="1" fill="#1f2937" stroke="#10b981" stroke-width="1.25" />
  <circle cx="12" cy="12" r="2.5" fill="#3b82f6" />
</svg>
