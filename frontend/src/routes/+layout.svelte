<script>
  import '../app.css';
  import { onMount } from 'svelte';
  import { authStore, checkAuth } from '$lib/stores/auth';

  onMount(() => {
    checkAuth();
  });
</script>

<div class="h-screen w-screen flex flex-col">
  {#if $authStore.loading}
    <div class="flex-1 flex items-center justify-center">
      <div class="text-xl">Loading...</div>
    </div>
  {:else}
    <slot />
  {/if}
</div>
