"""The annotator's closed audit vocabulary: every action it records on the ``lance.audit`` trail is one of these.

A client of the lakehouse owns its vocabulary, so the lakehouse's ``AuditAction`` names none of it.
"""

from __future__ import annotations

from enum import StrEnum


class AnnotatorAuditAction(StrEnum):
    """Every action the annotator records."""

    ANNOTATION_PROJECT_CREATE = "annotation_project.create"
    ANNOTATION_PROJECT_GET = "annotation_project.get"
    ANNOTATION_PROJECT_LIST = "annotation_project.list"
    ANNOTATION_PROJECT_MEMBERS_GRANT = "annotation_project.members.grant"
    ANNOTATION_PROJECT_MEMBERS_LIST = "annotation_project.members.list"
    ANNOTATION_PROJECT_MEMBERS_REVOKE = "annotation_project.members.revoke"
    ANNOTATION_PROJECT_UPDATE_ONTOLOGY = "annotation_project.update_ontology"
    ANNOTATOR_ASSIST = "annotator.assist"
    ANNOTATOR_AUTHORIZE = "annotator.authorize"
    PROJECT_ADJUDICATE = "project.adjudicate"
    PROJECT_DROP_TASK = "project.drop_task"
    PROJECT_LIST_TASKS = "project.list_tasks"
    PROJECT_PUBLISH = "project.publish"
    PROJECT_SEND = "project.send"
    PROJECT_TRANSITION = "project.transition"
    TASK_IMPORT = "task.import"
    TASK_SAVE_DRAFT = "task.save_draft"
    TASK_TRANSITION = "task.transition"
    TASK_VIEW = "task.view"
    TASK_VIEW_DRAFT = "task.view_draft"
