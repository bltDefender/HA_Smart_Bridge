<script lang="ts">
  import { get } from './api';
  import { heat, lastDays } from './format';
  import { live } from './live.svelte';
  import type { Overview } from './types';

  let data = $state<Overview | null>(null);
  let by = $state<'area' | 'tag' | 'target'>('area');
  const LABEL = { area: 'Area', tag: 'Tag', target: 'Modell-Ziel' };

  async function load() {
    data = await get<Overview>('/overview');
  }
  $effect(() => {
    live.runTick;
    load();
  });
  const days = $derived(data ? lastDays(data.heatmap.days) : []);
  const max = $derived(Math.max(0, ...Object.values(data?.heatmap.rules ?? {}).flatMap((d) => Object.values(d))));
  const byId = $derived(Object.fromEntries((data?.rules ?? []).map((r) => [r.id, r])));
  const flagged = (id: string) =>
    data?.highlight.error.includes(id) ? 'error' : data?.highlight.shadow.includes(id) ? 'shadow' : '';
</script>

<h2>Übersicht</h2>
{#if data}
  {#if data.highlight.error.length || data.highlight.shadow.length}
    <div class="callout" data-testid="highlights">
      {#each data.highlight.error as id}<a class="badge st-error" href="#/rules/{id}">Fehler: {id}</a>{/each}
      {#each data.highlight.shadow as id}<a class="badge st-shadow" href="#/rules/{id}">Shadow: {id}</a>{/each}
    </div>
  {/if}

  <div class="tabs">
    {#each Object.entries(LABEL) as [k, l]}
      <button class:on={by === k} onclick={() => (by = k as typeof by)}>{l}</button>
    {/each}
  </div>
  <div class="groups" data-testid="groups">
    {#each Object.entries(data.groups[by]) as [name, ids]}
      <section class="group">
        <h4>{name} <span class="sub">({ids.length})</span></h4>
        {#each ids as id}
          <a class="chip {flagged(id)}" href="#/rules/{id}">{byId[id]?.title ?? id}</a>
        {/each}
      </section>
    {/each}
  </div>

  <h3>Auslösehäufigkeit ({data.heatmap.days} Tage, UTC)</h3>
  <div class="heat" style="--cols:{days.length}" data-testid="heatmap">
    <div></div>
    {#each days as d}<div class="hd">{d.slice(8)}</div>{/each}
    {#each data.rules as r (r.id)}
      <a class="hn" href="#/rules/{r.id}">{r.title ?? r.id}</a>
      {#each days as d}
        {@const n = data.heatmap.rules[r.id]?.[d] ?? 0}
        <div class="hc" title="{r.id} {d}: {n}" style="--a:{heat(n, max)}">{n || ''}</div>
      {/each}
    {/each}
  </div>
{/if}
