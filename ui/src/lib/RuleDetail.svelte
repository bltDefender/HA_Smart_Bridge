<script lang="ts">
  import { get } from './api';
  import Flow from './Flow.svelte';
  import { STATUS_LABEL, TIER_LABEL, diffClass, highlightYaml, when } from './format';
  import { live } from './live.svelte';
  import type { Graph, RuleDetail, RunRow } from './types';

  let { id }: { id: string } = $props();
  let detail = $state<RuleDetail | null>(null);
  let graph = $state<Graph | null>(null);
  let runs = $state<RunRow[]>([]);
  let runId = $state('');
  let diff = $state('');
  let diffFrom = $state(0);
  let diffTo = $state(0);
  let error = $state('');

  async function loadGraph() {
    graph = await get<Graph>(`/rules/${encodeURIComponent(id)}/graph`, { run_id: runId });
  }

  async function load() {
    try {
      detail = await get<RuleDetail>(`/rules/${encodeURIComponent(id)}`);
      runs = (await get<{ runs: RunRow[] }>(`/rules/${encodeURIComponent(id)}/runs`, { limit: 20 })).runs;
      const vs = detail.versions.map((v) => v.version);
      diffTo = Math.max(0, ...vs);
      diffFrom = vs.length > 1 ? Math.min(...vs) : 0;
      await loadGraph();
    } catch (e) {
      error = String(e);
    }
  }

  async function showDiff() {
    const res = await get<{ diff: string }>(`/rules/${encodeURIComponent(id)}/diff`, { from: diffFrom, to: diffTo });
    diff = res.diff || '(keine Änderung)';
  }

  $effect(() => {
    id;
    runId = '';
    load();
  });
  $effect(() => {
    runId;
    if (detail) loadGraph().catch((e) => (error = String(e)));
  });
  $effect(() => {
    // neuer Lauf dieser Regel: Liste nachladen
    if (live.runTick && live.lastRun?.rule === id) {
      get<{ runs: RunRow[] }>(`/rules/${encodeURIComponent(id)}/runs`, { limit: 20 }).then((r) => (runs = r.runs));
    }
  });
</script>

<p><a href="#/">← Regeln</a></p>
{#if error}<p class="err">{error}</p>{/if}
{#if detail}
  <h2>{detail.title ?? detail.id}</h2>
  <p class="meta">
    <span class="badge st-{detail.status}">{STATUS_LABEL[detail.status] ?? detail.status}</span>
    <span class="badge tier-{detail.tier}">{TIER_LABEL[detail.tier]}</span>
    {#if detail.alert_active}<span class="badge sev-{detail.severity}">Alert aktiv</span>{/if}
    <span class="sub">{detail.id} · v{detail.version} · {detail.created_by} · zuletzt {when(detail.last_fired)}</span>
  </p>
  {#if detail.intent}<blockquote data-testid="intent">{detail.intent}</blockquote>{/if}

  <h3>Flow {#if runId}<span class="hint">(Lauf {runId})</span>{/if}</h3>
  {#if graph}<Flow {graph} />{/if}

  <h3>Läufe</h3>
  <table data-testid="run-table">
    <thead><tr><th>Zeit</th><th>Phase</th><th>Trigger</th><th>Ergebnis</th><th>Dauer</th><th>Executor</th><th></th></tr></thead>
    <tbody>
      {#each runs as r (r.run_id)}
        <tr class:sel={r.run_id === runId}>
          <td>{when(r.started_at)}</td><td>{r.phase}</td><td>{r.trigger.type}{r.trigger.entity_id ? ` · ${r.trigger.entity_id}` : ''}</td>
          <td>{r.result}</td><td>{r.duration_ms} ms</td><td>{r.executor ?? '–'}</td>
          <td>
            <button onclick={() => (runId = r.run_id === runId ? '' : r.run_id)}>
              {r.run_id === runId ? 'Trace aus' : 'Trace'}
            </button>
          </td>
        </tr>
      {:else}
        <tr><td colspan="7" class="hint">Noch keine Läufe.</td></tr>
      {/each}
    </tbody>
  </table>

  <h3>YAML</h3>
  <pre class="yaml" data-testid="yaml">{@html highlightYaml(detail.yaml)}</pre>

  <h3>Versionen</h3>
  {#if detail.versions.length}
    <div class="filters">
      <select bind:value={diffFrom}>{#each detail.versions as v}<option value={v.version}>v{v.version}</option>{/each}</select>
      →
      <select bind:value={diffTo}>{#each detail.versions as v}<option value={v.version}>v{v.version}</option>{/each}</select>
      <button onclick={showDiff}>Diff anzeigen</button>
    </div>
    {#if diff}
      <pre class="diff">{#each diff.split('\n') as line}<span class={diffClass(line)}>{line}
</span>{/each}</pre>
    {/if}
  {:else}<p class="hint">Keine Versionshistorie.</p>{/if}
{/if}
