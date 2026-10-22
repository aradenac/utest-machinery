"""Minimal LLM provider interface for generation.

The production model path uses a single in-image Scaleway Chat Completions
request (no coding agent, CCR gateway, tool loop or model fallback).
The deterministic recorded provider remains available for offline tests.
Both implementations return planning JSON text; mtm.plan validates the plan.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .errors import Code, MtmError

#: Recorded mock responses ship as package data so CI and tests are reproducible
#: with no network and no credential.
RECORDINGS_DIR = Path(__file__).resolve().parent / "recordings"

#: Bumped whenever the generation prompt template changes materially, so a plan
#: records which prompt shaped it (``docs/architecture/data-contracts.md``).
PROMPT_VERSION = "plan-v2"


@dataclass(frozen=True)
class LLMIdentity:
    """Who produced a proposal: recorded, never a secret."""

    provider: str
    model: str
    prompt_version: str = PROMPT_VERSION

    def to_dict(self) -> dict:
        return {"provider": self.provider, "model": self.model,
                "prompt_version": self.prompt_version}


class LLMProvider(Protocol):
    """Minimal interface every generation model adapter implements."""

    identity: LLMIdentity

    def complete(self, prompt: str, *, timeout: int = 300) -> str:
        """Return the model's raw text response to *prompt*."""


class MockProvider:
    """A recorded-response provider: deterministic, offline, token-free.

    The recording is selected by matching the *fixture facts* present in the
    prompt (function names / requirement ids), so the same provider works for a
    fixture regardless of the sandbox path it is generated from.
    """

    def __init__(self, recordings_dir: Path | None = None):
        self.recordings_dir = recordings_dir or RECORDINGS_DIR
        self.identity = LLMIdentity(provider="mock", model="recorded-deepseek-v4.1-flash")
        self._records = self._load()

    def _load(self) -> list[dict]:
        records: list[dict] = []
        if not self.recordings_dir.is_dir():
            return records
        for path in sorted(self.recordings_dir.glob("*.json")):
            try:
                records.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        return records

    def complete(self, prompt: str, *, timeout: int = 300) -> str:
        best: dict | None = None
        best_score = 0
        for record in self._records:
            match = record.get("match", {})
            score = 0
            for name in match.get("functions", []):
                if name in prompt:
                    score += 1
            for req in match.get("requirements", []):
                if req in prompt:
                    score += 1
            if score > best_score:
                best, best_score = record, score
        if best is None or best_score == 0:
            raise MtmError(
                code=Code.LLM_CONFIG,
                message="No recorded mock response matches this input.",
                action="Use --provider scaleway for a real generation, or add a recording.",
                stage="llm-complete",
            )
        identity = best.get("identity", {})
        self.identity = LLMIdentity(
            provider="mock",
            model=identity.get("model", "recorded-deepseek-v4.1-flash"),
            prompt_version=identity.get("prompt_version", PROMPT_VERSION),
        )
        return json.dumps(best["response"])


class ScalewayProvider:
    """Single in-image direct-API completion, with token usage as evidence.

    The untrusted model sees only the inventory prompt through stdin, never
    project files. The in-image Python worker emits a JSON envelope with the
    proposed plan text and upstream-reported token counts (when available).
    """

    def __init__(self, argv: list[str], *, model: str,
                 workspace: Path | None = None,
                 extra_env: dict[str, str] | None = None, timeout: int = 300):
        self.argv = argv
        self.workspace = workspace
        self.extra_env = extra_env or {}
        self.timeout = timeout
        self.identity = LLMIdentity(provider="scaleway", model=model)
        self.usage: dict = {}
        self.returned_model: str = ""

    def complete(self, prompt: str, *, timeout: int | None = None) -> str:
        self.usage = {}
        self.returned_model = ""
        try:
            proc = subprocess.run(
                self.argv, input=prompt, capture_output=True, text=True,
                timeout=timeout or self.timeout, check=False,
                env={**os.environ, **self.extra_env},
                cwd=str(self.workspace) if self.workspace else None,
            )
        except subprocess.TimeoutExpired:
            raise MtmError(
                code=Code.LLM_NETWORK,
                message="The direct Scaleway completion timed out.",
                action="Check connectivity or the configured token/time budget.",
                stage="llm-complete", retryable=True,
            ) from None
        except OSError:
            raise MtmError(
                code=Code.LLM_CONFIG,
                message="Could not launch the in-image direct-API worker.",
                action="Check Docker availability and run 'mtm install'.",
                stage="llm-complete",
            ) from None
        if proc.returncode != 0:
            raise _classify(proc.stderr)
        try:
            result = json.loads(proc.stdout)
            text = result["content"]
            usage = result.get("usage", {})
            model = result.get("returned_model", "")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("empty response")
            if not isinstance(usage, dict):
                raise ValueError("invalid token usage")
            if not isinstance(model, str):
                raise ValueError("invalid model")
        except (ValueError, TypeError, KeyError, AttributeError):
            raise MtmError(
                code=Code.LLM_CONFIG,
                message="The direct Scaleway worker returned an invalid envelope.",
                action="Inspect the provider implementation; no plan was accepted.",
                stage="llm-complete",
            ) from None
        # The worker sanitizes metadata; do not store request contents or token.
        self.usage = {key: value for key, value in usage.items()
                      if key in ("prompt_tokens", "completion_tokens",
                                 "total_tokens", "reasoning_tokens",
                                 "cached_prompt_tokens")
                      and type(value) is int and value >= 0}
        self.returned_model = model
        return text


def make_provider(name: str, *, image_ref: str, credentials_file: Path | None = None,
                  model: str = "scaleway/deepseek-v4.1-flash",
                  recordings_dir: Path | None = None) -> LLMProvider:
    """Build the generation provider.

    ``mock`` returns a **recorded** response (offline, token-free) — used by fast
    PR CI and the unit tests, and only ever on an **explicit** request: a recorded
    fixture response must never be silently substituted for a real model call
    (B6). ``scaleway`` runs the in-image harness. ``auto`` prefers the real
    harness and, when it is unavailable, **fails with a clear action** instead of
    quietly returning recorded answers, so a user can never see a suite that
    looks model-generated but came from a canned fixture.
    """
    credentials_file = credentials_file or (
        Path.home() / ".config" / "utest-machinery" / "credentials.env")
    from .docker import image_exists

    if name == "mock":
        return MockProvider(recordings_dir)
    if name in ("auto", "scaleway"):
        if not image_exists(image_ref):
            raise MtmError(
                code=Code.LLM_CONFIG,
                message=f"The generation image {image_ref!r} is not built, so the "
                        "in-image model route is unavailable.",
                action="Run 'mtm install' to build the image, or pass "
                       "'--provider mock' to use a recorded response explicitly.",
                stage="llm-init",
            )
        if not credentials_file.is_file():
            raise MtmError(
                code=Code.LLM_AUTH,
                message=f"Credentials file not found: {credentials_file}",
                action="Create it with SCW_BASE_URL and SCW_BEARER_TOKEN (mode 600), "
                       "or use --provider mock explicitly.",
                stage="llm-init",
            )
        return _docker_provider(image_ref, credentials_file, model)
    raise MtmError(
        code=Code.CONFIG_INVALID,
        message=f"Unknown LLM provider {name!r}.",
        action="Use one of: auto, mock, scaleway.",
        stage="llm-init",
    )


def _docker_provider(image_ref: str, credentials_file: Path,
                     model: str) -> ScalewayProvider:
    """Run one direct HTTP completion in a transient, credential-scoped image.

    No project filesystem is mounted, and all non-secret runtime options are
    passed explicitly. The image itself contains no provider credentials.
    """
    argv = ["docker", "run", "--rm", "-i", "--entrypoint", "mtm-llm"]
    user = _invoking_user()
    if user:
        argv += ["--user", user]
    argv += ["--env-file", str(credentials_file),
             "--env", f"MTM_LLM_MODEL={model}"]
    for option in ("MTM_LLM_REASONING_EFFORT", "MTM_LLM_MAX_TOKENS"):
        if option in os.environ:
            argv += ["--env", f"{option}={os.environ[option]}"]
    argv.append(image_ref)
    return ScalewayProvider(argv, model=model)


def _invoking_user() -> str | None:
    try:
        if hasattr(os, "getuid"):
            return f"{os.getuid()}:{os.getgid()}"
    except OSError:  # pragma: no cover
        pass
    return None


def _classify(stderr: str) -> MtmError:
    """Translate sanitized in-image error codes without leaking upstream data."""
    message = stderr.strip()
    if message.startswith("LLM_AUTH:"):
        return MtmError(
            code=Code.LLM_AUTH, message="Scaleway rejected or lacks credentials.",
            action="Check SCW_BASE_URL and SCW_BEARER_TOKEN.",
            stage="llm-complete")
    if message.startswith("LLM_NETWORK:"):
        return MtmError(
            code=Code.LLM_NETWORK, message="The Scaleway API was unreachable or unavailable.",
            action="Retry after checking connectivity and provider status.",
            stage="llm-complete", retryable=True)
    return MtmError(
        code=Code.LLM_CONFIG, message="The direct Scaleway request failed.",
        action="Check the model route, output budget and provider configuration.",
        stage="llm-complete")
