"""The controlplane's two shapes: the Project CR coming IN, and the DTO going OUT.

They are deliberately separate models. The CR is the `platform.rask.io/v1alpha1` custom resource that
`rask-operator` — a SEPARATE repo, versioned independently of this one — publishes into the cluster;
the DTO is this service's own frozen response contract. Collapsing them would tie the API the home
zone reads to a CRD nobody here owns.
"""

from pydantic import BaseModel, ConfigDict, Field


class ProjectDTO(BaseModel):
    slug: str
    name: str
    team: str
    workload: str
    phase: str
    namespace: str
    url: str
    created_at: str


class ProjectsResponse(BaseModel):
    projects: list[ProjectDTO]


#: Every CR sub-model ignores members it does not name. A CRD grows fields between releases, and a
#: controlplane that refused a Project because the operator added a status condition would fail
#: precisely when the estate was being upgraded.
_CR = ConfigDict(extra="ignore")


class ProjectWorkload(BaseModel):
    """`spec.workload`. The platform knows no workload NAMES — this is an opaque label the operator
    stamped and this service forwards."""

    model_config = _CR

    type: str = ""


class ProjectMeta(BaseModel):
    model_config = _CR

    name: str = ""
    #: The k8s field name, kept verbatim as the alias so the CR validates as it arrives.
    creation_timestamp: str = Field(default="", alias="creationTimestamp")


class ProjectSpec(BaseModel):
    model_config = _CR

    team: str = ""
    #: Absent on a CR that declares no workload — `None`, not an empty object, so "not stated" and
    #: "stated as empty" stay distinguishable to anything that later cares.
    workload: ProjectWorkload | None = None


class ProjectStatus(BaseModel):
    model_config = _CR

    #: The operator writes this once it has reconciled; until then the CR has no status at all.
    phase: str = ""
    namespace: str = ""


class ProjectCR(BaseModel):
    """One `platform.rask.io/v1alpha1` Project, VALIDATED at the boundary.

    This exists because the mapping used to walk the raw dict with eight `.get(..., "")` calls and
    the chain `(spec.get("workload") or {}).get("type", "")`. A CR one schema revision away from what
    those chains assumed — a scalar `workload`, a null `metadata` — did not produce a bad DTO, it
    raised `AttributeError` from inside the mapper, which the route deliberately does not catch. The
    caller got a bare 500 and the log got a traceback naming `service.py` rather than the CR. Every
    default below reproduces exactly what the corresponding `.get()` returned, so nothing the old
    chain accepted is refused now; what changes is that a shape it could NOT accept is named.
    """

    model_config = _CR

    metadata: ProjectMeta = Field(default_factory=ProjectMeta)
    spec: ProjectSpec = Field(default_factory=ProjectSpec)
    #: Absent until the operator reconciles the CR — the "Pending" case.
    status: ProjectStatus | None = None

    @property
    def workload_type(self) -> str:
        return self.spec.workload.type if self.spec.workload is not None else ""

    @property
    def phase(self) -> str:
        """`Pending` covers both "no status yet" and "status with an empty phase"."""
        return (self.status.phase if self.status is not None else "") or "Pending"

    @property
    def reported_namespace(self) -> str:
        """What the operator SAYS the namespace is — empty until it has reconciled."""
        return self.status.namespace if self.status is not None else ""

    @property
    def lookup_namespace(self) -> str:
        """Where to LOOK for this project's Ingress, which is not the same question.

        Falls back to the operator's naming convention while `status.namespace` is still empty, so a
        project mid-provision can still resolve its host. Deliberately not merged with
        `reported_namespace`: that one is what the API reports, and reporting a guess as fact is how
        a half-provisioned project reads as finished.
        """
        return self.reported_namespace or (f"project-{self.metadata.name}" if self.metadata.name else "")
