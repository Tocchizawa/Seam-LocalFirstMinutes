"""Collect legacy script checks without importing their top-level sys.exit().

All tests run with a disposable HOME. External-service/audio scripts remain
available with --run-manual; their prerequisites are reported as skips by default.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

import pytest

_TEST_HOME = tempfile.TemporaryDirectory(prefix="seam-pytest-home-")
os.environ["HOME"] = _TEST_HOME.name
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SCRIPT_TESTS = {
    "test_claude_api.py", "test_cli_providers.py", "test_openai_api.py",
    "test_project_models_security.py", "test_security_hardening.py",
    "test_summarize_api_preflight.py", "test_summarize_foundation.py",
}
MANUAL_TESTS = {
    "test_api.py": "isolated backend listening on 127.0.0.1:18900 with matching disposable HOME",
    "test_pipeline.py": "TTS meeting fixture and legacy faster-whisper model (see docs/live-notes-testing.md)",
    "test_pipeline_real.py": "10-minute meeting fixture and legacy faster-whisper model",
    "test_summarize.py": "Ollama qwen3:8b, faster-whisper and real meeting fixture",
    "test_summarize_integration.py": "populated disposable Seam DB and optional locally authenticated providers",
}
_CHECKS = {"passed": 0, "failed": 0}


def pytest_addoption(parser):
    parser.addoption("--run-manual", action="store_true", help="Run external service/audio scripts after preparing prerequisites")


def pytest_pycollect_makemodule(module_path, parent):
    if module_path.name in SCRIPT_TESTS or module_path.name in MANUAL_TESTS:
        return ScriptFile.from_parent(parent, path=module_path)
    return None


class ScriptFile(pytest.File):
    def collect(self):
        yield ScriptItem.from_parent(self, name="legacy_script")


class ScriptItem(pytest.Item):
    def runtest(self):
        name = self.path.name
        if name in MANUAL_TESTS and not self.config.getoption("--run-manual"):
            pytest.skip("manual prerequisite: " + MANUAL_TESTS[name])
        with tempfile.TemporaryDirectory(prefix="seam-script-home-") as home:
            tmpdir = Path(home) / "tmp"
            tmpdir.mkdir()
            env = dict(os.environ, HOME=home, TMPDIR=str(tmpdir), PYTHONPATH=str(ROOT))
            result = subprocess.run(
                [sys.executable, str(self.path)], cwd=ROOT, env=env,
                capture_output=True, text=True, timeout=300,
            )
        output = result.stdout + result.stderr
        passed = len(re.findall(r"(?m)^\s*PASS:", output))
        failed = len(re.findall(r"(?m)^\s*FAIL:", output))
        _CHECKS["passed"] += passed
        _CHECKS["failed"] += failed
        self.user_properties.extend([("legacy_checks_passed", passed), ("legacy_checks_failed", failed)])
        if result.returncode or failed:
            pytest.fail(f"{name}: exit={result.returncode}, checks={passed} passed/{failed} failed\n{output}", pytrace=False)

    def reportinfo(self):
        return self.path, 0, self.path.name


def pytest_terminal_summary(terminalreporter):
    terminalreporter.write_line(
        f"Legacy script assertions: {_CHECKS['passed']} passed, {_CHECKS['failed']} failed "
        "(in addition to individually collected pytest tests)"
    )
