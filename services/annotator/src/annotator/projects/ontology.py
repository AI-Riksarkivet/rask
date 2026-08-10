"""The labeling task's ONE definition — taxonomy, tools, attributes and relations together.

WHY THIS REPLACES TWO OBJECTS. A project used to carry `label_schema` (classes, each with a
`colour` and a `shape_types` list) *and* `template` (a `kind`, an allowed `tools` list, a
`required_labels` list of bare strings, and typed `attributes`). Nothing cross-checked them, and
they disagreed in ways the estate then acted on:

* No validator existed between them, so `Shape kind = polygon` + `Task type = bbox-detection` was
  an accepted 201 — a project whose taxonomy says polygon and whose enforcement refuses every
  polygon at submit.
* The published table stamped its class list from `label_schema` while enforcement read `template`,
  so one lineage facet carried two independent class lists nothing kept in agreement.
* The closed-set property — the entire reason a class list is a first-class object in CVAT, Encord
  and SuperAnnotate — was absent: a submit could carry `label="asdf"` and publish it, because the
  send capture copied the template onto the task and NOT the taxonomy, so the class list was not
  even in scope where enforcement happens.

The fix is the property Label Studio actually has, without its transport: ONE document is the whole
task definition. We keep it as a structured Pydantic object rather than an XML DSL because in Label
Studio the semantics are NOT in the document — a tool is the tag CLASS name, hardcoded in the
implementation, so reproducing it means shipping ~60 tag classes to express what `ShapeType` already
says in six. A text projection can be generated from this model later; the reverse is not true.

`kind` IS NOT AN ENUM, deliberately. The old `TemplateKind` was a closed `Literal` of seven names
that enforced nothing — its only uses were interpolation into error strings and one table property,
so choosing `doc-qa` over `object-detection` changed no behaviour, and an eighth task type meant
editing Python. The verified position across the ecosystem is that a task-type name buys a shared
VOCABULARY, not a contract: Hugging Face's 57 pipeline ids exist (their own header says so) for
widget selection, endpoint routing and search facets, and only 31 of them carry any JSON Schema at
all. So `kind` is a free string, aligned with those ids by convention, and the enforceable contract
lives in the classes and relations below.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


#: The drawable primitives. A class declares which of these it may be drawn with, so "allowed tools"
#: is derived from the taxonomy rather than declared beside it where the two can disagree.
ShapeType = Literal["bbox", "polygon", "mask", "keypoint", "polyline", "segment", "tag", "text"]
MediaKind = Literal["image", "audio", "video", "text"]
AttrType = Literal["free", "int", "enum", "bool"]


class OutputAttr(BaseModel):
    """One typed field an annotation of its owning class must (or may) answer.

    `extra="forbid"` because every enforcement-bearing field here defaults PERMISSIVE: a typo in
    `required` would otherwise be dropped silently and the create still answer 201, leaving a
    project that advertises a contract it does not apply.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    type: AttrType = "free"
    #: `enum` only — the closed set of legal values. Ignored (and rejected) for other types, because
    #: silently accepting `choices` on a `free` field advertises a constraint nothing applies.
    choices: list[str] = Field(default_factory=list)
    required: bool = False

    @model_validator(mode="after")
    def _choices_belong_to_enums(self) -> OutputAttr:
        if self.choices and self.type != "enum":
            raise ValueError(f"attribute {self.name!r}: `choices` is meaningful only for type='enum', not {self.type!r}")
        if self.type == "enum" and not self.choices:
            raise ValueError(f"attribute {self.name!r}: type='enum' needs a non-empty `choices`")
        return self


class LabelClass(BaseModel):
    """One class in the taxonomy — and the ONLY place its tools and attributes are declared.

    `required` is EXPLICIT here, per class. It used to be a project-level `required_labels` list that
    the create dialog derived from "every class name", which silently made every class mandatory on
    every item the moment a third class was added. A taxonomy is an ALLOWED set; being mandatory is a
    separate, per-class property. None of the five researched products conflates them.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    #: Rendered by the canvas and the sidebar. Optional, but no longer silently dropped.
    colour: str | None = None
    #: Which primitives this class may be drawn with. Empty = any (an unconstrained class).
    tools: list[ShapeType] = Field(default_factory=list)
    #: This class's regions CARRY TRANSCRIBED TEXT (the row's `text` facet) — an OCR paragraph
    #: does, a detection box does not. Previously undeclarable: the workspace offered the text
    #: editor on every row of every task, so "is transcription an attribute?" had no answer in
    #: the model at all. It is neither a tool (a span INTO text is; this is text ON a region)
    #: nor an attribute (typed side-fields; this is the primary content column). Like `colour`,
    #: it configures the workspace rather than gating submit: the inspector offers transcription
    #: only where it is declared, once an ontology declares classes at all.
    transcribe: bool = False
    #: Per-class typed fields. Replaces the project-level attribute list, which could not say WHICH
    #: class an attribute belonged to.
    attributes: list[OutputAttr] = Field(default_factory=list)
    #: At-least-once: a completed item must carry this class on some shape.
    required: bool = False

    @model_validator(mode="after")
    def _attribute_names_are_unique(self) -> LabelClass:
        seen = [a.name for a in self.attributes]
        dupes = sorted({n for n in seen if seen.count(n) > 1})
        if dupes:
            raise ValueError(f"class {self.name!r}: duplicate attribute names {dupes}")
        return self


class RelationClass(BaseModel):
    """A typed LINK between two annotations — the structure KIE and DocVQA cannot be expressed without.

    Key information extraction is a key→value linking problem, and document VQA binds a question to
    the region that answers it. Neither is a per-shape property, so no amount of attribute typing
    reaches them: `Shape` had no concept of pointing at another shape at all. Declaring the legal
    endpoints here means a link can be validated the same way a label is — against a closed set.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    #: Class names permitted at each end. Empty = any declared class.
    from_classes: list[str] = Field(default_factory=list)
    to_classes: list[str] = Field(default_factory=list)
    #: A link is directed unless this says otherwise (key→value is directed; "same-entity" is not).
    directed: bool = True
    #: At-least-once: a completed item must carry at least one link of this kind.
    required: bool = False


class LabelOntology(BaseModel):
    """The whole task definition, as ONE object.

    There is no `enforce` boolean. It used to be a document-wide kill switch that voided explicitly
    authored declarations — a project could declare required labels, typed attributes and a tool
    restriction and have every one ignored because one flag was false. Here the ontology IS the
    contract: an ontology with no classes constrains nothing (which is exactly the pre-ontology
    behaviour), and every declaration that IS present is applied. Nothing is declared and ignored.
    """

    model_config = ConfigDict(extra="forbid")

    #: A shared VOCABULARY, not an enforced type — align with Hugging Face pipeline ids by
    #: convention (`object-detection`, `document-question-answering`, `token-classification`, …).
    #: Free-form on purpose: a new task type must not require editing Python and a redeploy.
    kind: str = Field(default="", max_length=64)
    modality: MediaKind = "image"
    classes: list[LabelClass] = Field(default_factory=list)
    relations: list[RelationClass] = Field(default_factory=list)
    #: An ontology with classes refuses an EMPTY submission by default: every other rule is a
    #: per-shape test, so zero shapes satisfies them all vacuously — claim, submit, done, without
    #: drawing anything. A blank page is a real archival outcome, so it stays expressible; it just
    #: has to be declared rather than fallen into.
    allow_empty: bool = False

    @model_validator(mode="after")
    def _internally_consistent(self) -> LabelOntology:
        """The cross-checks whose ABSENCE was the original defect.

        Two objects could contradict each other because nothing looked at both. One object still
        can, unless something looks at all of it — so that happens here, at construction, and the
        create endpoint gets it for free.
        """
        names = [c.name for c in self.classes]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate class names {dupes}")

        declared = set(names)
        for relation in self.relations:
            unknown = sorted((set(relation.from_classes) | set(relation.to_classes)) - declared)
            if unknown:
                raise ValueError(f"relation {relation.name!r} references undeclared classes {unknown}")

        rel_names = [r.name for r in self.relations]
        rel_dupes = sorted({n for n in rel_names if rel_names.count(n) > 1})
        if rel_dupes:
            raise ValueError(f"duplicate relation names {rel_dupes}")
        return self

    @property
    def constrains(self) -> bool:
        """True when this ontology says anything at all. Replaces the `enforce` flag."""
        return bool(self.classes or self.relations)

    @property
    def tools(self) -> list[ShapeType]:
        """The union of every class's tools — DERIVED, so it cannot disagree with the taxonomy.

        An empty list means unconstrained: either no class declared tools, or there are no classes.
        """
        if any(not c.tools for c in self.classes):
            return []
        out: list[ShapeType] = []
        for klass in self.classes:
            for tool in klass.tools:
                if tool not in out:
                    out.append(tool)
        return out

    def klass(self, name: str | None) -> LabelClass | None:
        return next((c for c in self.classes if c.name == name), None)


class ShapeLike(BaseModel):
    """The subset of `Shape` the validator reads.

    Declared structurally rather than importing `Shape`, so the ontology module stays free of the
    project models and can be exercised (and reused by the frontend's mirror) on its own.
    """

    model_config = ConfigDict(extra="ignore")

    shape_id: str = ""
    shape_type: str = ""
    label: str | None = None
    attributes: dict[str, str] = Field(default_factory=dict)
    #: The textual facet — set only on a text SPAN. See `_span_violation`.
    text: str | None = None
    parent_id: str | None = None
    char_start: int | None = None
    char_end: int | None = None


class LinkLike(BaseModel):
    """One directed link between two shapes, as submitted."""

    model_config = ConfigDict(extra="ignore")

    name: str = ""
    from_shape: str = ""
    to_shape: str = ""


def _span_violation(shapes: list[ShapeLike]) -> str | None:
    """A text SPAN must name a real parent and land inside its text.

    A `text` shape is not necessarily a span. It is also how a free-standing text annotation is
    expressed — a DocVQA question someone typed IS the text, not a range into something else — and
    demanding a parent from every `text` shape breaks a shape the model has always allowed (caught by
    the DocVQA test, not by review). So span-ness is DECLARED: a `parent_id`, a character range, or
    both. Declare either and all of it must be coherent; declare neither and this is ordinary text.

    Checked rather than trusted because a span is the one shape whose coordinates mean nothing on
    their own: `(12, 40)` is only a span if some other row's text is at least 40 characters long. A
    span past the end of its parent, or pointing at a parent not in the submission, publishes a range
    nothing can resolve — and unlike a bad box, nobody can SEE it is wrong by looking at the page.
    """
    by_id = {s.shape_id: s for s in shapes}
    for shape in shapes:
        if shape.shape_type != "text":
            continue
        declares = shape.parent_id is not None or shape.char_start is not None or shape.char_end is not None
        if not declares:
            continue
        if not shape.parent_id:
            return f"text span {shape.shape_id} declares a character range but names no parent — a span is a range INTO another annotation's text"
        parent = by_id.get(shape.parent_id)
        if parent is None:
            return f"text span {shape.shape_id} points at {shape.parent_id!r}, which is not in this submission"
        start, end = shape.char_start, shape.char_end
        if start is None or end is None:
            return f"text span {shape.shape_id} has no character range"
        if start < 0 or end <= start:
            return f"text span {shape.shape_id} has an empty or reversed range ({start}, {end})"
        if end > len(parent.text or ""):
            return f"text span {shape.shape_id} ends at {end}, past the end of {shape.parent_id} ({len(parent.text or '')} characters)"
    return None


def membership_violation(ontology: LabelOntology, shapes: list[ShapeLike]) -> str | None:
    """Is every shape a legal MEMBER of the taxonomy? The first violation, or None.

    The half of the output contract that asks "does this annotation belong to the declared
    vocabulary" — closed label set, the tools a class permits, and the types its attributes declare.
    Deliberately NOT the other half, which asks "is this submission complete" (every required class
    present, every required relation drawn).

    Split out because IMPORT needs exactly this half and must not have the other. An import is
    partial, unreviewed work by definition — refusing it because a required class has not been
    annotated yet would refuse the very thing importing is for, and the completeness rules still run
    at submit where they mean something.

    Shared rather than reimplemented so the two stages cannot drift: a membership rule that import
    applied more loosely than submit would accept labels at the door that are refused later, which
    is a worse failure than refusing them up front.
    """
    if not ontology.constrains:
        return None

    declared = {c.name: c for c in ontology.classes}

    for shape in shapes:
        # THE CLOSED SET. This is the check that could not exist before: the taxonomy was never
        # carried onto the task, so the one property a class list is FOR — that a label is a member
        # of it — went unenforced, and `label="asdf"` published.
        if shape.label is not None and shape.label != "" and shape.label not in declared:
            return f"label {shape.label!r} is not in the taxonomy {sorted(declared)} — shape {shape.shape_id}"

        klass = declared.get(shape.label or "")
        if klass is None:
            continue

        if klass.tools and shape.shape_type not in klass.tools:
            return f"class {klass.name!r} allows tools {sorted(klass.tools)} — shape {shape.shape_id} is {shape.shape_type}"

        # A class that DECLARES attributes closes the set — an undeclared name is a vocabulary
        # violation, same stance as `extra="forbid"` on the models: a typo'd `ordr` accepted
        # silently would publish beside a "requires attribute 'order'" refusal, and nothing
        # anywhere would say the two are the same mistake. A class declaring NO attributes stays
        # unconstrained, mirroring how empty `tools` means any tool.
        if klass.attributes:
            declared_attrs = {a.name for a in klass.attributes}
            for name in shape.attributes:
                if name not in declared_attrs:
                    return f"attribute {name!r} is not declared on class {klass.name!r} (declares {sorted(declared_attrs)}) — shape {shape.shape_id}"

        for attr in klass.attributes:
            value = shape.attributes.get(attr.name)
            if value is None or value == "":
                # `required` governs PRESENCE ONLY. The type rules below apply to whatever is
                # actually there — an optional `enum` still declares a closed set.
                if attr.required:
                    return f"class {klass.name!r} requires attribute {attr.name!r} — shape {shape.shape_id} lacks it"
                continue
            if attr.type == "int":
                try:
                    int(value)
                except ValueError:
                    return f"attribute {attr.name!r} must be an integer — shape {shape.shape_id} carries {value!r}"
            elif attr.type == "bool":
                if value.lower() not in {"true", "false", "1", "0"}:
                    return f"attribute {attr.name!r} must be a boolean — shape {shape.shape_id} carries {value!r}"
            elif attr.type == "enum" and value not in attr.choices:
                return f"attribute {attr.name!r} must be one of {attr.choices} — shape {shape.shape_id} carries {value!r}"
    return None


def validate_against_ontology(
    ontology: LabelOntology,
    shapes: list[ShapeLike],
    links: list[LinkLike] | None = None,
) -> str | None:
    """The output contract as ONE pure function: the first violation, or None.

    Pure and shared so the actor's refusal and any test speak the same words. Violations NAME the
    rule and the offender — a 409 nobody can act on is not enforcement.

    Order matters: the emptiness rule runs first because every rule after it is a per-shape or
    per-link test that zero shapes satisfies vacuously.
    """
    if not ontology.constrains:
        return None

    links = links or []

    if not shapes and not ontology.allow_empty:
        return f"task {ontology.kind or 'ontology'} is constrained — a submission must carry at least one annotation (set allow_empty to accept blank items)"

    membership = membership_violation(ontology, shapes)
    if membership is not None:
        return membership

    span_problem = _span_violation(shapes)
    if span_problem is not None:
        return span_problem

    present = {s.label for s in shapes if s.label}
    missing = [c.name for c in ontology.classes if c.required and c.name not in present]
    if missing:
        return f"classes {missing} are required — no submitted annotation carries them"

    by_id = {s.shape_id: s for s in shapes}
    for link in links:
        rel = next((r for r in ontology.relations if r.name == link.name), None)
        if rel is None:
            return f"relation {link.name!r} is not declared — known relations are {sorted(r.name for r in ontology.relations)}"
        for end, shape_id in (("from", link.from_shape), ("to", link.to_shape)):
            if shape_id not in by_id:
                return f"relation {rel.name!r} points at unknown shape {shape_id!r} ({end})"
        allowed_from, allowed_to = rel.from_classes, rel.to_classes
        src, dst = by_id[link.from_shape], by_id[link.to_shape]
        if allowed_from and src.label not in allowed_from:
            return f"relation {rel.name!r} allows {sorted(allowed_from)} at its source — shape {src.shape_id} is {src.label!r}"
        if allowed_to and dst.label not in allowed_to:
            return f"relation {rel.name!r} allows {sorted(allowed_to)} at its target — shape {dst.shape_id} is {dst.label!r}"

    linked = {link.name for link in links}
    missing_rels = [r.name for r in ontology.relations if r.required and r.name not in linked]
    if missing_rels:
        return f"relations {missing_rels} are required — the submission carries none"
    return None
