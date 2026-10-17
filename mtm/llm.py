"""LLM provider abstraction for generation.

The product's initial route is **in-image Claude Code + CCR → DeepSeek V4.1
Flash** (``docs/development/setup.md``, ``worklog/decisions/0004``). Generation
must therefore run the model *inside the container*, not from the host, and the
credential is injected only for that operation.

Two providers satisfy the same small interface:

* :class:`ClaudeCodeProvider` — runs the in-image ``claude`` CLI in
  non-interactive mode. CCR routes it to the configured DeepSeek model. The token
  is supplied at runtime (a narrowly scoped ``--env-file``); it never appears in
  an argv, an image layer or a log.
* :class:`MockProvider` — returns a **recorded** response for a fixture, so the
  whole pipeline (prompt → plan → generated suite) is reproducible offline with
  no token. This is what fast PR CI and the unit tests use.

The provider returns *text*; validating and grounding that text against the
deterministic inventory is :mod:`mtm.plan`'s job, never the model's claim.
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
                action="Use --provider claude-code for a real generation, or add a recording.",
                stage="llm-complete",
            )
        identity = best.get("identity", {})
        self.identity = LLMIdentity(
            provider="mock",
            model=identity.get("model", "recorded-deepseek-v4.1-flash"),
            prompt_version=identity.get("prompt_version", PROMPT_VERSION),
        )
        return json.dumps(best["response"])


class ClaudeCodeProvider:
    """Run the in-image Claude Code CLI, routed through CCR to DeepSeek.

    ``argv`` is the *complete* launcher, e.g.
    ``["docker", "run", "--rm", "--env-file", creds, image, "claude", "-p"]``.
    The prompt is written to stdin, so no prompt text ever lands in an argv or a
    process listing. Any recognised authentication/routing failure is mapped to
    the LLM error contract; the upstream token is never captured.
    """

    def __init__(self, argv: list[str], *, model: str, workspace: Path | None = None,
                 extra_env: dict[str, str] | None = None, timeout: int = 300):
        self.argv = argv
        self.model = model
        self.workspace = workspace
        self.extra_env = extra_env or {}
        self.timeout = timeout
        self.identity = LLMIdentity(provider="claude-code", model=model)

    def complete(self, prompt: str, *, timeout: int | None = None) -> str:
        env = {**os.environ, **self.extra_env}
        try:
            proc = subprocess.run(
                self.argv, input=prompt, capture_output=True, text=True,
                timeout=timeout or self.timeout, check=False, env=env,
                cwd=str(self.workspace) if self.workspace else None,
            )
        except subprocess.TimeoutExpired:
            raise MtmError(
                code=Code.LLM_NETWORK,
                message="The LLM call timed out.",
                action="Retry, or raise the budget if the model is slow.",
                stage="llm-complete", retryable=True,
            )
        except OSError as exc:
            raise MtmError(
                code=Code.LLM_CONFIG,
                message=f"Could not start the LLM harness: {exc}",
                action="Check that the image is built ('mtm install') and the harness is present.",
                stage="llm-complete",
            )
        text = proc.stdout.strip()
        if proc.returncode != 0 and not text:
            raise _classify(proc.stderr or proc.stdout)
        return text


def make_provider(name: str, *, image_ref: str, credentials_file: Path | None = None,
                  model: str = "scaleway/deepseek-v4.1-flash",
                  recordings_dir: Path | None = None) -> LLMProvider:
    """Build the generation provider.

    ``mock`` returns a **recorded** response (offline, token-free) — used by fast
    PR CI and the unit tests, and only ever on an **explicit** request: a recorded
    fixture response must never be silently substituted for a real model call
    (B6). ``claude-code`` runs the in-image harness. ``auto`` prefers the real
    harness and, when it is unavailable, **fails with a clear action** instead of
    quietly returning recorded answers, so a user can never see a suite that
    looks model-generated but came from a canned fixture.
    """
    credentials_file = credentials_file or (
        Path.home() / ".config" / "utest-machinery" / "credentials.env")
    from .docker import image_exists

    if name == "mock":
        return MockProvider(recordings_dir)
    if name in ("auto", "claude-code"):
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
        action="Use one of: auto, mock, claude-code.",
        stage="llm-init",
    )


def _docker_provider(image_ref: str, credentials_file: Path, model: str) -> "ClaudeCodeProvider":
    """The in-image Claude Code harness, run with a scoped --env-file mount.

    The bench is deliberately NOT mounted: the LLM step only turns the inventory
    text into a plan and needs no filesystem access, so it cannot write to inputs.
    """
    # ``-i`` keeps stdin open: the prompt is piped in, never passed as an argv.
    argv = ["docker", "run", "--rm", "-i", "--entrypoint", "mtm-llm"]
    user = _invoking_user()
    if user:
        argv += ["--user", user]
    argv += ["--env-file", str(credentials_file), image_ref]
    return ClaudeCodeProvider(argv, model=model)


def _invoking_user() -> str | None:
    try:
        if hasattr(os, "getuid"):
            return f"{os.getuid()}:{os.getgid()}"
    except OSError:  # pragma: no cover
        pass
    return None


def _classify(stderr: str) -> MtmError:
    """Map a harness failure to the documented LLM error contract (no secrets)."""
    low = stderr.lower()
    if "401" in low or "403" in low or "unauthor" in low or "auth" in low:
        return MtmError(
            code=Code.LLM_AUTH,
            message="The LLM upstream rejected the credential.",
            action="Verify SCW_BEARER_TOKEN in the credentials file and that it is current.",
            stage="llm-complete",
        )
    if "timeout" in low or "network" in low or "connect" in low or "resolve" in low:
        return MtmError(
            code=Code.LLM_NETWORK,
            message="The LLM upstream was unreachable.",
            action="Check outbound connectivity from the container and retry.",
            stage="llm-complete", retryable=True,
        )
    return MtmError(
        code=Code.LLM_CONFIG,
        message="The LLM harness failed.",
        action="Inspect 'mtm doctor' and the LLM logs; the route may be misconfigured.",
        stage="llm-complete",
    )
