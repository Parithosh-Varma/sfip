import React from 'react';
import Graph from './src/components/Graph.jsx';

export function GraphHarness({ data }) {
  return React.createElement(Graph, { invId: 1 });
}
