"""Annotation projects — the domain core (slices S1/S3 of OPEN-WORK.md#design--annotation-projects).

Store-free by construction: nothing here touches the lakehouse plane, so the machines and the publish
schema are testable before any actor store exists.
"""

from annotator.projects.machines import (
    PROJECT_EDGES,
    SELF_REVIEW_FORBIDDEN,
    TASK_EDGES,
    IllegalTransition,
    may_publish,
    project_transition,
    submit_target,
    task_transition,
)
from annotator.projects.models import (
    TERMINAL_TASK_STATES,
    AnnotationProject,
    Draft,
    LabelSchema,
    ProjectState,
    PublishRecord,
    Shape,
    Task,
    TaskState,
    new_id,
)


__all__ = [
    "PROJECT_EDGES",
    "SELF_REVIEW_FORBIDDEN",
    "TASK_EDGES",
    "TERMINAL_TASK_STATES",
    "AnnotationProject",
    "Draft",
    "IllegalTransition",
    "LabelSchema",
    "ProjectState",
    "PublishRecord",
    "Shape",
    "Task",
    "TaskState",
    "may_publish",
    "new_id",
    "project_transition",
    "submit_target",
    "task_transition",
]
