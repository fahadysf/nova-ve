<!-- Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script lang="ts">
  import type { NodeCatalogExtraField } from '$lib/types';

  export let field: NodeCatalogExtraField;
  export let value: unknown;
  export let canReset: boolean;
  export let onChange: (value: unknown) => void;
  export let onReset: () => void;

  const control = 'block w-full rounded-lg border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-100 focus:border-blue-500 focus:outline-none';
  type EnvEntry = { key: string; value: string };

  function environmentEntries(raw: unknown): EnvEntry[] {
    if (!Array.isArray(raw)) return [];
    return raw.map((entry) => ({
      key: String(entry?.key ?? ''),
      value: String(entry?.value ?? ''),
    }));
  }

  function updateEntry(index: number, property: keyof EnvEntry, nextValue: string) {
    onChange(entries.map((entry, i) => i === index ? { ...entry, [property]: nextValue } : entry));
  }

  $: entries = environmentEntries(value);
  $: textValue = Array.isArray(value) ? value.join('\n') : String(value ?? '');
</script>

<div class={field.type === 'env' || field.type === 'textarea' ? 'sm:col-span-2' : ''} data-testid={'template-extra-' + field.key}>
  <div class="mb-1 flex items-baseline justify-between gap-2">
    {#if field.type === 'env'}
      <span class="text-xs text-gray-300" id={'template-extra-label-' + field.key}>{field.label}</span>
    {:else}
      <label class="text-xs text-gray-300" for={'template-extra-input-' + field.key}>{field.label}</label>
    {/if}
    {#if canReset && field.type !== 'readonly'}
      <button
        class="shrink-0 text-xs text-blue-300 hover:underline"
        type="button"
        aria-label={'Use default for ' + field.label}
        on:click={onReset}
      >Use default</button>
    {/if}
  </div>

  {#if field.type === 'env'}
    <div class="space-y-2" role="group" aria-labelledby={'template-extra-label-' + field.key}>
      {#each entries as entry, index}
        <div class="grid grid-cols-[minmax(0,1fr)_minmax(0,1.5fr)_auto] items-center gap-2">
          <input
            class={control}
            value={entry.key}
            placeholder="KEY"
            aria-label={'Variable name ' + (index + 1)}
            required
            on:input={(event) => updateEntry(index, 'key', event.currentTarget.value)}
          />
          <input
            class={control}
            value={entry.value}
            placeholder="value"
            aria-label={'Variable value ' + (index + 1)}
            on:input={(event) => updateEntry(index, 'value', event.currentTarget.value)}
          />
          <button
            class="rounded-lg border border-gray-700 px-2 py-1 text-xs text-gray-300 hover:text-white"
            type="button"
            aria-label={'Remove variable ' + (index + 1)}
            on:click={() => onChange(entries.filter((_, i) => i !== index))}
          >Remove</button>
        </div>
      {/each}
      <button
        class="rounded-lg border border-gray-700 px-3 py-1.5 text-xs text-gray-300 hover:text-white"
        type="button"
        on:click={() => onChange([...entries, { key: '', value: '' }])}
      >Add variable</button>
    </div>
  {:else if field.type === 'select'}
    <select
      class={control}
      id={'template-extra-input-' + field.key}
      value={textValue}
      on:change={(event) => onChange(event.currentTarget.value)}
    >
      {#if !field.options?.some((option) => option.value === textValue)}
        <option value={textValue}>{textValue || 'Default'}</option>
      {/if}
      {#each field.options ?? [] as option (option.value)}
        <option value={option.value}>{option.label ?? option.value}</option>
      {/each}
    </select>
  {:else if field.type === 'textarea'}
    <textarea
      class={control + ' resize-y font-mono'}
      id={'template-extra-input-' + field.key}
      value={textValue}
      placeholder={field.placeholder ?? ''}
      rows="2"
      spellcheck={false}
      on:input={(event) => {
        const next = event.currentTarget.value;
        onChange(Array.isArray(value) ? next.split('\n') : next);
      }}
    ></textarea>
    {#if Array.isArray(value)}
      <p class="mt-1 text-xs text-gray-400">Use one argument per line.</p>
    {/if}
  {:else if field.type === 'number'}
    <input
      type="number"
      class={control}
      id={'template-extra-input-' + field.key}
      value={String(value ?? '')}
      placeholder={field.placeholder ?? ''}
      required
      on:input={(event) => onChange(event.currentTarget.valueAsNumber)}
    />
  {:else}
    <input
      class={control}
      id={'template-extra-input-' + field.key}
      value={textValue}
      placeholder={field.placeholder ?? ''}
      readonly={field.type === 'readonly'}
      spellcheck={false}
      on:input={(event) => onChange(event.currentTarget.value)}
    />
  {/if}

  {#if field.description}
    <p class="mt-1 text-xs text-gray-400">{field.description}</p>
  {/if}
</div>
