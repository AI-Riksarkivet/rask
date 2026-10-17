"""A bring-your-own pipeline, end to end on the live estate ([[LH-478]], ADRs 0154 to 0158 and 0161).

The lakehouse runs no pipeline. A user's job, on its own compute, writes every tier through the catalog's Lance
Namespace doors with a stock client, follows the commit feed to learn what to do next, and publishes through the
tag gate. That job is `byo_scenario.drive`, run once per session on a scratch tenant (`conftest.py`'s `byo_run`), so
this module and every `phase1` criterion module read one run of it. This module holds the scenario's own claims: the
project's gate holds a breaching version behind `published`, where readers of `@published` keep the previous version,
until a validator approves; the insert's feed row names the bronze input and the job the pipeline named in the spec
Context; and a follower reads silver's rows in seq order up to a cursor that answers itself. What each commit carries
and the edges lineage records for it are criterion 1's claims (`test_provenance_is_correct.py`); which commits the feed
holds, and who is told of each, are criterion 4's (`test_events_are_correct.py`).

A second leg holds the write tier's vended credential and tries to forge the `published` tag in the store itself.

The scenario's own claims are part of Phase 1's acceptance (`phase1`, XC-090), beside the criteria that read its drive.
Run with `bash scripts/e2e_live.sh tests/e2e-py/test_a_bring_your_own_pipeline_runs_end_to_end.py`, which discovers
every address from the release, or `make e2e-byo-pipeline` with the addresses set.
"""

from __future__ import annotations

import json
import uuid

import boto3
import byo_scenario as scenario
import lance
import lance_namespace
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError
from byo_scenario import ScenarioRun, Tenant


pytestmark = [pytest.mark.e2e, pytest.mark.byo_pipeline, pytest.mark.phase1, pytest.mark.skipif(not scenario.LIVE, reason=scenario.REQUIRES)]


def test_a_pipeline_seeds_bronze_derives_silver_and_its_breach_waits_for_a_validator(byo_tenant: Tenant, byo_run: ScenarioRun) -> None:
    run = byo_run
    first, held, approved = run.first_publish, run.held_publish, run.approval
    (insert,) = [row for row in run.silver_rows if scenario.lance_facet(row).get("operation") == "insert"]

    assert (first["published"], first["held"]) == (True, False), first
    assert (held["published"], held["held"], held["hold"]["reasons"]) == (False, True, ["row_count_delta"]), held
    assert run.published_rows_while_held == 8, "a reader of @published saw the held version"
    assert run.stock_rows_while_held == 24
    assert (approved["published_version"], approved["hold"]["decided_by"]) == (2, byo_tenant.validator.principal), approved
    assert run.published_rows_after_approval == 24
    assert [int(row["seq"]) for row in run.silver_rows] == sorted(int(row["seq"]) for row in run.silver_rows)
    assert (insert["inputs"], scenario.lance_facet(insert)["job"]) == ([run.bronze_table], "byo-silver"), insert
    assert (run.caught_up["events"], run.caught_up["next_cursor"]) == ([], run.cursor), run.caught_up


def test_the_write_tier_credential_cannot_forge_the_published_tag(byo_tenant: Tenant) -> None:
    """The pipeline owns its table, so `DescribeTable` vends it the write tier. That credential reads the table's
    prefix and puts under `data/`, and the store refuses its PUT of `_refs/tags/published.json`: only the catalog
    moves a tag."""
    table_id = [byo_tenant.bronze, "forge"]
    stock = byo_tenant.pipeline.namespace()
    lance.write_dataset(
        scenario.rows_of(range(2)), namespace_client=stock, table_id=table_id, mode="create", data_storage_version="2.2", enable_stable_row_ids=True
    )
    byo_tenant.tables.append("$".join(table_id))
    described = stock.describe_table(lance_namespace.DescribeTableRequest(id=table_id))
    vended = described.storage_options or {}
    bucket, _, prefix = str(described.location).removeprefix("s3://").partition("/")
    store = boto3.client(
        "s3",
        endpoint_url=vended["endpoint"],
        aws_access_key_id=vended["aws_access_key_id"],
        aws_secret_access_key=vended["aws_secret_access_key"],
        aws_session_token=vended["aws_session_token"],
        region_name=vended.get("region", "us-east-1"),
        config=Config(s3={"addressing_style": "path"}),
    )
    listed = store.list_objects_v2(Bucket=bucket, Prefix=f"{prefix}/_versions/")
    # The write tier's own allowance, so the refusal below is that tier's Deny on `_refs/`, not a read-only credential.
    store.put_object(Bucket=bucket, Key=f"{prefix}/data/forge-control-{uuid.uuid4().hex}.bin", Body=b"control")

    with pytest.raises(ClientError) as refused:
        store.put_object(Bucket=bucket, Key=f"{prefix}/_refs/tags/published.json", Body=json.dumps({"version": 1, "branch": None}).encode())

    assert listed["KeyCount"] >= 1, "the vended credential could not read the table it was vended for"
    assert refused.value.response["Error"]["Code"] == "AccessDenied", refused.value.response
    assert "published" not in lance.dataset(namespace_client=stock, table_id=table_id).tags.list()
