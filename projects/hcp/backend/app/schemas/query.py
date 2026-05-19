"""Pydantic models for the HCP Metadata Query API."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


# ── Request models ────────────────────────────────────────────────────


class ObjectQuery(BaseModel):
    """Parameters for an object-based metadata query."""

    model_config = ConfigDict(populate_by_name=True)

    query: str = Field(description="Query expression (Lucene syntax)")
    count: int = Field(default=100, description="Max results to return", alias="count")
    offset: int = Field(default=0, description="Starting offset")
    sort: Optional[str] = Field(
        default=None,
        description="Sort field (+/- prefix for asc/desc)",
        alias="sort",
    )
    verbose: bool = Field(default=False, description="Include full metadata")
    object_properties: Optional[list[str]] = Field(
        default=None,
        alias="objectProperties",
        description="Specific properties to return",
    )
    facets: Optional[list[str]] = Field(
        default=None, description="Facet fields to aggregate"
    )
    content_properties: Optional[bool] = Field(
        default=None,
        alias="contentProperties",
        description="Return available content property definitions",
    )


class LastResult(BaseModel):
    """Cursor for paging through operation-based query results."""

    model_config = ConfigDict(populate_by_name=True)

    url_name: str = Field(alias="urlName")
    change_time_milliseconds: str = Field(alias="changeTimeMilliseconds")
    version: Optional[str] = Field(default=None)


class OperationSystemMetadataTransactions(BaseModel):
    """Transaction type filter for operation queries."""

    model_config = ConfigDict(populate_by_name=True)

    transaction: Optional[list[str]] = Field(
        default=None, description="Transaction types: create, delete, purge, dispose"
    )


class ChangeTimeRange(BaseModel):
    """Time range filter for operation queries."""

    model_config = ConfigDict(populate_by_name=True)

    start: Optional[str] = Field(
        default=None, description="Start time (epoch ms or ISO 8601)"
    )
    end: Optional[str] = Field(
        default=None, description="End time (epoch ms or ISO 8601)"
    )


class NamespaceList(BaseModel):
    """Namespace filter wrapper for operation queries."""

    model_config = ConfigDict(populate_by_name=True)

    namespace: Optional[list[str]] = Field(
        default=None, description="List of namespace names"
    )


class DirectoryList(BaseModel):
    """Directory filter wrapper for operation queries."""

    model_config = ConfigDict(populate_by_name=True)

    directory: Optional[list[str]] = Field(
        default=None, description="List of directory paths"
    )


class OperationSystemMetadata(BaseModel):
    """System metadata filter for operation queries."""

    model_config = ConfigDict(populate_by_name=True)

    change_time: Optional[ChangeTimeRange] = Field(
        default=None,
        alias="changeTime",
        description="Time range filter",
    )
    directories: Optional[DirectoryList] = Field(
        default=None, description="Filter to specific directories"
    )
    indexable: Optional[bool] = Field(
        default=None, description="Filter by indexable status"
    )
    namespaces: Optional[NamespaceList] = Field(
        default=None, description="Filter to specific namespaces"
    )
    replication_collision: Optional[bool] = Field(
        default=None,
        alias="replicationCollision",
        description="Filter by replication collision status",
    )
    transactions: Optional[OperationSystemMetadataTransactions] = Field(
        default=None, description="Transaction type filters"
    )


class OperationQuery(BaseModel):
    """Parameters for an operation-based metadata query."""

    model_config = ConfigDict(populate_by_name=True)

    count: int = Field(default=100, description="Max results to return")
    last_result: Optional[LastResult] = Field(
        default=None,
        alias="lastResult",
        description="Cursor from previous page for paged queries",
    )
    object_properties: Optional[list[str]] = Field(
        default=None,
        alias="objectProperties",
        description="Specific properties to return",
    )
    verbose: bool = Field(default=False, description="Include full metadata")
    system_metadata: Optional[OperationSystemMetadata] = Field(
        default=None,
        alias="systemMetadata",
        description="System metadata filters",
    )


class ObjectQueryRequest(BaseModel):
    """Wire format: ``{"object": {...}}``."""

    model_config = ConfigDict(populate_by_name=True)

    object: ObjectQuery = Field(alias="object")


class OperationQueryRequest(BaseModel):
    """Wire format: ``{"operation": {...}}``."""

    model_config = ConfigDict(populate_by_name=True)

    operation: OperationQuery = Field(alias="operation")


# ── Response models ───────────────────────────────────────────────────


class QueryResultObject(BaseModel):
    """A single result entry from an object or operation query."""

    model_config = ConfigDict(populate_by_name=True)

    url_name: str = Field(alias="urlName")
    operation: str = Field(default="")
    change_time_milliseconds: Optional[str] = Field(
        default=None, alias="changeTimeMilliseconds"
    )
    change_time_string: Optional[str] = Field(default=None, alias="changeTimeString")
    version: Optional[int | str] = Field(default=None)
    namespace: Optional[str] = Field(default=None)
    utf8_name: Optional[str] = Field(default=None, alias="utf8Name")
    object_path: Optional[str] = Field(default=None, alias="objectPath")
    size: Optional[int] = Field(default=None)
    content_type: Optional[str] = Field(default=None, alias="contentType")
    hold: Optional[bool] = Field(default=None)
    shred: Optional[bool] = Field(default=None)
    dpl: Optional[int] = Field(default=None)
    retention: Optional[int | str] = Field(default=None)
    retention_string: Optional[str] = Field(default=None, alias="retentionString")
    retention_class: Optional[str] = Field(default=None, alias="retentionClass")
    hash_scheme: Optional[str] = Field(default=None, alias="hashScheme")
    hash_value: Optional[str] = Field(default=None, alias="hash")
    custom_metadata: Optional[bool] = Field(default=None, alias="customMetadata")
    custom_metadata_annotation: Optional[str] = Field(
        default=None, alias="customMetadataAnnotation"
    )
    acl: Optional[bool] = Field(default=None)
    replicated: Optional[bool] = Field(default=None)
    replication_collision: Optional[bool] = Field(
        default=None, alias="replicationCollision"
    )
    index: Optional[bool] = Field(default=None)
    ingest_time: Optional[int] = Field(default=None, alias="ingestTime")
    ingest_time_milliseconds: Optional[str] = Field(
        default=None, alias="ingestTimeMilliseconds"
    )
    ingest_time_string: Optional[str] = Field(default=None, alias="ingestTimeString")
    update_time: Optional[int] = Field(default=None, alias="updateTime")
    update_time_string: Optional[str] = Field(default=None, alias="updateTimeString")
    access_time: Optional[int] = Field(default=None, alias="accessTime")
    access_time_string: Optional[str] = Field(default=None, alias="accessTimeString")
    uid: Optional[int] = Field(default=None)
    gid: Optional[int] = Field(default=None)
    permissions: Optional[int | str] = Field(default=None)
    owner: Optional[str] = Field(default=None)
    type: Optional[str] = Field(default=None)


class QueryStatus(BaseModel):
    """Status block returned with every query response."""

    model_config = ConfigDict(populate_by_name=True)

    total_results: int = Field(alias="totalResults", default=0)
    results: int = Field(default=0)
    code: str = Field(default="COMPLETE")
    message: Optional[str] = Field(default=None)


class FacetFrequency(BaseModel):
    """A single facet value + count."""

    model_config = ConfigDict(populate_by_name=True)

    value: str
    count: int


class Facet(BaseModel):
    """A facet field with its frequency list."""

    model_config = ConfigDict(populate_by_name=True)

    name: str
    frequency: list[FacetFrequency] = Field(default_factory=list)


class ObjectQueryInfo(BaseModel):
    """The ``query`` block in an object-based query response."""

    model_config = ConfigDict(populate_by_name=True)

    expression: Optional[str] = Field(default=None)


class OperationQueryInfo(BaseModel):
    """The ``query`` block in an operation-based query response."""

    model_config = ConfigDict(populate_by_name=True)

    start: Optional[int | str] = Field(default=None)
    end: Optional[int | str] = Field(default=None)


class ObjectQueryResponse(BaseModel):
    """Response from an object metadata query."""

    model_config = ConfigDict(populate_by_name=True)

    query: Optional[ObjectQueryInfo] = Field(default=None)
    status: QueryStatus
    resultSet: list[QueryResultObject] = Field(default_factory=list, alias="resultSet")
    facets: Optional[list[Facet]] = Field(default=None)


class OperationQueryResponse(BaseModel):
    """Response from an operation metadata query."""

    model_config = ConfigDict(populate_by_name=True)

    query: Optional[OperationQueryInfo] = Field(default=None)
    status: QueryStatus
    resultSet: list[QueryResultObject] = Field(default_factory=list, alias="resultSet")
