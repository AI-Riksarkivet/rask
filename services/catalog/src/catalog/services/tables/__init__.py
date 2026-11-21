"""The table operations the catalog's doors run in-process through pylance, one module per job.

Where the native ``DirectoryNamespace`` stubs an operation (``refs``, ``schema``, ``writes``), its module resolves the
table's dataset through the namespace and performs the operation with pylance, returning the typed ``lance_namespace``
response model."""
