<script lang="ts">
  import Devices from './lib/Devices.svelte';
  import OverviewView from './lib/OverviewView.svelte';
  import RuleDetail from './lib/RuleDetail.svelte';
  import RuleList from './lib/RuleList.svelte';
  import { live } from './lib/live.svelte';

  // Hash-Routing: keine serverseitigen Fallback-Routen nötig, nur GET auf statische Dateien.
  let hash = $state(location.hash.slice(1) || '/');
  $effect(() => {
    const on = () => (hash = location.hash.slice(1) || '/');
    window.addEventListener('hashchange', on);
    live.start();
    return () => window.removeEventListener('hashchange', on);
  });
  const parts = $derived(hash.split('/').filter(Boolean).map(decodeURIComponent));
  const view = $derived(parts[0] ?? 'rules-list');
</script>

<header>
  <strong>smart-bridge</strong>
  <nav>
    <a href="#/" class:on={view === 'rules-list'}>Regeln</a>
    <a href="#/devices" class:on={view === 'devices'}>Geräte</a>
    <a href="#/overview" class:on={view === 'overview'}>Übersicht</a>
  </nav>
  <span class="live" class:on={live.connected} title="Live-Updates (SSE)">● {live.connected ? 'live' : 'offline'}</span>
  <span class="alerts" data-testid="alert-count">{live.alerts.length} Alert(s)</span>
</header>
<main>
  {#if view === 'rules'}
    {#key parts[1]}<RuleDetail id={parts[1] ?? ''} />{/key}
  {:else if view === 'devices'}
    <Devices id={parts[1] ? parts.slice(1).join('/') : ''} />
  {:else if view === 'overview'}
    <OverviewView />
  {:else}
    <RuleList />
  {/if}
</main>
