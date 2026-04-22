<script lang="ts">
  import { goto } from '$app/navigation';
  import { onMount } from 'svelte';

  onMount(() => {
    goto('/login');
  });
</script>

<div class="flex min-h-screen items-center justify-center bg-gray-950 text-sm text-gray-400">
  Redirecting to login...
</div>
