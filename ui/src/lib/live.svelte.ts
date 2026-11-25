import { eventsUrl } from './api';

// Live-Updates per SSE. Zustände, Läufe und Alerts landen in reaktiven Feldern.
class Live {
  connected = $state(false);
  states = $state<Record<string, { state: string; stale: boolean }>>({});
  alerts = $state<any[]>([]);
  lastRun = $state<{ run_id: string; rule: string; result: string } | null>(null);
  runTick = $state(0);
  private source: EventSource | null = null;

  start() {
    if (this.source) return;
    const es = new EventSource(eventsUrl());
    this.source = es;
    es.onopen = () => (this.connected = true);
    es.onerror = () => (this.connected = false); // EventSource verbindet selbst neu
    es.addEventListener('state', (e) => {
      const d = JSON.parse((e as MessageEvent).data);
      this.states[d.entity_id] = { state: d.state, stale: d.stale };
    });
    es.addEventListener('run', (e) => {
      this.lastRun = JSON.parse((e as MessageEvent).data);
      this.runTick++;
    });
    es.addEventListener('alerts', (e) => (this.alerts = JSON.parse((e as MessageEvent).data)));
  }
}

export const live = new Live();
