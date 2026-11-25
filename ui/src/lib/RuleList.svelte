<script lang="ts">
  import { get } from './api';
  import { STATUS_LABEL, TIER_LABEL, when } from './format';
  import type { RuleSummary } from './types';

  let q = $state('');
  let area = $state('');
  let tag = $state('');
  let tier = $state('');
  let status = $state('');
  let createdBy = $state('');
  let rules = $state<RuleSummary[]>([]);
  let all = $state<RuleSummary[]>([]);
  let semantic = $state(false);
  let error = $state('');

  const areas = $derived([...new Set(all.flatMap((r) => r.areas))].sort());
  const tags = $derived([...new Set(all.flatMap((r) => r.tags))].sort());

  async function load() {
    try {
      const res = await get<{ rules: RuleSummary[]; semantic: boolean }>('/rules', {
        q, area, tag, tier, status, created_by: createdBy
      });
      rules = res.rules;
      semantic = res.semantic;
      if (!all.length) all = (await get<{ rules: RuleSummary[] }>('/rules')).rules;
      error = '';
    } catch (e) {
      error = String(e);
    }
  }

  let timer: ReturnType<typeof setTimeout>;
  $effect(() => {
    [q, area, tag, tier, status, createdBy];
    clearTimeout(timer);
    timer = setTimeout(load, 200);
    return () => clearTimeout(timer);
  });
</script>

<h2>Regeln</h2>
<div class="filters">
  <input type="search" placeholder="Suche (semantisch)…" bind:value={q} />
  <select bind:value={area}><option value="">Area</option>{#each areas as a}<option>{a}</option>{/each}</select>
  <select bind:value={tag}><option value="">Tag</option>{#each tags as t}<option>{t}</option>{/each}</select>
  <select bind:value={tier}>
    <option value="">Stufe</option>{#each TIER_LABEL as l, i}<option value={i}>{l}</option>{/each}
  </select>
  <select bind:value={status}>
    <option value="">Status</option>{#each Object.entries(STATUS_LABEL) as [k, l]}<option value={k}>{l}</option>{/each}
  </select>
  <select bind:value={createdBy}>
    <option value="">created_by</option>{#each ['claude', 'manual', 'file'] as c}<option>{c}</option>{/each}
  </select>
</div>
{#if error}<p class="err">{error}</p>{/if}
{#if q && semantic}<p class="hint">Semantische Treffer, nach Relevanz sortiert.</p>{/if}
<table data-testid="rule-table">
  <thead><tr><th>Regel</th><th>Status</th><th>Stufe</th><th>Ziel</th><th>Area</th><th>Zuletzt</th></tr></thead>
  <tbody>
    {#each rules as r (r.id)}
      <tr>
        <td>
          <a href="#/rules/{r.id}">{r.title ?? r.id}</a>
          <div class="sub">{r.id}{#each r.tags as t} <span class="tag">{t}</span>{/each}</div>
        </td>
        <td>
          <span class="badge st-{r.status}">{STATUS_LABEL[r.status] ?? r.status}</span>
          {#if r.alert_active}<span class="badge sev-{r.severity}">Alert</span>{/if}
        </td>
        <td><span class="badge tier-{r.tier}">{TIER_LABEL[r.tier]}</span></td>
        <td>{r.target ?? '–'}</td>
        <td>{r.areas.join(', ') || '–'}</td>
        <td>{when(r.last_fired)}</td>
      </tr>
    {:else}
      <tr><td colspan="6" class="hint">Keine Regeln.</td></tr>
    {/each}
  </tbody>
</table>
