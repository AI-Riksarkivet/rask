"""The model-identity declaration, and the two invariants that keep it honest (#89).

The defect these guard: every weight load resolved Hugging Face ``main``, a MOVING pointer, so an
upstream push made the identical pipeline over the identical pages produce DIFFERENT transcriptions —
and the published ALTO named the software but not one model, so the difference was unobservable in
the artefact the archive hands out.
"""

import io
import pathlib
import re

import numpy as np
import pytest
from PIL import Image

from htr.models import LINE_MODEL, MODEL_REVISION, PIPELINE_MODELS, REGION_MODEL, TEXT_MODEL, ModelRef


SRC = pathlib.Path(__file__).resolve().parents[1] / "src"


def test_model_ref_stringifies_as_repo_at_revision():
    ref = ModelRef("text-recognition", "org/model", "abc123")
    assert str(ref) == "org/model@abc123"


def test_pipeline_models_default_to_the_process_revision():
    """A run cannot straddle two revisions: one env read, at import, shared by every ref."""
    assert {m.revision for m in PIPELINE_MODELS} == {MODEL_REVISION}
    assert [m.step for m in PIPELINE_MODELS] == ["region-segmentation", "line-segmentation", "text-recognition"]


def test_model_repos_appear_only_in_this_module():
    """The repo strings are declared ONCE.

    They were three facts held in six places — each actor's default AND `runner.pipeline`'s
    `fn_constructor_kwargs`. Duplication was survivable while nothing read them back; it stopped
    being survivable when the ALTO started RECORDING them, because a provenance record assembled
    from a second copy of a constant is only true while the copies agree and nothing was checking.
    """
    offenders = {path.relative_to(SRC).as_posix() for path in SRC.rglob("*.py") if path.name != "models.py" and "Riksarkivet/" in path.read_text()}
    assert offenders == set(), f"model repo literals escaped htr/models.py: {sorted(offenders)}"


@pytest.mark.parametrize("ref", PIPELINE_MODELS, ids=lambda r: r.step)
def test_every_model_is_named_in_the_published_alto(ref: ModelRef):
    """The archive's artefact says which weights read the page — repo AND revision."""
    from htr._columns import pack
    from htr.actors.alto_export import AltoExportActor

    buf = io.BytesIO()
    Image.new("RGB", (200, 100), color=(255, 255, 255)).save(buf, format="JPEG")
    out = AltoExportActor()(
        {
            "key": np.array(["A0060198/00012.jpg"], dtype=object),
            "image_bytes": np.array([buf.getvalue()], dtype=object),
            "regions": np.array([pack([])], dtype=object),
            "transcribed": np.array([pack([])], dtype=object),
        }
    )
    xml = out["alto_xml"][0].decode()
    assert f"<processingStepDescription>{ref.step}</processingStepDescription>" in xml
    # `repo@revision`, not the repo alone — the revision is the half that was silently moving.
    assert f"<processingStepSettings>model={ref.repo}@{MODEL_REVISION}</processingStepSettings>" in xml


def test_alto_processing_ids_stay_unique_and_ncname_safe():
    """`ID` is xsd:ID — a duplicate or a non-NCName makes the ALTO invalid, not merely ugly."""
    from htr._columns import pack
    from htr.actors.alto_export import AltoExportActor

    buf = io.BytesIO()
    Image.new("RGB", (200, 100), color=(255, 255, 255)).save(buf, format="JPEG")
    out = AltoExportActor()(
        {
            "key": np.array(["A0060198/00012.jpg"], dtype=object),
            "image_bytes": np.array([buf.getvalue()], dtype=object),
            "regions": np.array([pack([])], dtype=object),
            "transcribed": np.array([pack([])], dtype=object),
        }
    )
    ids = re.findall(r'<Processing ID="([^"]+)"', out["alto_xml"][0].decode())
    assert len(ids) == len(PIPELINE_MODELS) + 1  # + the pre-existing "general" software block
    assert len(set(ids)) == len(ids), f"duplicate xsd:ID in the ALTO: {ids}"
    for value in ids:
        assert re.fullmatch(r"[A-Za-z_][\w.-]*", value), f"not an NCName: {value}"


def test_no_models_means_no_block_rather_than_a_wrong_one():
    """A run whose models are unknown records nothing — silence is honest, a guess is not."""
    from htr.alto.serialize import serialize_alto
    from htr.schemas import PageImage, PageWithText

    buf = io.BytesIO()
    Image.new("RGB", (200, 100), color=(255, 255, 255)).save(buf, format="JPEG")
    page = PageWithText(page=PageImage(key="x/1.jpg", image_bytes=buf.getvalue()), region_lines=[])
    xml = serialize_alto(page, models=[])
    assert "processingStepSettings" not in xml
    assert '<Processing ID="general">' in xml  # the software block is unaffected


#: Every Hugging Face entry point that resolves a repo to BYTES. Each defaults to `main` when no
#: revision is given — which is the whole defect, and it is a default, so it fails silently forever.
_HF_LOADERS = frozenset({"hf_hub_download", "list_repo_files", "snapshot_download", "from_pretrained"})


def test_every_hugging_face_load_pins_a_revision():
    """No weight may be fetched from a moving pointer.

    A source-level guard rather than a behavioural one on purpose: the failure mode is a call site
    ADDED later without `revision=`, and by the time that shows up in behaviour the estate has already
    published transcriptions nobody can reproduce. This catches it at the edit.
    """
    import ast

    unpinned: list[str] = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name in _HF_LOADERS and not any(kw.arg == "revision" for kw in node.keywords):
                unpinned.append(f"{path.relative_to(SRC).as_posix()}:{node.lineno} {name}(...)")
    assert unpinned == [], "Hugging Face load without revision= (resolves the moving `main`):\n  " + "\n  ".join(unpinned)


def test_actors_default_to_the_declared_repos():
    """The load sites and the record read the same constants — checked, not assumed."""
    import inspect

    from htr.actors.layout import LayoutActor
    from htr.actors.lines import LineActor
    from htr.actors.transcription import TranscribeActor

    for actor, expected in ((LayoutActor, REGION_MODEL), (LineActor, LINE_MODEL), (TranscribeActor, TEXT_MODEL)):
        assert inspect.signature(actor.__init__).parameters["model"].default == expected.repo


# --- the /htrflow lane ----------------------------------------------------------------------------
# The second pipeline shape (`Segmentation -> Segmentation -> TextRecognition` collapsed into ONE
# Serve replica) declares its models in `htrflow_pipeline.yaml`, in htrflow's own config format. It
# had the identical unpinned-`main` defect and is easy to miss, because the `Riksarkivet/` guard above
# only reads `.py`.


def _yaml_model_settings() -> list[dict]:
    import yaml

    from runner.htrflow_service import PIPELINE_YAML

    with PIPELINE_YAML.open() as handle:
        config = yaml.safe_load(handle)
    return [s["settings"]["model_settings"] for s in config["steps"] if "model_settings" in s.get("settings", {})]


def test_htrflow_yaml_declares_the_same_models():
    """Both lanes read the same pages; they must not read them with different weights."""
    assert {s["model"] for s in _yaml_model_settings()} == {m.repo for m in PIPELINE_MODELS}


def test_htrflow_pipeline_config_pins_every_model():
    """The YAML cannot interpolate an env var, so the pin is injected at load — verify it lands."""
    from runner.htrflow_service import pinned_pipeline_config

    settings = [s["settings"]["model_settings"] for s in pinned_pipeline_config()["steps"] if "model_settings" in s.get("settings", {})]
    assert settings, "no model_settings found — the YAML shape changed and the pin now injects nothing"
    assert all(s.get("revision") == MODEL_REVISION for s in settings), settings
    # And the on-disk YAML is untouched: the pin is a load-time overlay, not a file rewrite.
    assert all("revision" not in s for s in _yaml_model_settings())


def test_pinned_config_reaches_htrflow_in_the_form_it_accepts():
    """`Pipeline.from_config` takes a PATH and `open()`s it.

    The first cut of this pin handed it the config DICT. Every assertion about the dict passed, `ty`
    flagged the argument type, and the deployment would have raised `TypeError` in every `/htrflow`
    replica's `__init__` at startup. So the guard is on the SEAM, not the dict: what the deployment
    passes must be something `open()` accepts, and reloading it must still show the pin.
    """
    import yaml

    from runner.htrflow_service import pinned_pipeline_path

    with pinned_pipeline_path() as config_path:
        assert isinstance(config_path, str)
        with open(config_path) as handle:  # exactly what htrflow does with it
            reloaded = yaml.safe_load(handle)
        written = pathlib.Path(config_path)
        assert written.exists()
    revisions = {s["settings"]["model_settings"].get("revision") for s in reloaded["steps"] if "model_settings" in s.get("settings", {})}
    assert revisions == {MODEL_REVISION}
    assert not written.exists(), "the temp config outlived the context manager"


def test_htrflow_deployment_is_still_a_serve_deployment():
    """`@serve.deployment` must decorate the CLASS.

    Guarding a mistake that was actually made while pinning this lane: inserting
    `pinned_pipeline_config` directly above `class HTRFlowDeployment` slipped it BETWEEN the decorator
    and the class, so the decorator wrapped the helper and the deployment silently became a plain
    class. Nothing in the suite touched `HTRFlowDeployment`, so all 38 tests stayed green.
    """
    from ray.serve.deployment import Deployment

    from runner.htrflow_service import HTRFlowDeployment, pinned_pipeline_config

    assert isinstance(HTRFlowDeployment, Deployment)
    assert callable(pinned_pipeline_config) and not isinstance(pinned_pipeline_config, Deployment)
