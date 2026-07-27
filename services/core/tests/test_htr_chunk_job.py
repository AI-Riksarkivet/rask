import importlib.util
from pathlib import Path
from types import ModuleType


_PATH = Path(__file__).resolve().parents[3] / "scripts/htr_chunk_job.py"


def _load_script(path: Path, name: str) -> ModuleType:
    """Import a `scripts/` file that isn't an importable package member.

    `spec_from_file_location` returns None for an unreadable/unrecognised path and
    a spec may carry no loader, so both are checked — a missing script then fails
    with a path-carrying ImportError instead of an opaque AttributeError.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {name} from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


m = _load_script(_PATH, "htr_chunk_job")


def test_bucket_strips_s3_scheme():
    assert m.bucket_name("s3://images-batch-alto") == "images-batch-alto"
    assert m.bucket_name("images-batch-alto") == "images-batch-alto"


def test_out_key_maps_jpg_to_xml_under_batch():
    assert m.out_key("008558342/008558342_00003.jpg") == "008558342/008558342_00003.xml"


def test_is_jpg():
    assert m.is_jpg("a/b.jpg") and m.is_jpg("a/b.JPG")
    assert not m.is_jpg("a/b.xml") and not m.is_jpg("a/")
