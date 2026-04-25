<script lang="ts">
  import { toastStore } from '$lib/stores/toasts';

  const toneClasses = {
    error: 'border-[color:var(--toast-error-bd)] bg-[color:var(--toast-error-bg)] text-[color:var(--toast-error-fg)]',
    success:
      'border-[color:var(--toast-success-bd)] bg-[color:var(--toast-success-bg)] text-[color:var(--toast-success-fg)]',
    info: 'border-[color:var(--toast-info-bd)] bg-[color:var(--toast-info-bg)] text-[color:var(--toast-info-fg)]'
  } as const;

  const labelByKind = {
    error: 'Error',
    success: 'Success',
    info: 'Info'
  } as const;
</script>

<div class="pointer-events-none fixed right-4 top-[4.5rem] z-50 flex w-full max-w-sm flex-col gap-3">
  {#each $toastStore as toast (toast.id)}
    <div
      class={`pointer-events-auto rounded-xl border px-4 py-3 shadow-xl shadow-black/20 backdrop-blur ${toneClasses[toast.kind]}`}
    >
      <div class="flex items-start justify-between gap-3">
        <div class="min-w-0">
          <div class="text-[10px] uppercase tracking-[0.22em] text-white/70">{labelByKind[toast.kind]}</div>
          <p class="mt-2 text-sm leading-5">{toast.message}</p>
        </div>
        <button
          type="button"
          class="shrink-0 text-[10px] uppercase tracking-[0.22em] text-white/70 transition hover:text-white"
          on:click={() => toastStore.remove(toast.id)}
        >
          Close
        </button>
      </div>
    </div>
  {/each}
</div>
