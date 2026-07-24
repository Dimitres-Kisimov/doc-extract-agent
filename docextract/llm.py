"""Pluggable LLM provider interface for the extraction pipeline.

The pipeline is designed like a real agentic system: each stage asks a
*provider* to do the work. The default :class:`MockProvider` signals that no
model is available, so the pipeline falls back to its built-in deterministic
heuristics — which means the whole app runs offline with **no API key**.

Swapping in a real model is a one-line change: set ``DOCEXTRACT_PROVIDER=anthropic``
(and ``ANTHROPIC_API_KEY``) and :func:`get_provider` returns an
:class:`AnthropicProvider` that mirrors the same interface. The Anthropic SDK is
imported lazily inside the provider so it is never a hard dependency.

Example::

    from docextract.llm import get_provider

    provider = get_provider()          # MockProvider unless configured
    print(provider.name)               # "mock"
    print(provider.available)          # False -> pipeline uses heuristics
"""

from __future__ import annotations

import os
from typing import Any, Protocol


class LLMProvider(Protocol):
    """Structural interface every provider implements.

    A provider offers a single ``complete`` method plus two metadata
    attributes. The pipeline only *consults* a provider when
    :attr:`available` is ``True``; otherwise it uses deterministic parsing.
    """

    name: str
    available: bool

    def complete(self, prompt: str, *, system: str | None = None) -> str:
        """Return a completion for ``prompt`` (may raise if unavailable)."""
        ...


class MockProvider:
    """Deterministic no-op provider (the default).

    It reports ``available = False`` so the pipeline knows to rely on its
    built-in heuristic "agent". This keeps the demo fully offline while still
    exercising the provider-abstraction code path.
    """

    name = "mock"
    available = False

    def complete(self, prompt: str, *, system: str | None = None) -> str:
        # The mock never produces model output — the heuristic engine does the
        # real work. Returning an empty string documents that contract.
        return ""


class AnthropicProvider:
    """Provider backed by the Anthropic Messages API (stub, opt-in).

    This is a thin, production-shaped adapter. The ``anthropic`` SDK is imported
    lazily so it is only required when this provider is actually selected. In a
    real deployment each pipeline stage would send a structured-extraction
    prompt here; for this portfolio project the heuristic engine remains the
    reference implementation.
    """

    name = "anthropic"

    def __init__(
        self,
        *,
        model: str = "claude-opus-4-8",
        api_key: str | None = None,
    ) -> None:
        self.model = model
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self._client: Any = None
        self.available = bool(self._api_key)

    def _ensure_client(self) -> Any:
        """Lazily construct the SDK client on first use."""
        if self._client is None:
            try:
                import anthropic  # imported lazily — not a hard dependency
            except ImportError as exc:  # pragma: no cover - depends on env
                raise RuntimeError(
                    "AnthropicProvider requires the 'anthropic' package. "
                    "Install it with: pip install anthropic"
                ) from exc
            self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    def complete(self, prompt: str, *, system: str | None = None) -> str:
        """Send a single Messages API request and return the text response."""
        client = self._ensure_client()
        message = client.messages.create(  # pragma: no cover - needs network
            model=self.model,
            max_tokens=1024,
            system=system or "You extract structured data from business documents.",
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(
            block.text for block in message.content if block.type == "text"
        )


def get_provider(name: str | None = None) -> LLMProvider:
    """Return the configured provider.

    Selection order:

    1. Explicit ``name`` argument, if given.
    2. The ``DOCEXTRACT_PROVIDER`` environment variable.
    3. Default to the offline :class:`MockProvider`.

    The ``anthropic`` provider still falls back to mock behaviour inside the
    pipeline unless an API key is present, so this never breaks the offline demo.
    """
    selected = (name or os.environ.get("DOCEXTRACT_PROVIDER") or "mock").lower()
    if selected == "anthropic":
        return AnthropicProvider()
    return MockProvider()
