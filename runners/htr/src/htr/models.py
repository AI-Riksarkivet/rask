"""The model identities an HTR run loads — declared ONCE, read by everything that needs them.

Three consumers, and the reason this module exists is that they must never disagree:

1. **The actors**, which load the weights (`layout`, `lines`, `transcription`, `transcribe_service`).
2. **`runner.pipeline`**, which constructs those actors — it used to repeat the same two repo strings
   as `fn_constructor_kwargs` literals, so the pipeline and the actor default were two copies of one
   fact.
3. **The ALTO serializer**, which now RECORDS them in the published XML.

(3) is why the duplication had to go rather than merely being tidied. A provenance record assembled
from a second copy of a constant is only true while the copies agree, and nothing was checking. The
model strings now appear exactly once in the source, pinned by
`tests/test_models.py::test_model_repos_appear_only_in_this_module`.

REVISION. `main` is a MOVING POINTER: an upstream push changes what every one of those loads
resolves to, so the identical pipeline over the identical pages yields DIFFERENT transcriptions
while the record names the same model. The estate publishes machine-generated readings of historical
records; a reading nobody can reproduce is the provenance claim that matters most. The default stays
`main` deliberately — pinning a sha here would be a guess about which weights the estate wants, and
a wrong pin is worse than an honest moving one. What matters is that the pin is now EXPRESSIBLE
(`RASK_HTR_MODEL_REVISION`) and that whatever it resolves to is written into the ALTO.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


#: Resolved ONCE per process, at import. A run therefore cannot straddle two revisions, and the
#: value the ALTO records is by construction the value every actor in that run loaded with.
MODEL_REVISION = os.environ.get("RASK_HTR_MODEL_REVISION", "main")

#: The runner build's git commit, baked into the image env at build time (#88). Empty when unset —
#: and an empty sha renders NO provenance block rather than a placeholder, for the same reason
#: ``serialize_alto(models=[])`` renders none: silence is honest, a guess is not. Together with
#: ``MODEL_REVISION`` this closes the "from the run" story: the ALTO names the weights it loaded
#: AND the code that ran them, so a published transcription is reproducible from its own record.
#: `unknown` is scripts/dagger-image.sh's git-less fallback for VCS_REF — treating it as a sha
#: would stamp `commit=unknown` into published ALTO, the exact placeholder lie the empty case
#: refuses. Normalised here, at the ONE place the value enters, so neither lane can stamp it.
_RAW_SHA = os.environ.get("RASK_GIT_SHA", "")
COMMIT_SHA = "" if _RAW_SHA == "unknown" else _RAW_SHA


@dataclass(frozen=True, slots=True)
class ModelRef:
    """One model, and the processing step it performs.

    ``step`` is the ALTO ``processingStepDescription`` — a reader's name for what this model DID,
    not an implementation detail. It is also spliced into an xsd:ID, so keep it an NCName-safe slug.
    """

    step: str
    repo: str
    revision: str = MODEL_REVISION

    def __str__(self) -> str:
        """``repo@revision`` — the form Hugging Face itself uses, and what lands in the ALTO."""
        return f"{self.repo}@{self.revision}"


REGION_MODEL = ModelRef("region-segmentation", "Riksarkivet/yolov9-regions-1")
LINE_MODEL = ModelRef("line-segmentation", "Riksarkivet/yolov9-lines-within-regions-1")
TEXT_MODEL = ModelRef("text-recognition", "Riksarkivet/trocr-base-handwritten-hist-swe-2")

#: The actor-per-stage pipeline's models, in the order a page passes through them. This is what the
#: ALTO records by default. `/htrflow` collapses the same three into one Serve deployment — same
#: models, same refs, so the same provenance holds.
PIPELINE_MODELS: tuple[ModelRef, ...] = (REGION_MODEL, LINE_MODEL, TEXT_MODEL)
