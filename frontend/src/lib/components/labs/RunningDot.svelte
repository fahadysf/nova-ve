<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<!-- Folder marker for "something in here is live" (#365): a slow, barely
     perceptible breath — a status hint, not an alarm. -->
<script lang="ts">
  export let count = 0;

  $: label = `${count} ${count === 1 ? 'node' : 'nodes'} running inside`;
</script>

<span class="running-dot" role="img" aria-label={label} title={label} data-testid="folder-running-dot"></span>

<style>
  .running-dot {
    flex: none;
    width: 0.375rem;
    height: 0.375rem;
    border-radius: 9999px;
    background: rgb(52 211 153); /* emerald-400 */
    box-shadow: 0 0 0 3px rgb(52 211 153 / 0.15);
    animation: nv-running-breathe 6s ease-in-out infinite;
  }

  /* Very slow (6s) and very low delta (opacity 1 -> 0.6). */
  @keyframes nv-running-breathe {
    0%,
    100% {
      opacity: 1;
    }
    50% {
      opacity: 0.6;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .running-dot {
      animation: none;
    }
  }
</style>
