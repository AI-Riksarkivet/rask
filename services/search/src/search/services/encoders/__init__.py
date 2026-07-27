"""Vendored query-encoder clients for the vLLM model servers.

Copied (and trimmed) from ``packages/ratch/clients/`` because the backend may not
import the pipeline package (LANCE_MEDIA_MERGE §4.4): only the query-time
surface search needs — the bi-encoder embedding client, the cross-encoder
reranker, their shared HTTP transport, and the image preprocessing the
embedding server requires. The vendored copies carry no pipeline schema
constants; the expected embedding dimension arrives from the dataset
descriptor at construction time.
"""
