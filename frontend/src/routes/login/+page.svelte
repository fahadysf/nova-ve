<script lang="ts">
  import { login, authStore } from '$lib/stores/auth';
  import { goto } from '$app/navigation';

  let username = '';
  let password = '';
  let error = '';

  async function handleLogin() {
    error = '';
    const result = await login(username, password);
    if (result.success) {
      goto('/labs');
    } else {
      error = result.message || 'Login failed';
    }
  }

  $: if ($authStore.authenticated) {
    goto('/labs');
  }
</script>

<div class="flex-1 flex items-center justify-center bg-gray-900">
  <div class="bg-gray-800 p-8 rounded-lg shadow-lg w-96 border border-gray-700">
    <h1 class="text-2xl font-bold mb-6 text-center">nova-ve</h1>
    <form on:submit|preventDefault={handleLogin} class="space-y-4">
      <div>
        <label for="username" class="block text-sm text-gray-400 mb-1">Username</label>
        <input
          id="username"
          type="text"
          bind:value={username}
          class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded focus:outline-none focus:border-blue-500"
          required
        />
      </div>
      <div>
        <label for="password" class="block text-sm text-gray-400 mb-1">Password</label>
        <input
          id="password"
          type="password"
          bind:value={password}
          class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded focus:outline-none focus:border-blue-500"
          required
        />
      </div>
      {#if error}
        <div class="text-red-400 text-sm">{error}</div>
      {/if}
      <button
        type="submit"
        class="w-full py-2 bg-blue-600 hover:bg-blue-500 rounded font-semibold transition"
      >
        Login
      </button>
    </form>
  </div>
</div>
