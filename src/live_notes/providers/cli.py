"""One invocation per batch, isolated workspace and explicit tool denial.

References: https://learn.chatgpt.com/docs/config-file/config-reference
https://code.claude.com/docs/en/cli-reference
These limits constrain model tools, not a malicious user-supplied CLI binary or
launcher. Enterprise policy may reject them; no permissive fallback is used.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from src.summarize.cli_launcher import build_command_argv

from .base import LiveProviderError, StructuredInferenceProvider
from .process import cli_failure, reject_tool_event, run_cli


def _json_object(raw: str | bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise LiveProviderError(
            "CLI did not return valid completed JSON", structural=True
        ) from exc
    if not isinstance(value, dict):
        raise LiveProviderError("CLI proposal must be a JSON object", structural=True)
    return value


class _CLIProvider(StructuredInferenceProvider):
    binary: str

    def __init__(self, config: dict[str, Any]) -> None:
        # Never inherit extra_args, repository paths, add-dir, session IDs or
        # post-recording summary prompts. Launcher fields are trusted user setup.
        self.config = {
            key: value
            for key, value in (config or {}).items()
            if key
            in {
                "binary_path",
                "launcher_command",
                "launcher_shell",
                "launcher_interactive",
            }
        }
        self.model = str((config or {}).get("model", "") or "").strip()
        effort = str((config or {}).get("reasoning_effort", "low") or "low")
        self.effort = (
            effort if effort in {"minimal", "low", "medium", "high", "xhigh"} else "low"
        )
        self._ready: dict[str, Any] | None = None
        self._features: list[str] = []
        self._preflight_lock = asyncio.Lock()

    async def _run(self, args: list[str], cwd: str, **kwargs: Any) -> bytes:
        argv, _ = await asyncio.to_thread(
            build_command_argv,
            self.config,
            default_binary=self.binary,
            command_args=args,
        )
        if not argv:
            raise LiveProviderError(f"{self.binary} CLI was not found", retryable=False)
        return await run_cli(argv, cwd=cwd, **kwargs)

    async def preflight(self) -> dict[str, Any]:
        async with self._preflight_lock:
            try:
                with tempfile.TemporaryDirectory(prefix="seam-live-probe-") as cwd:
                    version = (
                        (await self._run(["--version"], cwd))
                        .decode(errors="replace")
                        .strip()[:120]
                    )
                    await self._check_capabilities(cwd)
                self._ready = {
                    "ok": True,
                    "version": version,
                    "provider": self.name,
                    "authentication_checked": False,
                }
            except LiveProviderError as exc:
                self._ready = {
                    "ok": False,
                    "error": str(exc),
                    "provider": self.name,
                    "retryable": exc.retryable,
                    "code": exc.code,
                }
            return dict(self._ready)

    async def infer_operations(
        self,
        prompt: str,
        schema: dict[str, Any],
        timeout: float = 120,
        cancellation: asyncio.Event | None = None,
    ) -> dict[str, Any]:
        if cancellation is not None and cancellation.is_set():
            raise LiveProviderError("Live inference was cancelled")
        if self._ready is None or not self._ready.get("ok"):
            result = await self.preflight()
            if not result["ok"]:
                raise LiveProviderError(
                    result["error"],
                    retryable=result.get("retryable", False),
                    code=result.get("code", "CLI_ERROR"),
                )
        with tempfile.TemporaryDirectory(prefix="seam-live-infer-") as cwd:
            args = self._arguments(schema, cwd)
            raw = await self._run(
                args,
                cwd,
                stdin=prompt,
                timeout=timeout,
                cancellation=cancellation,
                monitor_events=True,
            )
            return self._parse(raw)

    async def _check_capabilities(self, cwd: str) -> None:
        raise NotImplementedError

    def _arguments(self, schema: dict[str, Any], cwd: str) -> list[str]:
        raise NotImplementedError

    def _parse(self, raw: bytes) -> dict[str, Any]:
        raise NotImplementedError


class CodexLiveProvider(_CLIProvider):
    name = "codex"
    binary = "codex"

    async def _check_capabilities(self, cwd: str) -> None:
        help_text = (await self._run(["exec", "--help"], cwd)).decode(errors="replace")
        required = {
            "--ignore-user-config",
            "--ignore-rules",
            "--ephemeral",
            "--output-schema",
            "--json",
            "--sandbox",
            "--skip-git-repo-check",
            "--strict-config",
        }
        missing = sorted(flag for flag in required if flag not in help_text)
        if missing:
            raise LiveProviderError(
                "Codex lacks restricted live options: " + ", ".join(missing), False
            )
        # This local query does not create a session or invoke the model.
        feature_text = (await self._run(["features", "list"], cwd)).decode(
            errors="replace"
        )
        features = []
        for line in feature_text.splitlines():
            match = re.match(r"^([a-z][a-z0-9_.]*)\s+.+\s+(?:true|false)\s*$", line)
            if match:
                features.append(match.group(1))
        if not {"shell_tool", "apps", "plugins", "hooks", "multi_agent"}.issubset(
            features
        ):
            raise LiveProviderError(
                "Codex tool capability discovery is unsupported", False
            )
        image_features = set(features) & {"view_image", "view_image_tool"}
        if not image_features:
            raise LiveProviderError(
                "Codex image-tool restriction is unsupported", False
            )
        # Disable every discovered feature rather than assuming a future tool's
        # name. Keep only auth storage and wire-protocol infrastructure. Removed
        # features are accepted by Codex for compatibility, but are harmless.
        safe_features = {
            "secret_auth_storage",
            "enable_request_compression",
            "api_key_model_discovery",
            "content_item_kinds",
        }
        self._features = [name for name in features if name not in safe_features]
        # Feature discovery alone does not establish that managed policy permits
        # disabling these capabilities. Check the effective local configuration,
        # without starting exec or sending any prompt to the model. features does
        # not support --strict-config; exec below deliberately does.
        effective = (
            await self._run([*self._restriction_args(), "features", "list"], cwd)
        ).decode(errors="replace")
        states = {}
        for line in effective.splitlines():
            match = re.match(r"^([a-z][a-z0-9_.]*)\s+(.+)\s+(true|false)\s*$", line)
            if match:
                states[match.group(1)] = (
                    match.group(2).strip(),
                    match.group(3) == "true",
                )
        if not {"shell_tool", "apps", "plugins", "hooks", "multi_agent"}.issubset(
            states
        ):
            raise LiveProviderError(
                "Codex effective tool configuration is unavailable", False
            )
        if not image_features.issubset(states):
            raise LiveProviderError(
                "Codex effective image-tool configuration is unavailable", False
            )
        for name, (stage, enabled) in states.items():
            # unified_exec is mandatory in newer Codex, but shell_tool=false
            # removes its command tools. Removed compatibility flags are inert.
            if (
                enabled
                and stage != "removed"
                and name not in safe_features | {"unified_exec"}
            ):
                raise LiveProviderError(
                    "Codex policy prevents disabling live tool capabilities", False
                )
        # Exercise the exact exec/strict-config flags without reaching a model.
        # A missing schema must stop startup locally. A separate empty auth home
        # and a loopback-only provider also prevent cloud access if CLI ordering changes.
        probe_args = self._arguments({}, cwd)
        schema_path = Path(cwd) / "operations.schema.json"
        schema_path.unlink()
        with tempfile.TemporaryDirectory(prefix="seam-live-probe-home-") as auth_home:
            environment = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith(("CODEX_", "OPENAI_", "ANTHROPIC_", "CLAUDE_"))
            }
            environment["CODEX_HOME"] = auth_home
            await self._run(
                [
                    *probe_args[:-1],
                    "-c",
                    'model_provider="seam_local_probe"',
                    "-c",
                    'model_providers.seam_local_probe={name="Seam offline configuration probe",base_url="http://127.0.0.1:9/v1",wire_api="responses",requires_openai_auth=false}',
                    "-",
                ],
                cwd,
                stdin="Seam offline configuration validation.",
                environment=environment,
                expected_error=f"Failed to read output schema file {schema_path}",
            )

    def _restriction_args(self) -> list[str]:
        args = []
        for feature in self._features:
            args += ["--disable", feature]
        for setting in (
            'web_search="disabled"',
            "mcp_servers={}",
            "plugins={}",
            "hooks={}",
            "apps._default.enabled=false",
            "project_doc_max_bytes=0",
            'approval_policy="never"',
            f'model_reasoning_effort="{self.effort}"',
        ):
            args += ["-c", setting]
        return args

    def _arguments(self, schema: dict[str, Any], cwd: str) -> list[str]:
        schema_path = Path(cwd) / "operations.schema.json"
        schema_path.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
        args = [
            "exec",
            "--ignore-user-config",
            "--ignore-rules",
            "--ephemeral",
            "--strict-config",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--json",
            "--output-schema",
            str(schema_path),
            *self._restriction_args(),
        ]
        if self.model:
            args += ["--model", self.model]
        return [*args, "-"]

    def _parse(self, raw: bytes) -> dict[str, Any]:
        final: str | None = None
        complete = False
        for line in raw.splitlines():
            if not line.strip():
                continue
            event = _json_object(line)
            reject_tool_event(event)
            kind = event.get("type")
            if kind in {"error", "turn.failed"}:
                raise cli_failure(json.dumps(event, ensure_ascii=False))
            if kind == "turn.started":
                complete = False
                final = None
            if kind == "item.completed":
                if complete:
                    raise LiveProviderError(
                        "Codex emitted output after turn completion"
                    )
                item = event.get("item", {})
                if not isinstance(item, dict):
                    raise LiveProviderError("Codex returned a malformed item")
                if item.get("type") == "agent_message":
                    final = item.get("text")
            if kind == "turn.completed":
                complete = True
        if not complete or not isinstance(final, str):
            raise LiveProviderError(
                "Codex exited without a completed structured response"
            )
        return _json_object(final)


class ClaudeLiveProvider(_CLIProvider):
    name = "claude_code"
    binary = "claude"

    async def _check_capabilities(self, cwd: str) -> None:
        help_text = (await self._run(["--help"], cwd)).decode(errors="replace")
        required = {
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
        missing = sorted(flag for flag in required if flag not in help_text)
        if missing:
            raise LiveProviderError(
                "Claude lacks restricted live options: " + ", ".join(missing), False
            )

    def _arguments(self, schema: dict[str, Any], cwd: str) -> list[str]:
        args = [
            "--bare",
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema),
            "--tools",
            "",
            "--disallowedTools",
            "mcp__*",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--setting-sources",
            "",
            "--no-session-persistence",
            "--max-turns",
            "1",
        ]
        if self.model:
            args += ["--model", self.model]
        return args

    def _parse(self, raw: bytes) -> dict[str, Any]:
        try:
            result = _json_object(raw)
        except LiveProviderError:
            result = {}
            for line in raw.splitlines():
                event = _json_object(line)
                reject_tool_event(event)
                if event.get("type") == "result":
                    result = event
        reject_tool_event(result)
        if (
            result.get("type") != "result"
            or result.get("is_error")
            or result.get("subtype") != "success"
        ):
            raise LiveProviderError(
                "Claude exited without a successful structured response"
            )
        proposal = result.get("structured_output")
        if isinstance(proposal, dict):
            return proposal
        raise LiveProviderError("Claude result did not include structured_output")
