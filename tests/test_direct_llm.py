"""No-agent Scaleway transport: deterministic, network-free contract tests.

The transport is deliberately tested with a fake HTTP response. No live
credentials, DeepSeek inference or Docker service is needed in CI.
"""
from __future__ import annotations

import io
import json
import subprocess
from urllib.error import HTTPError

import pytest

from mtm import direct_llm
from mtm.errors import MtmError
from mtm.llm import ScalewayProvider, make_provider


class FakeHTTPResponse:
    def __init__(self, response):
        self.payload = json.dumps(response).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        return False

    def read(self, *unused):
        return self.payload


def _completion(content='{"scenarios": []}', finish="stop"):
    return {
        "model": "deepseek-v4.1-flash",
        "choices": [{"message": {"content": content}, "finish_reason": finish}],
        "usage": {
            "prompt_tokens": 120, "completion_tokens": 80,
            "total_tokens": 200,
            "completion_tokens_details": {"reasoning_tokens": 15},
            "prompt_tokens_details": {"cached_tokens": 12},
        },
    }


def test_direct_worker_sends_one_bounded_completion_with_json_mode(monkeypatch):
    sent = []

    def fake_open(req, timeout):
        sent.append((req, timeout))
        return FakeHTTPResponse(_completion())

    monkeypatch.setattr(direct_llm, "urlopen", fake_open)
    response = direct_llm.request_completion(
        "Generate a JSON test plan", base_url="https://api.scaleway.ai/v1",
        token="SENTINEL_PRIVATE", model="scaleway/deepseek-v4.1-flash")
    assert len(sent) == 1
    req, timeout = sent[0]
    assert req.full_url == "https://api.scaleway.ai/v1/chat/completions"
    assert timeout == 240
    assert req.get_header("Authorization") == "Bearer SENTINEL_PRIVATE"
    payload = json.loads(req.data)
    assert payload["model"] == "deepseek-v4.1-flash"
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["reasoning_effort"] == "low"
    assert payload["max_tokens"] == 8192
    assert payload["stream"] is False
    assert len(payload["messages"]) == 2
    assert "JSON" in payload["messages"][0]["content"]
    assert payload["messages"][1]["content"] == "Generate a JSON test plan"
    assert response["content"] == '{"scenarios": []}'
    assert response["usage"] == {
        "prompt_tokens": 120, "completion_tokens": 80,
        "total_tokens": 200, "reasoning_tokens": 15,
        "cached_prompt_tokens": 12,
    }
    assert "SENTINEL_PRIVATE" not in json.dumps(response)


def test_direct_worker_accepts_full_endpoint_and_no_reasoning(monkeypatch):
    seen = {}

    def fake_open(req, timeout):
        seen["request"] = json.loads(req.data)
        return FakeHTTPResponse(_completion())

    monkeypatch.setattr(direct_llm, "urlopen", fake_open)
    direct_llm.request_completion(
        "JSON plan", base_url="https://api.scaleway.ai/v1/chat/completions",
        token="private", model="deepseek-v4.1-flash",
        reasoning_effort="none", max_tokens=4096)
    assert seen["request"]["reasoning_effort"] == "none"
    assert seen["request"]["max_tokens"] == 4096


@pytest.mark.parametrize("base", [
    "http://example.org/v1", "https://user:pass@example.org/v1",
    "https://example.org/v1?token=secret", "not-a-url",
])
def test_direct_worker_rejects_unsafe_base_urls(base):
    with pytest.raises(direct_llm.DirectLLMError) as exc:
        direct_llm.request_completion(
            "JSON plan", base_url=base, token="private",
            model="deepseek-v4.1-flash")
    assert exc.value.code == "LLM_CONFIG"


@pytest.mark.parametrize("finish", ["length", "content_filter", None])
def test_direct_worker_refuses_incomplete_response(monkeypatch, finish):
    monkeypatch.setattr(
        direct_llm, "urlopen",
        lambda req, timeout: FakeHTTPResponse(_completion(finish=finish)))
    with pytest.raises(direct_llm.DirectLLMError) as exc:
        direct_llm.request_completion(
            "JSON plan", base_url="https://api.scaleway.ai/v1",
            token="private", model="deepseek-v4.1-flash")
    assert exc.value.code == "LLM_CONFIG"


@pytest.mark.parametrize("status,expected", [
    (401, "LLM_AUTH"), (403, "LLM_AUTH"),
    (429, "LLM_NETWORK"), (502, "LLM_NETWORK"), (400, "LLM_CONFIG"),
])
def test_http_error_classification_does_not_expose_upstream_body(
        monkeypatch, status, expected):
    def fail(req, timeout):
        raise HTTPError(req.full_url, status, "private upstream message",
                        hdrs={}, fp=io.BytesIO(b"BEARER_TOKEN_SECRET"))

    monkeypatch.setattr(direct_llm, "urlopen", fail)
    with pytest.raises(direct_llm.DirectLLMError) as exc:
        direct_llm.request_completion(
            "JSON plan", base_url="https://api.scaleway.ai/v1",
            token="private", model="deepseek-v4.1-flash")
    assert exc.value.code == expected
    assert "BEARER_TOKEN_SECRET" not in str(exc.value)
    assert "private upstream message" not in str(exc.value)


def test_api_response_without_usage_reports_no_fabricated_counters(monkeypatch):
    reply = _completion()
    del reply["usage"]
    monkeypatch.setattr(direct_llm, "urlopen",
                        lambda req, timeout: FakeHTTPResponse(reply))
    result = direct_llm.request_completion(
        "JSON plan", base_url="https://api.scaleway.ai/v1",
        token="private", model="deepseek-v4.1-flash")
    assert result["usage"] == {}


def test_scaleway_provider_extracts_raw_plan_and_usage(monkeypatch):
    seen = []
    plan = '{"scenarios": [{"function": "f"}]}'

    def fake_run(argv, **kw):
        seen.append((argv, kw))
        return subprocess.CompletedProcess(
            argv, 0,
            stdout=json.dumps({"content": plan,
                               "usage": {"prompt_tokens": 14, "total_tokens": 27},
                               "returned_model": "deepseek-v4.1-flash"}),
            stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    provider = ScalewayProvider(["docker", "run", "image"], model="scaleway/deepseek-v4.1-flash")
    assert provider.complete("JSON plan") == plan
    assert provider.usage == {"prompt_tokens": 14, "total_tokens": 27}
    assert provider.returned_model == "deepseek-v4.1-flash"
    assert len(seen) == 1
    assert seen[0][1]["input"] == "JSON plan"


def test_scaleway_provider_failure_never_accepts_stdout(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, **kw: subprocess.CompletedProcess(
            argv, 1, stdout='{"scenarios":[]}', stderr="LLM_NETWORK: HTTP 502"))
    provider = ScalewayProvider(["docker", "run", "image"], model="model")
    with pytest.raises(MtmError) as exc:
        provider.complete("JSON plan")
    assert exc.value.code == "LLM_NETWORK"


def test_scaleway_provider_rejects_invalid_envelope(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, **kw: subprocess.CompletedProcess(
            argv, 0, stdout='{"unexpected":"value"}', stderr=""))
    provider = ScalewayProvider(["docker", "run", "image"], model="model")
    with pytest.raises(MtmError) as exc:
        provider.complete("JSON plan")
    assert exc.value.code == "LLM_CONFIG"


def test_auto_selects_direct_worker_and_forwards_model(monkeypatch, tmp_path):
    from mtm import docker

    monkeypatch.setattr(docker, "image_exists", lambda _: True)
    creds = tmp_path / "credentials.env"
    creds.write_text("SCW_BASE_URL=https://api.scaleway.ai/v1\n"
                     "SCW_BEARER_TOKEN=secret\n")
    provider = make_provider(
        "auto", image_ref="utest-machinery/mtm:1.0.0",
        credentials_file=creds, model="scaleway/deepseek-v4.1-flash")
    assert isinstance(provider, ScalewayProvider)
    assert provider.identity.provider == "scaleway"
    assert "--entrypoint" in provider.argv
    assert "mtm-llm" in provider.argv
    assert "--env-file" in provider.argv
    assert "MTM_LLM_MODEL=scaleway/deepseek-v4.1-flash" in provider.argv
    assert "secret" not in repr(provider.argv)
    assert all("claude" not in part and "ccr" not in part
               for part in provider.argv)
