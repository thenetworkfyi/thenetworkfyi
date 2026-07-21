from __future__ import annotations

import tomllib
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from packaging.requirements import Requirement


class _Decision(Enum):
    ALLOW = "allow"
    BLOCK = "block"
    HUMAN_IN_THE_LOOP_REQUIRED = "human_in_the_loop_required"


class _Message:
    def __init__(self, content: str):
        self.content = content


class _Tokenizer:
    def encode(self, text, *, add_special_tokens):
        tokens = text.split() if text else []
        return ["<s>", *tokens, "</s>"] if add_special_tokens else tokens

    def decode(
        self,
        token_ids,
        *,
        skip_special_tokens,
        clean_up_tokenization_spaces,
    ):
        return " ".join(token_ids)

    def num_special_tokens_to_add(self, *, pair):
        return 2


class _PromptGuard:
    def __init__(self):
        self.tokenizer = _Tokenizer()

    def _preprocess_text_for_promptguard(self, text):
        return text


class _Scanner:
    def __init__(self, decision_for=None, *, raw_reason="private raw email"):
        self.pg = _PromptGuard()
        self.decision_for = decision_for or (lambda _text: _Decision.ALLOW)
        self.raw_reason = raw_reason
        self.scanned = []

    async def scan(self, message):
        encoded = self.pg.tokenizer.encode(
            self.pg._preprocess_text_for_promptguard(message.content),
            add_special_tokens=True,
        )
        assert len(encoded) <= 512
        self.scanned.append(message.content)
        return SimpleNamespace(
            decision=self.decision_for(message.content),
            reason=self.raw_reason,
        )


def _enabled(scanner):
    return (
        patch(
            "thenetwork.security.content_scan.get_settings",
            return_value=SimpleNamespace(content_scan_enabled=True),
        ),
        patch(
            "thenetwork.security.content_scan._get_scanner",
            return_value=scanner,
        ),
        patch(
            "thenetwork.security.content_scan._get_llamafirewall_types",
            return_value=(_Decision, _Message),
        ),
    )


@pytest.mark.asyncio
async def test_enabled_scanner_error_fails_closed():
    from thenetwork.security import content_scan

    with (
        patch(
            "thenetwork.security.content_scan.get_settings",
            return_value=SimpleNamespace(content_scan_enabled=True),
        ),
        patch(
            "thenetwork.security.content_scan._get_scanner",
            side_effect=RuntimeError("model unavailable"),
        ),
    ):
        assert await content_scan.scan_content("hello") == (False, "scanner_error")


@pytest.mark.asyncio
async def test_disabled_scan_does_not_load_optional_dependency():
    from thenetwork.security import content_scan

    with (
        patch(
            "thenetwork.security.content_scan.get_settings",
            return_value=SimpleNamespace(content_scan_enabled=False),
        ),
        patch(
            "thenetwork.security.content_scan._build_scanner",
            side_effect=AssertionError("optional scanner must stay unloaded"),
        ),
        patch(
            "thenetwork.security.content_scan._get_llamafirewall_types",
            side_effect=AssertionError("optional types must stay unloaded"),
        ),
    ):
        assert await content_scan.scan_content("hello") == (True, "disabled")


@pytest.mark.asyncio
async def test_scanner_is_initialized_once(monkeypatch):
    from thenetwork.security import content_scan

    scanner = _Scanner()
    builds = 0

    def build():
        nonlocal builds
        builds += 1
        return scanner

    monkeypatch.setattr(content_scan, "_scanner", None)
    monkeypatch.setattr(content_scan, "_build_scanner", build)
    with (
        patch(
            "thenetwork.security.content_scan.get_settings",
            return_value=SimpleNamespace(content_scan_enabled=True),
        ),
        patch(
            "thenetwork.security.content_scan._get_llamafirewall_types",
            return_value=(_Decision, _Message),
        ),
    ):
        assert await content_scan.scan_content("first") == (True, "ok")
        assert await content_scan.scan_content("second") == (True, "ok")

    assert builds == 1


@pytest.mark.asyncio
async def test_injection_after_first_context_window_is_blocked():
    from thenetwork.security import content_scan

    scanner = _Scanner(
        lambda text: _Decision.BLOCK if "INJECTION" in text else _Decision.ALLOW
    )
    text = " ".join([*(f"benign-{i}" for i in range(600)), "INJECTION"])
    settings, get_scanner, types = _enabled(scanner)
    with settings, get_scanner, types:
        result = await content_scan.scan_content(text)

    assert result == (False, "prompt_injection_detected")
    assert len(scanner.scanned) >= 2
    assert "INJECTION" not in scanner.scanned[0]
    assert "INJECTION" in scanner.scanned[-1]


@pytest.mark.asyncio
async def test_benign_multi_window_mail_is_allowed():
    from thenetwork.security import content_scan

    scanner = _Scanner()
    text = " ".join(f"benign-{i}" for i in range(1_200))
    settings, get_scanner, types = _enabled(scanner)
    with settings, get_scanner, types:
        result = await content_scan.scan_content(text)

    assert result == (True, "ok")
    assert len(scanner.scanned) >= 3


@pytest.mark.asyncio
async def test_overlap_covers_an_injection_split_at_a_window_boundary():
    from thenetwork.security import content_scan

    tokens = [f"benign-{i}" for i in range(600)]
    tokens[509:511] = ["boundary-start", "boundary-end"]
    scanner = _Scanner(
        lambda text: (
            _Decision.BLOCK
            if "boundary-start boundary-end" in text
            else _Decision.ALLOW
        )
    )
    settings, get_scanner, types = _enabled(scanner)
    with settings, get_scanner, types:
        result = await content_scan.scan_content(" ".join(tokens))

    assert result == (False, "prompt_injection_detected")
    assert "boundary-start boundary-end" not in scanner.scanned[0]
    assert "boundary-start boundary-end" in scanner.scanned[1]


@pytest.mark.asyncio
async def test_raw_llamafirewall_reason_is_never_returned():
    from thenetwork.security import content_scan

    raw_reason = 'Full text: "private acquisition closes Friday"'
    scanner = _Scanner(lambda _text: _Decision.BLOCK, raw_reason=raw_reason)
    settings, get_scanner, types = _enabled(scanner)
    with settings, get_scanner, types:
        result = await content_scan.scan_content("private acquisition closes Friday")

    assert result == (False, "prompt_injection_detected")
    assert raw_reason not in repr(result)


@pytest.mark.asyncio
async def test_non_allow_non_block_decision_fails_closed():
    from thenetwork.security import content_scan

    scanner = _Scanner(lambda _text: _Decision.HUMAN_IN_THE_LOOP_REQUIRED)
    settings, get_scanner, types = _enabled(scanner)
    with settings, get_scanner, types:
        assert await content_scan.scan_content("hello") == (False, "scanner_error")


def test_disabled_startup_check_does_not_load_scanner():
    from thenetwork.security import content_scan

    with (
        patch(
            "thenetwork.security.content_scan.get_settings",
            return_value=SimpleNamespace(content_scan_enabled=False),
        ),
        patch(
            "thenetwork.security.content_scan._model_cache_path",
            side_effect=AssertionError(
                "disabled startup must not inspect a model cache"
            ),
        ),
        patch(
            "thenetwork.security.content_scan._get_scanner",
            side_effect=AssertionError("disabled startup must not load the scanner"),
        ),
    ):
        content_scan.assert_content_scanner_ready()


def test_content_scanner_has_one_runtime_switch_and_core_dependencies():
    root = Path(__file__).parents[1]
    with (root / "pyproject.toml").open("rb") as config_file:
        project = tomllib.load(config_file)["project"]

    dependency_names = {
        Requirement(dependency).name for dependency in project["dependencies"]
    }
    assert {"llamafirewall", "huggingface-hub"} <= dependency_names
    assert "content-scan" not in project.get("optional-dependencies", {})

    deployment_files = [
        root / "Dockerfile",
        root / "docker-compose.yml",
        root / ".env.example",
        root / ".github" / "workflows" / "publish.yml",
    ]
    deployment_config = "\n".join(
        path.read_text(encoding="utf-8") for path in deployment_files
    )
    assert "INSTALL_CONTENT_SCAN" not in deployment_config
    assert "CONTENT_SCAN_ENABLED" in deployment_config
    assert 'pyproject["project"]["dependencies"]' in deployment_config
    assert "HF_HOME: /home/appuser/.cache/huggingface" in deployment_config
    assert "hf-cache:/home/appuser/.cache/huggingface" in deployment_config


def test_enabled_startup_without_cache_or_token_fails_before_scanner_login(tmp_path):
    from thenetwork.security import content_scan

    with (
        patch(
            "thenetwork.security.content_scan.get_settings",
            return_value=SimpleNamespace(content_scan_enabled=True),
        ),
        patch(
            "thenetwork.security.content_scan._model_cache_path",
            return_value=tmp_path / "missing-model",
        ),
        patch(
            "thenetwork.security.content_scan._has_huggingface_token",
            return_value=False,
        ),
        patch("thenetwork.security.content_scan._get_scanner") as get_scanner,
    ):
        with pytest.raises(RuntimeError, match="cached Llama Prompt Guard 2 model"):
            content_scan.assert_content_scanner_ready()

    get_scanner.assert_not_called()


def test_enabled_startup_preloads_with_noninteractive_token(tmp_path):
    from thenetwork.security import content_scan

    scanner = _Scanner()
    scanner.pg.get_jailbreak_score = lambda *, text: 0.01
    with (
        patch(
            "thenetwork.security.content_scan.get_settings",
            return_value=SimpleNamespace(content_scan_enabled=True),
        ),
        patch(
            "thenetwork.security.content_scan._model_cache_path",
            return_value=tmp_path / "missing-model",
        ),
        patch(
            "thenetwork.security.content_scan._has_huggingface_token",
            return_value=True,
        ),
        patch(
            "thenetwork.security.content_scan._get_scanner", return_value=scanner
        ) as get_scanner,
    ):
        content_scan.assert_content_scanner_ready()

    get_scanner.assert_called_once_with()


def test_enabled_startup_uses_cache_without_huggingface_credentials(tmp_path):
    from thenetwork.security import content_scan

    model_cache = tmp_path / "cached-model"
    model_cache.mkdir()
    scanner = _Scanner()
    scanner.pg.get_jailbreak_score = lambda *, text: 0.01
    with (
        patch(
            "thenetwork.security.content_scan.get_settings",
            return_value=SimpleNamespace(content_scan_enabled=True),
        ),
        patch(
            "thenetwork.security.content_scan._model_cache_path",
            return_value=model_cache,
        ),
        patch(
            "thenetwork.security.content_scan._has_huggingface_token",
            side_effect=AssertionError("cached startup must not require credentials"),
        ),
        patch(
            "thenetwork.security.content_scan._get_scanner", return_value=scanner
        ) as get_scanner,
    ):
        content_scan.assert_content_scanner_ready()

    get_scanner.assert_called_once_with()


def test_enabled_startup_fails_when_readiness_inference_fails(tmp_path):
    from thenetwork.security import content_scan

    model_cache = tmp_path / "cached-model"
    model_cache.mkdir()
    scanner = _Scanner()
    scanner.pg.get_jailbreak_score = MagicMock(
        side_effect=RuntimeError("device unavailable")
    )
    with (
        patch(
            "thenetwork.security.content_scan.get_settings",
            return_value=SimpleNamespace(content_scan_enabled=True),
        ),
        patch(
            "thenetwork.security.content_scan._model_cache_path",
            return_value=model_cache,
        ),
        patch("thenetwork.security.content_scan._get_scanner", return_value=scanner),
    ):
        with pytest.raises(RuntimeError, match="failed startup preload"):
            content_scan.assert_content_scanner_ready()
