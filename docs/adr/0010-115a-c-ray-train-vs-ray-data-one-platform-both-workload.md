# 0010. #115a-c — Ray TRAIN vs Ray DATA (one platform, both workload classes)

**Decision.** The platform hosts **both** batch/ETL (the medallion cascade — the Ray *Data* shape) and
long-running **training** (Ray *Train*) as distinct workload classes with different runtime treatment
(bounded stage-transform vs fire-and-track submit+ack; RETRY vs terminal FAIL on GPU-hours; `ETL` vs
`TRAINING` jobType) but **one** provenance model, **one** authz model, **one** storage substrate. `POST
/train` gets its own topic (not a field on the stage trigger). #115a (head + topic + submit-and-ack
consumer), #115b (`ray_train_job.py` + registry publish + lifecycle lineage) and #115c (seed grants) all
landed at the unit tier.

**Rationale.** Training and ETL are genuinely different workload classes, but forking the governance /
lineage / storage model across them would be the wrong seam. Open residual: the chart values passthrough and
the live kind drive.
