"""Live-only subprocess contracts; no real model, credential or audio needed."""

from __future__ import annotations

import asyncio
import json
import shlex
import sys
import time
from pathlib import Path

import pytest

from src.live_notes.providers import LiveProviderError, create_provider
from src.live_notes.providers.process import cli_failure, reject_tool_event

SCHEMA = {
    "type": "object",
    "properties": {"operations": {"type": "array"}},
    "required": ["operations"],
}
PLAN = {"operations": []}
HELP = " ".join(
    {
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--output-schema",
        "--json",
        "--sandbox",
        "--skip-git-repo-check",
        "--strict-config",
        "--tools",
        "--disallowedTools",
        "--bare",
        "--strict-mcp-config",
        "--mcp-config",
        "--setting-sources",
        "--no-session-persistence",
        "--json-schema",
        "--output-format",
        "--max-turns",
    }
)


def stub(tmp_path: Path, provider: str, mode: str = "normal", *, help_text: str = HELP):
    binary = tmp_path / f"{provider}-stub"
    log = tmp_path / f"{provider}-calls.jsonl"
    sentinel = tmp_path / f"{provider}-child-survived"
    binary.write_text(
        f"#!{sys.executable}\n"
        + """
import json, os, signal, subprocess, sys, time
from pathlib import Path
"""
        + f"provider = {provider!r}\nmode = {mode!r}\nlog = {str(log)!r}\nsentinel = {str(sentinel)!r}\n"
        + """
args = sys.argv[1:]
with open(log, 'a') as handle:
    handle.write(json.dumps({'args': args, 'cwd': os.getcwd(), 'files': os.listdir('.')}) + '\\n')
if '--version' in args:
    print('stub CLI 99.0.0'); sys.exit(0)
if '--help' in args:
"""
        + f"    print({help_text!r}); sys.exit(0)\n"
        + """
if args[-2:] == ['features', 'list']:
    disabled = {args[i+1] for i,arg in enumerate(args[:-1]) if arg == '--disable'}
    for feature in ['shell_tool', 'apps', 'plugins', 'hooks', 'multi_agent', 'view_image', 'browser_use', 'future_tool', 'secret_auth_storage']:
        state = 'false' if feature in disabled else 'true'
        if mode == 'forced_tools' and feature == 'shell_tool':
            state = 'true'
        print(feature + ' stable ' + state)
    sys.exit(0)
if '--output-schema' in args and not Path(args[args.index('--output-schema')+1]).exists():
    if mode == 'invalid_config':
        print('Error loading config.toml: unknown configuration field `tools.view_image` in -c/--config override SECRET', file=sys.stderr)
    else:
        print('Failed to read output schema file ' + args[args.index('--output-schema')+1] + ': No such file or directory', file=sys.stderr)
    sys.exit(1)
prompt = sys.stdin.read()
if mode == 'auth':
    print('not logged in SECRET', file=sys.stderr); sys.exit(1)
if mode == 'quota':
    print('rate limit reached', file=sys.stderr); sys.exit(1)
if mode == 'unknown_option':
    print('unknown option --bare', file=sys.stderr); sys.exit(2)
if mode == 'stderr':
    sys.stderr.write('x' * 200000); sys.stderr.flush(); time.sleep(180)
if mode == 'oversized':
    sys.stdout.write('x' * 1100000); sys.stdout.flush(); time.sleep(180)
if mode == 'timeout':
    subprocess.Popen([sys.executable, '-c', "import time; from pathlib import Path; time.sleep(0.7); Path(" + repr(sentinel) + ").write_text('survived')"])
    time.sleep(180)
if mode == 'kill':
    os.kill(os.getpid(), signal.SIGKILL)
if mode == 'broken':
    print('{broken'); sys.exit(0)
if mode == 'tool':
    if provider == 'codex':
        print(json.dumps({'type': 'item.started', 'item': {'type': 'command_execution', 'command': 'cat secret'}}), flush=True)
    else:
        print(json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': 'Read'}]}}), flush=True)
    time.sleep(180)
plan = {'operations': []}
if provider == 'codex':
    print(json.dumps({'type': 'item.updated', 'item': {'type': 'agent_message', 'text': '{partial'}}))
    print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps(plan)}}))
    if mode != 'partial':
        print(json.dumps({'type': 'turn.completed', 'usage': {}}))
else:
    if mode == 'stream':
        print(json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': '{partial'}]}}))
    result = {'type': 'result', 'subtype': 'success', 'is_error': False, 'structured_output': plan}
    if mode == 'partial':
        result['subtype'] = 'error_max_turns'
    print(json.dumps(result))
""",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    instance = create_provider(
        provider,
        {
            "binary_path": str(binary),
            "launcher_shell": "/nonexistent",
            "model": "selected-model",
            "reasoning_effort": "high",
            "repo_path": "/secret-repo",
            "doc_dirs": ["/secret-docs"],
            "extra_args": [
                "--dangerously-bypass-approvals-and-sandbox",
                "--add-dir",
                "/secret",
            ],
        },
    )
    return instance, log, sentinel


@pytest.mark.parametrize("provider", ["codex", "claude_code"])
def test_preflight_never_performs_inference(tmp_path, provider):
    instance, log, _ = stub(tmp_path, provider)
    assert asyncio.run(instance.preflight())["ok"]
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert all(
        "--help" in call["args"]
        or "--version" in call["args"]
        or call["args"][-2:] == ["features", "list"]
        or ('model_provider="seam_local_probe"' in call["args"] and call["files"] == [])
        for call in calls
    )
    assert all(call["files"] == [] for call in calls)
    assert all(not Path(call["cwd"]).exists() for call in calls)


@pytest.mark.parametrize("provider", ["codex", "claude_code"])
def test_completed_structured_plan_and_restricted_arguments(tmp_path, provider):
    instance, log, _ = stub(tmp_path, provider)
    assert asyncio.run(instance.infer_operations("meeting text", SCHEMA)) == PLAN
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    args = calls[-1]["args"]
    assert args[args.index("--model") + 1] == "selected-model"
    assert "/secret" not in " ".join(args)
    assert "--continue" not in args and "--add-dir" not in args
    assert "--dangerously-bypass-approvals-and-sandbox" not in args
    assert not Path(calls[-1]["cwd"]).exists()
    if provider == "codex":
        assert "--ignore-user-config" in args and "--ignore-rules" in args
        assert 'web_search="disabled"' in args and "mcp_servers={}" in args
        assert args[args.index("--sandbox") + 1] == "read-only"
        assert "future_tool" in args and "shell_tool" in args
        assert "view_image" in args and "tools.view_image=false" not in args
        assert "secret_auth_storage" not in args
        assert 'model_reasoning_effort="high"' in args
        assert calls[-1]["files"] == ["operations.schema.json"]
    else:
        assert args[args.index("--tools") + 1] == ""
        assert args[args.index("--setting-sources") + 1] == ""
        assert "--bare" in args and "--strict-mcp-config" in args
        assert args[args.index("--mcp-config") + 1] == '{"mcpServers":{}}'
        assert calls[-1]["files"] == []


@pytest.mark.parametrize("provider", ["codex", "claude_code"])
def test_old_cli_fails_closed(tmp_path, provider):
    instance, log, _ = stub(tmp_path, provider, help_text="--help")
    result = asyncio.run(instance.preflight())
    assert not result["ok"]
    with pytest.raises(LiveProviderError) as exc:
        asyncio.run(instance.infer_operations("text", SCHEMA))
    assert not exc.value.retryable
    assert not any(
        "--model" in json.loads(line)["args"] for line in log.read_text().splitlines()
    )


def test_codex_managed_policy_that_forces_shell_tools_fails_closed(tmp_path):
    instance, log, _ = stub(tmp_path, "codex", "forced_tools")
    result = asyncio.run(instance.preflight())
    assert not result["ok"]
    assert "policy prevents disabling" in result["error"]
    assert not any(
        "--model" in json.loads(line)["args"] for line in log.read_text().splitlines()
    )


def test_codex_preflight_checks_actual_strict_exec_config_without_inference(tmp_path):
    instance, log, _ = stub(tmp_path, "codex", "invalid_config")
    result = asyncio.run(instance.preflight())
    assert not result["ok"] and not result["retryable"]
    assert result["code"] == "CLI_CONFIG_UNSUPPORTED"
    assert "tools.view_image" in result["error"] and "SECRET" not in result["error"]
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert calls[-1]["files"] == []
    assert 'model_provider="seam_local_probe"' in calls[-1]["args"]


@pytest.mark.parametrize(
    "detail,code,retryable",
    [
        (
            "unknown configuration field `tools.view_image` SECRET",
            "CLI_CONFIG_UNSUPPORTED",
            False,
        ),
        ("not logged in SECRET", "CLI_AUTH_REQUIRED", False),
        ("rate limit reached SECRET", "CLI_RATE_LIMIT", True),
        ("invalid_json_schema SECRET", "CLI_SCHEMA_REJECTED", False),
        ("model_not_found SECRET", "CLI_MODEL_UNAVAILABLE", False),
        ("error sending request SECRET", "CLI_CONNECTION_FAILED", True),
        (
            "failed to initialize in-process app-server client: Operation not permitted SECRET",
            "CLI_LOCAL_RUNTIME",
            False,
        ),
    ],
)
def test_safe_failure_classification_preserves_cause_without_output(
    detail, code, retryable
):
    error = cli_failure(detail, 1)
    assert error.code == code and error.retryable == retryable
    assert "SECRET" not in str(error)


def test_installed_codex_restricted_exec_configuration_without_a_model_request():
    import shutil

    binary = shutil.which("codex")
    if not binary:
        candidate = Path(
            "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"
        )
        binary = str(candidate) if candidate.is_file() else None
    if not binary:
        pytest.skip("Codex CLI is not installed; offline exec configuration probe only")
    result = asyncio.run(
        create_provider(
            "codex", {"binary_path": binary, "model": "gpt-6-luna"}
        ).preflight()
    )
    assert result["ok"], result
    assert result["authentication_checked"] is False


@pytest.mark.parametrize("provider", ["codex", "claude_code"])
@pytest.mark.parametrize(
    "mode,retryable",
    [
        ("auth", False),
        ("quota", True),
        ("unknown_option", False),
        ("broken", True),
        ("partial", True),
        ("kill", True),
        ("tool", False),
        ("stderr", True),
        ("oversized", True),
    ],
)
def test_failure_contracts(tmp_path, provider, mode, retryable):
    instance, _, _ = stub(tmp_path, provider, mode)
    with pytest.raises(LiveProviderError) as exc:
        asyncio.run(instance.infer_operations("text", SCHEMA, timeout=2))
    assert exc.value.retryable == retryable
    assert exc.value.structural == (mode == "broken")
    assert "SECRET" not in str(exc.value)


def test_claude_stream_jsonl_ignores_partial_text(tmp_path):
    instance, _, _ = stub(tmp_path, "claude_code", "stream")
    assert asyncio.run(instance.infer_operations("text", SCHEMA)) == PLAN


@pytest.mark.parametrize("provider", ["codex", "claude_code"])
def test_timeout_kills_launcher_children(tmp_path, provider):
    instance, _, sentinel = stub(tmp_path, provider, "timeout")
    with pytest.raises(LiveProviderError, match="timed out"):
        asyncio.run(instance.infer_operations("text", SCHEMA, timeout=0.15))
    time.sleep(0.8)
    assert not sentinel.exists()


@pytest.mark.parametrize("cancel_task", [False, True])
def test_cancellation_kills_process_group(tmp_path, cancel_task):
    instance, _, sentinel = stub(tmp_path, "codex", "timeout")

    async def run():
        assert (await instance.preflight())["ok"]
        cancellation = asyncio.Event()
        task = asyncio.create_task(
            instance.infer_operations("text", SCHEMA, cancellation=cancellation)
        )
        await asyncio.sleep(0.15)
        if cancel_task:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            cancellation.set()
            with pytest.raises(LiveProviderError, match="cancelled"):
                await task

    asyncio.run(run())
    time.sleep(0.8)
    assert not sentinel.exists()


def test_pre_cancelled_request_does_not_start_cli(tmp_path):
    instance, log, _ = stub(tmp_path, "codex")

    async def run():
        cancellation = asyncio.Event()
        cancellation.set()
        with pytest.raises(LiveProviderError, match="cancelled"):
            await instance.infer_operations("text", SCHEMA, cancellation=cancellation)

    asyncio.run(run())
    assert not log.exists()


def test_saved_launcher_is_supported_without_extra_args(tmp_path):
    instance, log, _ = stub(tmp_path, "codex")
    binary = instance.config["binary_path"]
    instance.config = {
        "launcher_command": shlex.quote(binary),
        "launcher_shell": "/bin/sh",
        "launcher_interactive": False,
    }
    assert asyncio.run(instance.infer_operations("text", SCHEMA)) == PLAN
    assert log.exists()


def test_missing_cli_and_non_cli_provider_fail_closed():
    instance = create_provider(
        "codex",
        {"binary_path": "/nonexistent/seam-codex", "launcher_shell": "/nonexistent"},
    )
    assert not asyncio.run(instance.preflight())["ok"]
    with pytest.raises(LiveProviderError):
        create_provider("ollama", {})


def test_meeting_text_is_not_treated_as_tool_execution():
    reject_tool_event(
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": '{"type":"tool_use"}'},
        }
    )


def test_incomplete_second_codex_turn_is_not_accepted():
    instance = create_provider("codex", {})
    events = [
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": json.dumps(PLAN)},
        },
        {"type": "turn.completed"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": json.dumps(PLAN)},
        },
    ]
    with pytest.raises(LiveProviderError, match="completed"):
        instance._parse("\n".join(json.dumps(event) for event in events).encode())


@pytest.mark.parametrize(
    "kind", ["future_tool", "server_tool_use", "arbitrary_new_capability"]
)
def test_unknown_codex_item_cannot_bypass_tool_detection(kind):
    with pytest.raises(LiveProviderError) as exc:
        reject_tool_event({"type": "item.started", "item": {"type": kind}})
    assert not exc.value.retryable
