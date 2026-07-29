-- Initialize the OpenLineage graph store: enable Apache AGE and create the
-- 'lineage' graph. Runs once on first DB init (docker-entrypoint-initdb.d).
-- Nodes: Job / Run / Dataset (+ Lance version). Edges: WROTE / READ / DERIVED_FROM.
CREATE EXTENSION IF NOT EXISTS age;
LOAD 'age';
SET search_path = ag_catalog, "$user", public;
SELECT create_graph('lineage');
