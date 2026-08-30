"""Annotation projects — the domain core (slices S1/S3 of OPEN-WORK.md#design--annotation-projects).

Store-free by construction: nothing here touches the lakehouse plane, so the machines and the publish
schema are testable before any actor store exists.
"""

from annotator.projects.machines import (
    PROJECT_EDGES,
    SELF_REVIEW_FORBIDDEN,
    TASK_EDGES,
    IllegalTransition,
    UnreadableOntology,
    may_publish,
    project_transition,
    submit_target,
    task_transition,
)
from annotator.projects.models import (
    TERMINAL_TASK_STATES,
    AnnotationProject,
    Draft,
    ProjectState,
    PublishRecord,
    Shape,
    Task,
    TaskState,
    new_id,
)
from annotator.projects.ontology import (
    LabelClass,
    LabelOntology,
    OutputAttr,
    RelationClass,
    validate_against_ontology,
)


__all__ = [
    "PROJECT_EDGES",
    "SELF_REVIEW_FORBIDDEN",
    "TASK_EDGES",
    "TERMINAL_TASK_STATES",
    "AnnotationProject",
    "Draft",
    "IllegalTransition",
    "LabelClass",
    "LabelOntology",
    "OutputAttr",
    "ProjectState",
    "PublishRecord",
    "RelationClass",
    "Shape",
    "Task",
    "TaskState",
    "UnreadableOntology",
    "may_publish",
    "new_id",
    "project_transition",
    "submit_target",
    "task_transition",
    "validate_against_ontology",
]
