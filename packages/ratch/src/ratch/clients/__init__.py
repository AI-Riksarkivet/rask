"""Model-inference clients — HTTP wrappers around the vLLM servers.

Each client encodes one capability (embed / rerank / caption / summarize) against
an OpenAI-style vLLM endpoint and shares the small :class:`~ratch.clients.base.VLLMTransport`
for connection pooling + concurrent fan-out. Import the submodule you need — the
package root stays import-light so FTS-only paths never pull httpx/numpy/Pillow.
"""
