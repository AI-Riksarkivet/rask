---
title: Lance Namespace & Catalog (prose)
description: The upstream Lance Catalog and Namespace docs, vendored; spec.yaml is authoritative.
---

!!! warning "spec.yaml is the contract"
    Where this prose and [`ns_catalog/spec.yaml`](spec.md) differ (the REST paths among them), the spec wins. Cite the
    spec for a route.

!!! info "Vendored upstream pages"
    Every page under [`lance_docs/ns_catalog`](https://github.com/AI-Riksarkivet/rask/tree/main/lance_docs/ns_catalog), embedded verbatim in tree order
    (upstream: `lance-format/lance-namespace` `docs/src/`; the commit is in [Provenance](provenance.md)). Internal links and images may point at
    upstream paths outside this site.

--8<-- "lance_docs/ns_catalog/index.md"

--8<-- "lance_docs/ns_catalog/partitioning-spec.md"

--8<-- "lance_docs/ns_catalog/catalog/index.md"

--8<-- "lance_docs/ns_catalog/catalog/dir/index.md"

--8<-- "lance_docs/ns_catalog/catalog/rest/index.md"

--8<-- "lance_docs/ns_catalog/namespace/index.md"

--8<-- "lance_docs/ns_catalog/namespace/object-relationship.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/index.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/errors.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/AddColumnsEntry.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/AddVirtualColumnEntry.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/AddVirtualColumnOutputEntry.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/AlterColumnsEntry.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/AlterTableAddColumnsRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/AlterTableAddColumnsResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/AlterTableAlterColumnsRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/AlterTableAlterColumnsResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/AlterTableBackfillColumnsRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/AlterTableBackfillColumnsResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/AlterTableDropColumnsRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/AlterTableDropColumnsResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/AlterTransactionAction.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/AlterTransactionRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/AlterTransactionResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/AlterTransactionSetProperty.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/AlterTransactionSetStatus.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/AlterTransactionUnsetProperty.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/AlterVirtualColumnEntry.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/AnalyzeTableQueryPlanRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/AnalyzeTableQueryPlanResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/BatchCommitTablesRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/BatchCommitTablesResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/BatchCreateTableVersionsRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/BatchCreateTableVersionsResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/BatchDeleteTableVersionsRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/BatchDeleteTableVersionsResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/BooleanQuery.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/BoostQuery.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/BranchApi.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/BranchContents.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/CommitTableOperation.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/CommitTableResult.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/CountTableRowsRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/CountTableRowsResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/CreateMaterializedViewRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/CreateMaterializedViewResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/CreateNamespaceRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/CreateNamespaceResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/CreateTableBranchRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/CreateTableBranchResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/CreateTableIndexRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/CreateTableIndexResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/CreateTableRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/CreateTableResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/CreateTableScalarIndexResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/CreateTableTagRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/CreateTableTagResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/CreateTableVersionEntry.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/CreateTableVersionRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/CreateTableVersionResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/DeclareTableRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/DeclareTableResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/DeleteFromTableRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/DeleteFromTableResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/DeleteTableBranchRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/DeleteTableBranchResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/DeleteTableTagRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/DeleteTableTagResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/DeregisterTableRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/DeregisterTableResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/DescribeNamespaceRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/DescribeNamespaceResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/DescribeTableIndexStatsRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/DescribeTableIndexStatsResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/DescribeTableRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/DescribeTableResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/DescribeTableVersionRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/DescribeTableVersionResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/DescribeTransactionRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/DescribeTransactionResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/DropNamespaceRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/DropNamespaceResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/DropTableIndexRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/DropTableIndexResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/DropTableRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/DropTableResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/ErrorResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/ExplainTableQueryPlanRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/ExplainTableQueryPlanResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/FragmentStats.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/FragmentSummary.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/FtsQuery.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/GetTableStatsRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/GetTableStatsResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/GetTableTagVersionRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/GetTableTagVersionResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/Identity.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/IndexContent.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/InsertIntoTableRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/InsertIntoTableResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/JsonArrowDataType.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/JsonArrowField.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/JsonArrowSchema.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/ListNamespacesRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/ListNamespacesResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/ListTableBranchesRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/ListTableBranchesResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/ListTableIndicesRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/ListTableIndicesResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/ListTableTagsRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/ListTableTagsResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/ListTableVersionsRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/ListTableVersionsResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/ListTablesRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/ListTablesResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/MatchQuery.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/MaterializedViewApi.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/MaterializedViewUdtfEntry.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/MergeInsertIntoTableRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/MergeInsertIntoTableResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/MultiMatchQuery.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/NamespaceExistsRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/NamespaceExistsResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/PartitionField.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/PartitionSpec.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/PartitionTransform.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/PhraseQuery.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/QueryTableRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/QueryTableRequestColumns.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/QueryTableRequestFullTextQuery.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/QueryTableRequestVector.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/QueryTableResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/RefreshMaterializedViewRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/RefreshMaterializedViewResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/RegisterTableRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/RegisterTableResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/RenameTableRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/RenameTableResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/RestoreTableRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/RestoreTableResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/StringFtsQuery.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/StructuredFtsQuery.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/TableBasicStats.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/TableExistsRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/TableExistsResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/TableVersion.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/TagContents.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/UpdateFieldMetadataEntry.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/UpdateFieldMetadataRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/UpdateFieldMetadataResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/UpdateTableRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/UpdateTableResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/UpdateTableSchemaMetadataRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/UpdateTableSchemaMetadataResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/UpdateTableTagRequest.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/UpdateTableTagResponse.md"

--8<-- "lance_docs/ns_catalog/namespace/operations/models/VersionRange.md"

--8<-- "lance_docs/ns_catalog/namespace/supported-catalogs/index.md"

--8<-- "lance_docs/ns_catalog/namespace/supported-catalogs/lance-dir.md"

--8<-- "lance_docs/ns_catalog/namespace/supported-catalogs/lance-rest.md"

--8<-- "lance_docs/ns_catalog/namespace/supported-catalogs/template.md"
