"""Tests for CLI commands — verify arg parsing and output formatting."""

import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

from typer.testing import CliRunner

from rahcp_cli.main import app

runner = CliRunner()


class FakeClient:
    """Fake HCPClient that skips HTTP entirely."""

    def __init__(self, **method_returns):
        from rahcp_client.bulk.protocol import TransferSettings

        self.token = "test-token"
        self.s3 = MagicMock()
        self.mapi = MagicMock()
        self.transfer_settings = TransferSettings(
            verify_ssl=False, timeout=30.0, multipart_threshold=100 * 1024 * 1024
        )
        for attr, value in method_returns.items():
            parts = attr.split(".")
            obj = self
            for part in parts[:-1]:
                obj = getattr(obj, part)
            setattr(obj, parts[-1], AsyncMock(return_value=value))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def request(self, *args, **kwargs):
        raise NotImplementedError("Use specific method mocks")


def _invoke(args, **method_returns):
    client = FakeClient(**method_returns)
    with (
        patch("rahcp_cli.s3.make_client", return_value=client),
        patch("rahcp_cli.namespace.make_client", return_value=client),
        patch("rahcp_cli.auth.make_client", return_value=client),
    ):
        result = runner.invoke(app, args)
    return result, client


# ── s3 ls (buckets) ────────────────────────────────────────────────


def test_s3_ls_buckets():
    result, _ = _invoke(
        ["s3", "ls"],
        **{"s3.list_buckets": {"buckets": [{"Name": "b1"}, {"Name": "b2"}]}},
    )
    assert result.exit_code == 0
    assert "b1" in result.output
    assert "b2" in result.output


def test_s3_ls_buckets_json():
    result, _ = _invoke(
        ["--json", "s3", "ls"],
        **{"s3.list_buckets": {"buckets": [{"Name": "b1"}]}},
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["buckets"][0]["Name"] == "b1"


# ── s3 ls BUCKET (objects) ─────────────────────────────────────────


def test_s3_ls_objects():
    result, _ = _invoke(
        ["s3", "ls", "mybucket"],
        **{"s3.list_objects": {"objects": [{"Key": "file.txt", "Size": 100}]}},
    )
    assert result.exit_code == 0
    assert "file.txt" in result.output


def test_s3_ls_objects_with_prefix():
    result, client = _invoke(
        ["s3", "ls", "mybucket", "--prefix", "data/"],
        **{"s3.list_objects": {"objects": []}},
    )
    assert result.exit_code == 0
    client.s3.list_objects.assert_called_once_with(
        "mybucket", "data/", max_keys=100, continuation_token=None, delimiter=None
    )


# ── s3 presign ──────────────────────────────────────────────────────


def test_s3_presign():
    result, _ = _invoke(
        ["s3", "presign", "b", "k"],
        **{"s3.presign_get": "https://signed-url"},
    )
    assert result.exit_code == 0
    assert "https://signed-url" in result.output


# ── ns list ─────────────────────────────────────────────────────────


def test_ns_list():
    result, _ = _invoke(
        ["ns", "list", "my-tenant"],
        **{"mapi.list_namespaces": {"name": ["ns1", "ns2"]}},
    )
    assert result.exit_code == 0
    assert "ns1" in result.output
    assert "ns2" in result.output


# ── ns get ──────────────────────────────────────────────────────────


def test_ns_get_json():
    result, _ = _invoke(
        ["--json", "ns", "get", "t", "ns1"],
        **{"mapi.get_namespace": {"name": "ns1", "hardQuota": "10 GB"}},
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["name"] == "ns1"


# ── ns export ───────────────────────────────────────────────────────


def test_ns_export():
    result, _ = _invoke(
        ["ns", "export", "t", "ns1"],
        **{
            "mapi.export_namespace": {"version": "1.0", "namespaces": [{"name": "ns1"}]}
        },
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["version"] == "1.0"


# ── s3 download-all ─────────────────────────────────────────────────


def _fake_download(dest_dir):
    """Create a mock download that writes a .tmp file so rename succeeds."""

    async def _download(bucket, key, tmp_path):
        tmp_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_bytes(b"x" * 5)
        return 5

    return _download


def test_s3_download_all(tmp_path):
    """Paginates, downloads files, skips folders and existing files."""
    client = FakeClient()
    page1 = {
        "objects": [
            {"Key": "data/", "Size": 0},
            {"Key": "data/a.txt", "Size": 5},
            {"Key": "data/b.txt", "Size": 3},
        ],
        "is_truncated": True,
        "next_continuation_token": "tok2",
    }
    page2 = {
        "objects": [{"Key": "data/c.txt", "Size": 4}],
        "is_truncated": False,
    }
    client.s3.list_objects = AsyncMock(side_effect=[page1, page2])
    client.s3.download = AsyncMock(side_effect=_fake_download(tmp_path))

    with (
        patch("rahcp_cli.s3.make_client", return_value=client),
        patch("rahcp_cli.namespace.make_client", return_value=client),
        patch("rahcp_cli.auth.make_client", return_value=client),
    ):
        tracker_db = str(tmp_path / ".tracker.db")
        result = runner.invoke(
            app,
            [
                "s3",
                "download-all",
                "mybucket",
                "-o",
                str(tmp_path),
                "--tracker-db",
                tracker_db,
            ],
        )
    assert result.exit_code == 0
    assert "Downloaded 3 files" in result.output
    assert client.s3.download.call_count == 3


def test_s3_download_all_skips_existing(tmp_path):
    """Files with matching size are skipped."""
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "exists.txt").write_text("hello")  # 5 bytes

    client = FakeClient()
    client.s3.list_objects = AsyncMock(
        return_value={
            "objects": [
                {"Key": "data/exists.txt", "Size": 5},
                {"Key": "data/new.txt", "Size": 3},
            ],
            "is_truncated": False,
        }
    )
    client.s3.download = AsyncMock(side_effect=_fake_download(tmp_path))

    with (
        patch("rahcp_cli.s3.make_client", return_value=client),
        patch("rahcp_cli.namespace.make_client", return_value=client),
        patch("rahcp_cli.auth.make_client", return_value=client),
    ):
        tracker_db = str(tmp_path / ".tracker.db")
        result = runner.invoke(
            app,
            [
                "s3",
                "download-all",
                "mybucket",
                "-o",
                str(tmp_path),
                "--tracker-db",
                tracker_db,
            ],
        )
    assert result.exit_code == 0
    assert "Downloaded 1 files" in result.output
    assert "skipped 1 existing" in result.output
    assert client.s3.download.call_count == 1


# ── auth whoami ─────────────────────────────────────────────────────


def test_auth_whoami():
    payload = (
        base64.urlsafe_b64encode(
            json.dumps({"sub": "testuser", "tenant": "t1"}).encode()
        )
        .decode()
        .rstrip("=")
    )
    client = FakeClient()
    client.token = f"header.{payload}.signature"
    with patch("rahcp_cli.auth.make_client", return_value=client):
        result = runner.invoke(app, ["auth", "whoami"])
    assert result.exit_code == 0
    assert "testuser" in result.output
    assert "t1" in result.output
