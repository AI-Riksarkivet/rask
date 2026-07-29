"""Vulture whitelist — symbols vulture reports as dead that are NOT dead.

Regenerate:  make deadcode-whitelist

WHY THIS FILE EXISTS. Vulture is a static reachability checker; it cannot see call sites that live in a
framework's registry rather than in our source. Without it the sweep reported ~70 "dead" symbols in
services/, every single one a false positive — and a sweep that cries wolf 70 times is WORSE than no sweep
at all, because a genuinely dead symbol is then invisible in the noise. With it, `make deadcode` prints ~0
and a NEW dead symbol stands out immediately. That is the whole point: keep the signal-to-noise at 1.

The categories held here, all verified by reading the call site:
  · FastAPI route handlers / exception handlers  — invoked by the router, never by name.
  · Dapr pub-sub + cron handlers (on_stage, on_bronze_arrival, on_train_trigger) — invoked by the sidecar.
  · pydantic `model_config` + `@model_validator` / `@field_validator` methods — invoked by pydantic.
  · pydantic MODEL FIELDS on response/claims schemas — "used" via serialization, not by name.
  · Test hooks (e.g. _reset_peek_cache) and unittest.mock attributes (return_value / side_effect).

LANDMINE — do NOT "clean up" what vulture flags without reading it first. `Image.MAX_IMAGE_PIXELS` in
services/medallion/services/media.py is reported as an "unused attribute" because it is an assignment to a
Pillow global. It is the DECOMPRESSION-BOMB GUARD. Deleting it silently disarms a security control while
every test stays green. That symbol is the reason this file is documented rather than auto-trusted.

NOT whitelisted on purpose: services/common/sinks.py (SinkAdapter/LocalDirSink/S3Sink). No SERVICE imports
it — its only consumers are its own unit test and scripts/media_pipeline_e2e.py. It is the un-wired "gold
sink" seam from the data-zone architecture. It is left visible so it keeps showing up as an architectural
gap to either wire or delete, rather than being quietly blessed as fine.
"""

done  # unused variable (scripts/medallion_demo.py:99)
model_config  # unused variable (services/catalog/core/config.py:23)
model_config  # unused variable (services/common/oidc.py:69)
exp  # unused variable (services/common/oidc.py:74)
iat  # unused variable (services/common/oidc.py:75)
model_config  # unused variable (services/common/sources.py:22)
model_config  # unused variable (services/compaction/core/config.py:14)
model_config  # unused variable (services/lineage/core/config.py:23)
model_config  # unused variable (services/lineage/models.py:24)
model_config  # unused variable (services/lineage/models.py:185)
model_config  # unused variable (services/lineage/models.py:216)
model_config  # unused variable (services/lineage/models.py:224)
quality_passed  # unused variable (services/lineage/schemas.py:78)
description  # unused variable (services/lineage/schemas.py:112)
transformation_type  # unused variable (services/lineage/schemas.py:164)
description  # unused variable (services/lineage/schemas.py:167)
on_stage  # unused function (services/medallion/api/events.py:36)
on_bronze_arrival  # unused function (services/medallion/api/bronze_arrival.py:41)
on_train_trigger  # unused function (services/medallion/api/train.py:100)
model_config  # unused variable (services/medallion/core/config.py:21)
pytestmark  # unused variable (tests/e2e/test_auth_e2e.py:24)
pytestmark  # unused variable (tests/e2e/test_client_direct_e2e.py:27)
pytestmark  # unused variable (tests/e2e/test_compaction_e2e.py:33)
pytestmark  # unused variable (tests/e2e/test_e2e.py:17)
pytestmark  # unused variable (tests/e2e/test_gateway_e2e.py:23)
pytestmark  # unused variable (tests/e2e/test_governance_e2e.py:40)
pytestmark  # unused variable (tests/e2e/test_governed_union_e2e.py:72)
pytestmark  # unused variable (tests/e2e/test_lineage_e2e.py:22)
pytestmark  # unused variable (tests/e2e/test_medallion_e2e.py:36)
pytestmark  # unused variable (tests/e2e/test_media_e2e.py:39)
pytestmark  # unused variable (tests/e2e/test_multibase_e2e.py:32)
pytestmark  # unused variable (tests/e2e/test_object_store_cas_e2e.py:42)
pytestmark  # unused variable (tests/e2e/test_observability_e2e.py:41)
pytestmark  # unused variable (tests/e2e/test_outbox_crash_e2e.py:50)
pytestmark  # unused variable (tests/e2e/test_outbox_e2e.py:29)
pytestmark  # unused variable (tests/e2e/test_warehouses_e2e.py:32)
_.return_value  # unused attribute (tests/integration/test_api.py:39)
_.return_value  # unused attribute (tests/integration/test_api.py:45)
_.return_value  # unused attribute (tests/integration/test_api.py:51)
_.return_value  # unused attribute (tests/integration/test_api.py:64)
_.return_value  # unused attribute (tests/integration/test_api.py:72)
_.return_value  # unused attribute (tests/integration/test_api.py:82)
_.return_value  # unused attribute (tests/integration/test_api.py:98)
_.return_value  # unused attribute (tests/integration/test_api.py:154)
_.return_value  # unused attribute (tests/integration/test_api.py:169)
_.return_value  # unused attribute (tests/integration/test_api.py:181)
_.return_value  # unused attribute (tests/integration/test_api.py:186)
_.return_value  # unused attribute (tests/integration/test_api.py:193)
_.side_effect  # unused attribute (tests/integration/test_api.py:203)
_.side_effect  # unused attribute (tests/integration/test_api.py:212)
_.side_effect  # unused attribute (tests/integration/test_api.py:220)
_.return_value  # unused attribute (tests/integration/test_api.py:229)
_.return_value  # unused attribute (tests/integration/test_api.py:279)
_.return_value  # unused attribute (tests/integration/test_api.py:350)
_.return_value  # unused attribute (tests/integration/test_api.py:369)
_.return_value  # unused attribute (tests/integration/test_api.py:387)
_.return_value  # unused attribute (tests/integration/test_api.py:407)
_.return_value  # unused attribute (tests/integration/test_api.py:432)
_.return_value  # unused attribute (tests/integration/test_api.py:458)
_.return_value  # unused attribute (tests/integration/test_api.py:482)
_.return_value  # unused attribute (tests/integration/test_api.py:495)
_.return_value  # unused attribute (tests/integration/test_api.py:510)
_.side_effect  # unused attribute (tests/integration/test_api.py:521)
_.return_value  # unused attribute (tests/integration/test_api.py:527)
_.return_value  # unused attribute (tests/integration/test_api.py:537)
_.return_value  # unused attribute (tests/integration/test_api.py:561)
_.return_value  # unused attribute (tests/integration/test_auth.py:33)
_.return_value  # unused attribute (tests/integration/test_auth.py:47)
_.return_value  # unused attribute (tests/integration/test_auth.py:49)
_.side_effect  # unused attribute (tests/integration/test_auth.py:79)
_.return_value  # unused attribute (tests/integration/test_auth.py:105)
_.return_value  # unused attribute (tests/integration/test_auth.py:107)
_.return_value  # unused attribute (tests/integration/test_authz.py:79)
_.return_value  # unused attribute (tests/integration/test_authz.py:102)
_.return_value  # unused attribute (tests/integration/test_authz.py:263)
_.return_value  # unused attribute (tests/integration/test_authz.py:287)
_.side_effect  # unused attribute (tests/integration/test_authz.py:315)
_.return_value  # unused attribute (tests/integration/test_authz.py:332)
_.return_value  # unused attribute (tests/integration/test_authz.py:348)
_.return_value  # unused attribute (tests/integration/test_authz.py:373)
_.return_value  # unused attribute (tests/integration/test_authz.py:398)
_.return_value  # unused attribute (tests/integration/test_authz.py:413)
_.return_value  # unused attribute (tests/integration/test_authz.py:428)
_.return_value  # unused attribute (tests/integration/test_authz.py:483)
_.return_value  # unused attribute (tests/integration/test_authz.py:484)
_.return_value  # unused attribute (tests/integration/test_authz.py:515)
_.return_value  # unused attribute (tests/integration/test_authz.py:540)
_.return_value  # unused attribute (tests/integration/test_authz.py:563)
_.return_value  # unused attribute (tests/integration/test_authz.py:564)
_.return_value  # unused attribute (tests/integration/test_authz.py:565)
_.return_value  # unused attribute (tests/integration/test_authz.py:590)
_.return_value  # unused attribute (tests/integration/test_authz.py:650)
_.return_value  # unused attribute (tests/integration/test_authz.py:852)
_.return_value  # unused attribute (tests/integration/test_authz.py:875)
_.return_value  # unused attribute (tests/integration/test_authz.py:910)
_.return_value  # unused attribute (tests/integration/test_authz.py:929)
_.return_value  # unused attribute (tests/integration/test_authz.py:939)
_.return_value  # unused attribute (tests/integration/test_vending_endpoint.py:24)
_.return_value  # unused attribute (tests/integration/test_vending_endpoint.py:35)
_.return_value  # unused attribute (tests/integration/test_vending_endpoint.py:60)
_.return_value  # unused attribute (tests/integration/test_vending_endpoint.py:67)
_.return_value  # unused attribute (tests/integration/test_vending_endpoint.py:80)
_.return_value  # unused attribute (tests/integration/test_vending_endpoint.py:82)
_.return_value  # unused attribute (tests/integration/test_vending_endpoint.py:97)
_.return_value  # unused attribute (tests/integration/test_warehouses.py:125)
_.dispatch  # unused method (tests/unit/test_body_limit.py:18)
_.side_effect  # unused attribute (tests/unit/test_warehouses.py:85)
_.side_effect  # unused attribute (tests/unit/test_warehouses.py:92)
