import importlib.util
from pathlib import Path


_PATH = Path(__file__).resolve().parents[4] / "components/scripts/htr_chunk_job.py"
_spec = importlib.util.spec_from_file_location("htr_chunk_job", _PATH)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)  # type: ignore[union-attr]


def test_bucket_strips_s3_scheme():
    assert m.bucket_name("s3://images-batch-alto") == "images-batch-alto"
    assert m.bucket_name("images-batch-alto") == "images-batch-alto"


def test_out_key_maps_jpg_to_xml_under_batch():
    assert m.out_key("008558342/008558342_00003.jpg") == "008558342/008558342_00003.xml"


def test_is_jpg():
    assert m.is_jpg("a/b.jpg") and m.is_jpg("a/b.JPG")
    assert not m.is_jpg("a/b.xml") and not m.is_jpg("a/")
