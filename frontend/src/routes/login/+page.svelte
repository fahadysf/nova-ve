<script lang="ts">
  import { login, authStore } from '$lib/stores/auth';
  import { goto } from '$app/navigation';
  import { page } from '$app/stores';
  import BrandMark from '$lib/components/BrandMark.svelte';

  let username = '';
  let password = '';
  let error = '';

  function getNextUrl(): string {
    const next = $page.url.searchParams.get('next') ?? '';
    return next.startsWith('/') && !next.startsWith('//') ? next : '/labs';
  }

  async function handleLogin() {
    error = '';
    const result = await login(username, password);
    if (result.success) {
      goto(getNextUrl());
    } else {
      error = result.message || 'Login failed';
    }
  }

  $: if ($authStore.authenticated) {
    goto(getNextUrl());
  }
</script>

<div class="flex flex-1 items-center justify-center bg-gray-900 px-4 py-10">
  <div class="w-full max-w-sm rounded-xl border border-gray-700 bg-gray-800 px-8 py-8 shadow-2xl shadow-black/30">
    <div class="text-center">
      <div class="flex items-center justify-center gap-3">
        <div class="flex h-11 w-11 items-center justify-center rounded-2xl border border-gray-700 bg-gray-950/90 shadow-lg shadow-black/20">
          <BrandMark />
        </div>
        <h1 class="text-[31px] font-bold leading-none text-gray-100" style="font-family: var(--font-sans); letter-spacing: -0.02em;">
          nova<span class="text-blue-500">-</span>ve
        </h1>
      </div>
      <p class="mt-3 text-sm text-gray-400">
        Sign in to manage labs, launch nodes, and open console sessions.
      </p>
    </div>

    <form on:submit|preventDefault={handleLogin} class="mt-8 space-y-4">
      <div>
        <label for="username" class="mb-1 block text-[13px] text-gray-400">Username</label>
        <input
          id="username"
          type="text"
          bind:value={username}
          class="w-full rounded border border-gray-600 bg-gray-700 px-3 py-2 text-sm text-gray-100 outline-none transition focus:border-blue-500"
          required
        />
      </div>

      <div>
        <label for="password" class="mb-1 block text-[13px] text-gray-400">Password</label>
        <input
          id="password"
          type="password"
          bind:value={password}
          class="w-full rounded border border-gray-600 bg-gray-700 px-3 py-2 text-sm text-gray-100 outline-none transition focus:border-blue-500"
          required
        />
      </div>

      {#if error}
        <div class="rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-200">
          {error}
        </div>
      {/if}

      <button
        type="submit"
        class="w-full rounded bg-blue-600 px-4 py-2 text-sm font-semibold text-white transition hover:bg-blue-500"
      >
        Login
      </button>
    </form>
  </div>
</div>
