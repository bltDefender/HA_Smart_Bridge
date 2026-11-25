<script lang="ts">
  import { SvelteFlow, Background, Controls, MiniMap, type Node, type Edge } from '@xyflow/svelte';
  import '@xyflow/svelte/dist/style.css';
  import FlowNode from './FlowNode.svelte';
  import { layout, SIZE } from './layout';
  import { live } from './live.svelte';
  import type { Graph } from './types';

  let { graph }: { graph: Graph } = $props();
  let nodes = $state.raw<Node[]>([]);
  let edges = $state.raw<Edge[]>([]);
  let error = $state('');
  const nodeTypes = { rule: FlowNode };

  // Hervorhebung des genommenen Pfads kommt komplett aus graph.run.highlight.
  $effect(() => {
    const g = graph;
    const states = live.states;
    layout(g)
      .then(({ positions }) => {
        const hl = g.run?.highlight;
        nodes = g.nodes.map((n) => ({
          id: n.id,
          type: 'rule',
          position: positions[n.id] ?? { x: 0, y: 0 },
          width: SIZE.width,
          height: SIZE.height,
          draggable: false,
          connectable: false,
          data: {
            node: n,
            trace: g.run?.nodes[n.id],
            taken: hl?.nodes.includes(n.id) ?? false,
            dim: hl ? !hl.nodes.includes(n.id) : false,
            live: n.kind === 'entity' ? (states[n.data.entity_id]?.state ?? n.data.state) : undefined
          }
        }));
        edges = g.edges.map((e) => ({
          id: e.id,
          source: e.source,
          target: e.target,
          label: e.label ?? undefined,
          animated: hl?.edges.includes(e.id) ?? false,
          class: `e-${e.kind}${hl ? (hl.edges.includes(e.id) ? ' taken' : ' dim') : ''}`
        }));
      })
      .catch((err) => (error = String(err)));
  });
</script>

{#if error}<p class="err">{error}</p>{/if}
<div class="flow" data-testid="flow">
  <SvelteFlow
    {nodes}
    {edges}
    {nodeTypes}
    fitView
    nodesDraggable={false}
    nodesConnectable={false}
    elementsSelectable={false}
    proOptions={{ hideAttribution: true }}
  >
    <Background />
    <Controls showLock={false} />
    <MiniMap pannable zoomable />
  </SvelteFlow>
</div>
