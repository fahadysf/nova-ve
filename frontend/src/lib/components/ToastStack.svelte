<script lang="ts">
  import { toastStore } from '$lib/stores/toasts';

  const toneClasses = {
    error: 'border-red-500/40 bg-red-500/10 text-red-100',
    success: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-100',
    info: 'border-sky-500/40 bg-sky-500/10 text-sky-100'
  } as const;
</script>

<div class="pointer-events-none fixed right-4 top-4 z-50 flex w-full max-w-sm flex-col gap-3">
  {#each $toastStore as toast (toast.id)}
    <div class={`pointer-events-auto rounded-xl border px-4 py-3 shadow-lg backdrop-blur ${toneClasses[toast.kind]}`}>
      <div class="flex items-start justify-between gap-3">
        <p class="text-sm leading-5">{toast.message}</p>
        <button
          type="button"
          class="shrink-0 text-xs uppercase tracking-[0.2em] text-white/70 hover:text-white"
          on:click={() => toastStore.remove(toast.id)}
        >
          Close
        </button>
      </div>
    </div>
  {/each}
</div>
