# 0013. AGE-on-CNPG vs Lance-native-graph (the lineage-store decision)

**Decision.** The lineage graph needs the Apache **AGE** extension, but CNPG runs stock Postgres — so the
rask fold-in must pick one of: (a) point CNPG at a custom Postgres-with-AGE image, (b) keep AGE as a separate
operand, or (c) execute the pivot to move lineage to a **Lance-native graph**, which drops the AGE/Postgres
dependency entirely.

**Rationale.** This is the load-bearing pre-merge decision — it blocks the chart flip and shapes the CNPG
database list. The Lance-native-graph pivot is the option that *removes* an operand rather than adding a
custom image-build; it must be decided before/early in the merge.
