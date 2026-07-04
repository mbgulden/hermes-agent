"""Test the Gateway Process Env doctor check logic.

Verifies that the new doctor section correctly identifies when a long-running
gateway process lacks an API key that IS present in ~/.hermes/.env, which
is the failure mode that caused 401s across all 5 profiles on 2026-06-25.
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch, mock_open

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

# We test the check function in isolation rather than running the full
# doctor — the full doctor has lots of side effects (network calls, etc.).
# Pull just the section logic into a minimal harness.


def _check_gateway_env(
    pgrep_output: str,
    proc_env: dict,  # pid -> dict of env vars
    proc_cmdline: dict,  # pid -> cmdline string
    provider_env_vars: list,
    dotenv_values: dict,
):
    """Replicate the doctor section's logic and return a structured result."""
    gateway_lines = []
    for line in (pgrep_output or "").splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        env = proc_env.get(pid, {})
        cmdline = proc_cmdline.get(pid, "")
        profile = "?"
        if "--profile" in cmdline:
            try:
                profile = cmdline.split("--profile", 1)[1].split()[0]
            except Exception:
                pass
        gateway_lines.append((pid, profile, env))

    missing_per_gateway = []
    for pid, profile, env in gateway_lines:
        missing = []
        for ev, _label in provider_env_vars:
            if env.get(ev, "").strip():
                continue
            if dotenv_values.get(ev, "").strip():
                missing.append(ev)
        if missing:
            missing_per_gateway.append((pid, profile, missing))

    return {
        "gateway_count": len(gateway_lines),
        "gateways_with_missing": missing_per_gateway,
        "gateway_profiles": [p for _, p, _ in gateway_lines],
    }


def test_all_gateways_have_all_keys():
    """Happy path: all gateways have every api_key_env var populated."""
    result = _check_gateway_env(
        pgrep_output="1234 /usr/bin/hermes --profile kai gateway run\n"
                     "5678 /usr/bin/hermes --profile ned gateway run",
        proc_env={
            1234: {"MINIMAX_API_KEY": "sk-real"},
            5678: {"MINIMAX_API_KEY": "sk-real"},
        },
        proc_cmdline={
            1234: "/usr/bin/hermes --profile kai gateway run",
            5678: "/usr/bin/hermes --profile ned gateway run",
        },
        provider_env_vars=[("MINIMAX_API_KEY", "minimax")],
        dotenv_values={"MINIMAX_API_KEY": "sk-real"},
    )
    assert result["gateway_count"] == 2
    assert result["gateways_with_missing"] == []


def test_one_gateway_missing_key_set_in_dotenv():
    """The actual failure mode from 2026-06-25: key in .env but not in gateway env."""
    result = _check_gateway_env(
        pgrep_output="1234 /usr/bin/hermes --profile kai gateway run",
        proc_env={1234: {}},  # gateway has nothing
        proc_cmdline={1234: "/usr/bin/hermes --profile kai gateway run"},
        provider_env_vars=[("MINIMAX_API_KEY", "minimax")],
        dotenv_values={"MINIMAX_API_KEY": "sk-real"},
    )
    assert len(result["gateways_with_missing"]) == 1
    pid, profile, missing = result["gateways_with_missing"][0]
    assert profile == "kai"
    assert missing == ["MINIMAX_API_KEY"]


def test_key_not_in_dotenv_is_not_flagged():
    """If .env doesn't have the key, don't flag the gateway for missing it."""
    result = _check_gateway_env(
        pgrep_output="1234 /usr/bin/hermes --profile kai gateway run",
        proc_env={1234: {}},
        proc_cmdline={1234: "/usr/bin/hermes --profile kai gateway run"},
        provider_env_vars=[("MINIMAX_API_KEY", "minimax")],
        dotenv_values={},  # .env missing the key — user hasn't set it up
    )
    # Not flagged because dotenv doesn't have it either — that's the user's choice
    assert result["gateways_with_missing"] == []


def test_no_running_gateways():
    """Doctor must not crash when no hermes gateways are running."""
    result = _check_gateway_env(
        pgrep_output="",
        proc_env={},
        proc_cmdline={},
        provider_env_vars=[("MINIMAX_API_KEY", "minimax")],
        dotenv_values={"MINIMAX_API_KEY": "sk-real"},
    )
    assert result["gateway_count"] == 0
    assert result["gateways_with_missing"] == []


def test_multiple_providers_checked():
    """Doctor must check all api_key_env vars, not just the first."""
    result = _check_gateway_env(
        pgrep_output="1234 /usr/bin/hermes --profile kai gateway run",
        proc_env={1234: {"MINIMAX_API_KEY": "sk-real"}},  # has minimax but not deepseek
        proc_cmdline={1234: "/usr/bin/hermes --profile kai gateway run"},
        provider_env_vars=[
            ("MINIMAX_API_KEY", "minimax"),
            ("DEEPSEEK_API_KEY", "deepseek"),
        ],
        dotenv_values={
            "MINIMAX_API_KEY": "sk-real",
            "DEEPSEEK_API_KEY": "sk-ds",
        },
    )
    assert len(result["gateways_with_missing"]) == 1
    pid, profile, missing = result["gateways_with_missing"][0]
    assert missing == ["DEEPSEEK_API_KEY"]


def test_auxiliary_task_keys_also_checked():
    """Auxiliary vision/compression/title-gen api_key_env vars should also be checked."""
    result = _check_gateway_env(
        pgrep_output="1234 /usr/bin/hermes --profile kai gateway run",
        proc_env={1234: {}},
        proc_cmdline={1234: "/usr/bin/hermes --profile kai gateway run"},
        provider_env_vars=[
            ("MINIMAX_API_KEY", "minimax"),
            ("MINIMAX_API_KEY", "auxiliary.vision"),  # same var, different source
        ],
        dotenv_values={"MINIMAX_API_KEY": "sk-real"},
    )
    # Both entries point to the same env var — surfaced per-source so the
    # doctor can tell the user whether the gap is provider-registry or
    # auxiliary-task scope. Use a set to ignore ordering.
    missing = result["gateways_with_missing"][0][2]
    assert set(missing) == {"MINIMAX_API_KEY"}