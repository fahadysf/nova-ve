<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<!--
  Shell route group (#260): everything inside renders with the app shell
  (top bar + left nav). The lab editor lives outside this group and stays
  fullscreen.
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import { page } from '$app/stores';
  import { goto } from '$app/navigation';
  import { authStore } from '$lib/stores/auth';
  import AppShell from '$lib/components/shell/AppShell.svelte';

  onMount(() => {
    if (!$authStore.authenticated) {
      goto('/login');
    }
  });
</script>

<AppShell pathname={$page.url.pathname}>
  <slot />
</AppShell>
