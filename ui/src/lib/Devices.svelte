<script lang="ts">
  import { get } from './api';
  import { icon } from './format';
  import { live } from './live.svelte';
  import type { EntityRow, RuleSummary } from './types';

  let { id = '' }: { id?: string } = $props();
  let entities = $state<EntityRow[]>([]);
  let filter = $state('');
  let rules = $state<RuleSummary[]>([]);
  let info = $state<{ name?: string; state?: string } | null>(null);
  const ROLE = { trigger: 'Trigger', condition: 'Bedingung', action: 'Ziel', alert: 'Alert' } as Record<string, string>;

  $effect(() => {
    get<{ entities: EntityRow[] }>('/entities').then((r) => (entities = r.entities));
  });
  $effect(() => {
    if (!id) return void (rules = []);
    get<{ rules: RuleSummary[]; entity: typeof info }>(`/entities/${encodeURIComponent(id)}/rules`).then((r) => {
      rules = r.rules;
      info = r.entity;
    });
  });
  const shown = $derived(entities.filter((e) => `${e.entity_id} ${e.name}`.toLowerCase().includes(filter.toLowerCase())));
</script>

<h2>Geräte</h2>
<div class="split">
  <div>
    <input type="search" placeholder="Entity filtern…" bind:value={filter} />
    <ul class="list" data-testid="entity-list">
      {#each shown as e (e.entity_id)}
        <li class:sel={e.entity_id === id}>
          <a href="#/devices/{e.entity_id}">{icon(e.domain)} {e.name}</a>
          <span class="badge" class:stale={e.stale}>{live.states[e.entity_id]?.state ?? e.state ?? '?'}</span>
          <div class="sub">{e.entity_id} · {e.rule_count} Regel(n){e.area ? ` · ${e.area}` : ''}</div>
        </li>
      {/each}
    </ul>
  </div>
  <div>
    {#if id}
      <h3>{info?.name ?? id}</h3>
      <table data-testid="entity-rules">
        <thead><tr><th>Regel</th><th>Rolle</th></tr></thead>
        <tbody>
          {#each rules as r (r.id)}
            <tr><td><a href="#/rules/{r.id}">{r.title ?? r.id}</a></td><td>{(r.roles ?? []).map((x) => ROLE[x] ?? x).join(', ')}</td></tr>
          {:else}
            <tr><td colspan="2" class="hint">Keine Regel nutzt diese Entity.</td></tr>
          {/each}
        </tbody>
      </table>
    {:else}<p class="hint">Entity wählen.</p>{/if}
  </div>
</div>
