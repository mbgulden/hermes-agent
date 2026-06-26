"""Tests for auxiliary vision client config resolution: api_key_env support.

Regression for the case where auxiliary.<task>.api_key_env is set in
config.yaml but api_key is not. Previously this left the resolved api_key
as None, which forced downstream callers into the custom-provider path
with an empty Bearer token → 401 from the upstream API.

Verified end-to-end against the MiniMax-M3 /api.minimax.io/v1 endpoint,
which is the exact failure mode observed on 2026-06-25 when 4 hermes
gateways (autobot, kai, next-step, ned) plus the orchestrator session
returned 401 from vision_analyze despite MINIMAX_API_KEY being set in
~/.hermes/.env.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from unittest.mock import patch

from agent.auxiliary_client import _resolve_task_provider_model


def _fake_task_config(api_key="", api_key_env=None, base_url="", provider=""):
    """Build a minimal task_config dict like _get_auxiliary_task_config returns."""
    cfg = {
        "provider": provider,
        "model": "MiniMax-M3",
        "base_url": base_url,
        "api_key": api_key,
        "api_mode": "",
    }
    if api_key_env is not None:
        cfg["api_key_env"] = api_key_env
    return cfg


def test_api_key_env_resolves_when_api_key_unset():
    """api_key_env with a populated env var must populate cfg_api_key."""
    fake_cfg = _fake_task_config(
        api_key="", api_key_env="MINIMAX_API_KEY",
        base_url="https://api.minimax.io/v1", provider="minimax",
    )
    with patch(
        "agent.auxiliary_client._get_auxiliary_task_config",
        return_value=fake_cfg,
    ):
        with patch.dict(os.environ, {"MINIMAX_API_KEY": "sk-tes...cdef"}):
            prov, model, base_url, api_key, api_mode = _resolve_task_provider_model("vision")
    assert prov == "custom", f"expected custom with explicit base_url, got {prov}"
    assert model == "MiniMax-M3"
    assert base_url == "https://api.minimax.io/v1"
    assert api_key == "sk-tes...cdef", (
        f"api_key_env should resolve to actual key, got {api_key!r}"
    )


def test_api_key_env_unset_leaves_api_key_none():
    """If neither api_key nor api_key_env resolves, api_key stays None (provider path)."""
    fake_cfg = _fake_task_config(
        api_key="", api_key_env="MINIMAX_API_KEY",
        base_url="https://api.minimax.io/v1", provider="minimax",
    )
    with patch(
        "agent.auxiliary_client._get_auxiliary_task_config",
        return_value=fake_cfg,
    ):
        # Make sure the env var really isn't set
        env_backup = os.environ.pop("MINIMAX_API_KEY", None)
        try:
            prov, model, base_url, api_key, api_mode = _resolve_task_provider_model("vision")
            # When api_key stays None and provider is set, falls through to provider path
            # (provider-registry will attempt its own env resolution).
            assert api_key is None
        finally:
            if env_backup is not None:
                os.environ["MINIMAX_API_KEY"] = env_backup


def test_explicit_api_key_wins_over_api_key_env():
    """Explicit api_key takes precedence over api_key_env."""
    fake_cfg = _fake_task_config(
        api_key="sk-cfg",
        api_key_env="MINIMAX_API_KEY",
        base_url="https://api.minimax.io/v1", provider="minimax",
    )
    with patch(
        "agent.auxiliary_client._get_auxiliary_task_config",
        return_value=fake_cfg,
    ):
        with patch.dict(os.environ, {"MINIMAX_API_KEY": "sk-env"}):
            prov, model, base_url, api_key, api_mode = _resolve_task_provider_model("vision")
    assert api_key == "sk-cfg"


def test_explicit_api_key_arg_wins_when_base_url_also_explicit():
    """Caller-supplied api_key wins when caller also supplied explicit base_url."""
    fake_cfg = _fake_task_config(
        api_key="sk-cfg",
        api_key_env="MINIMAX_API_KEY",
        base_url="https://api.minimax.io/v1", provider="minimax",
    )
    with patch(
        "agent.auxiliary_client._get_auxiliary_task_config",
        return_value=fake_cfg,
    ):
        with patch.dict(os.environ, {"MINIMAX_API_KEY": "sk-env"}):
            prov, model, base_url, api_key, api_mode = _resolve_task_provider_model(
                "vision", base_url="https://custom.example/v1", api_key="sk-explicit",
            )
    # Explicit base_url arg short-circuits to "custom" with explicit api_key arg
    assert prov == "custom"
    assert base_url == "https://custom.example/v1"
    assert api_key == "sk-explicit"


def test_legacy_key_env_alias_also_resolves():
    """Older 'key_env' field name (used in custom_providers) should also work."""
    fake_cfg = {
        "provider": "minimax",
        "model": "MiniMax-M3",
        "base_url": "https://api.minimax.io/v1",
        "api_key": "",
        "key_env": "MINIMAX_API_KEY",
    }
    with patch(
        "agent.auxiliary_client._get_auxiliary_task_config",
        return_value=fake_cfg,
    ):
        with patch.dict(os.environ, {"MINIMAX_API_KEY": "sk-leg...xyz"}):
            prov, model, base_url, api_key, api_mode = _resolve_task_provider_model("vision")
    assert api_key == "sk-leg...xyz"


def test_no_env_falls_back_to_provider_path():
    """No api_key, no api_key_env, no env var set → provider path, api_key=None."""
    fake_cfg = _fake_task_config(
        api_key="", api_key_env="MINIMAX_API_KEY",
        base_url="https://api.minimax.io/v1", provider="minimax",
    )
    with patch(
        "agent.auxiliary_client._get_auxiliary_task_config",
        return_value=fake_cfg,
    ):
        env_backup = os.environ.pop("MINIMAX_API_KEY", None)
        try:
            prov, model, base_url, api_key, api_mode = _resolve_task_provider_model("vision")
            # With provider=minimax and base_url set, returns provider=minimax
            # so resolve_provider_client can attempt its own env resolution.
            assert prov == "minimax"
            assert api_key is None
        finally:
            if env_backup is not None:
                os.environ["MINIMAX_API_KEY"] = env_backup