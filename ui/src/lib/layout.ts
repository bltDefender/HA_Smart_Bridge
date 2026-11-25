import ELK from 'elkjs/lib/elk.bundled.js';
import type { Graph } from './types';

const elk = new ELK();
export const SIZE = { width: 220, height: 64 };

export interface Placed {
  positions: Record<string, { x: number; y: number }>;
}

// Das Layout kommt von elkjs; die Spalten (trigger → condition → entity → result) liefern
// nur die Reihenfolge über die Kanten, die Logik bleibt im Backend-Graphen.
export async function layout(graph: Graph): Promise<Placed> {
  const result = await elk.layout({
    id: 'root',
    layoutOptions: graph.layout_hints.elk,
    children: graph.nodes.map((n) => ({ id: n.id, ...SIZE })),
    edges: graph.edges.map((e) => ({ id: e.id, sources: [e.source], targets: [e.target] }))
  });
  const positions: Placed['positions'] = {};
  for (const c of result.children ?? []) positions[c.id] = { x: c.x ?? 0, y: c.y ?? 0 };
  return { positions };
}
