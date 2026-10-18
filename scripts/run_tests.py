"""Run script-style regressions in isolated processes without touching user data."""
from __future__ import annotations

import argparse
import ast
import os
from pathlib import Path
import re
import subprocess
import socket
import sys
import tempfile
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
MANUAL = {
    "test_api.py": "backend on 127.0.0.1:18900 with matching disposable HOME",
    "test_pipeline.py": "/tmp/seam-audio-test/tts_meeting.wav and faster-whisper",
    "test_pipeline_real.py": "/tmp/seam-audio-test/meeting.wav and faster-whisper",
    "test_summarize.py": "audio fixtures, faster-whisper and Ollama qwen3:8b",
    "test_summarize_integration.py": "populated disposable DB; provider detection may probe installed CLIs/Ollama",
}


def is_script(path: Path) -> bool:
    tree = ast.parse(path.read_text())
    for node in tree.body:
        if isinstance(node, ast.If) and "__name__" in ast.unparse(node.test):
            return True
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) and ast.unparse(node.value.func) == "sys.exit":
            return True
    return False


def run_api() -> int:
    """Boot a real local API with disposable DB, then run both old API suites."""
    with tempfile.TemporaryDirectory(prefix="seam-api-home-") as home:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        app_dir = Path(home) / ".seam"
        app_dir.mkdir()
        (app_dir / "config.yaml").write_text(
            f"server:\n  port: {port}\nollama:\n  auto_start: false\n"
            "minutes_ai:\n  auto_generate: false\nwhisper:\n  preload_on_startup: false\n"
        )
        env = dict(os.environ, HOME=home, PYTHONPATH=str(ROOT), SEAM_TEST_PORT=str(port),
                   SEAM_TEST_BASE=f"http://127.0.0.1:{port}")
        with tempfile.TemporaryFile(mode="w+") as log:
            server = subprocess.Popen([sys.executable, "-m", "uvicorn", "src.main:app", "--host", "127.0.0.1", "--port", str(port)],
                                      cwd=ROOT, env=env, stdout=log, stderr=log)
            try:
                for _ in range(100):
                    try:
                        with urlopen(env["SEAM_TEST_BASE"] + "/health", timeout=0.2) as response:
                            if response.status == 200:
                                break
                    except OSError:
                        pass
                    if server.poll() is not None:
                        raise RuntimeError("test server exited during startup")
                    time.sleep(0.1)
                else:
                    raise RuntimeError("test server startup timed out")
                codes = []
                for argv in ([sys.executable, "tests/test_api.py"], ["bash", "tests/test_api.sh"]):
                    result = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
                    output = result.stdout + result.stderr
                    passed = len(re.findall(r"(?m)^\s*PASS:", output))
                    failed = len(re.findall(r"(?m)^\s*FAIL:", output))
                    print(f"{argv[-1]}: {passed} checks passed, {failed} failed, exit={result.returncode}")
                    if result.returncode or failed:
                        print(output)
                    codes.append(result.returncode or int(failed > 0))
                return int(any(codes))
            except Exception:
                log.seek(0)
                print(log.read())
                raise
            finally:
                server.terminate()
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scripts", action="store_true", help="Run all automatic legacy Python script entrypoints")
    parser.add_argument("--api", action="store_true", help="Boot disposable local backend and run Python/shell API scripts")
    parser.add_argument("--manual", choices=sorted(MANUAL), help="Run one prepared external-service script")
    parser.add_argument("--home", type=Path, help="Disposable fixture HOME for --manual (never use your normal HOME)")
    args = parser.parse_args()
    if args.api:
        return run_api()
    if args.manual and not args.home:
        parser.error("--manual requires --home pointing to a prepared disposable fixture directory")
    if args.home and not args.manual:
        parser.error("--home is only used with --manual")
    if args.manual:
        print("Required:", MANUAL[args.manual])
        files = [ROOT / "tests" / args.manual]
    else:
        files = [p for p in sorted((ROOT / "tests").glob("test_*.py"))
                 if p.name not in MANUAL and is_script(p)]
    scripts_passed = checks_passed = checks_failed = 0
    for path in files:
        with tempfile.TemporaryDirectory(prefix="seam-script-home-") as tmp:
            tmpdir = Path(tmp) / "tmp"
            tmpdir.mkdir()
            env = dict(os.environ, HOME=str(args.home.resolve()) if args.home else tmp,
                       TMPDIR=str(tmpdir), PYTHONPATH=str(ROOT))
            try:
                result = subprocess.run([sys.executable, str(path)], cwd=ROOT, env=env,
                                        text=True, capture_output=True, timeout=300)
                output = result.stdout + result.stderr
                passed = len(re.findall(r"(?m)^\s*PASS:", output))
                failed = len(re.findall(r"(?m)^\s*FAIL:", output))
                if not passed and not failed:
                    ran = re.search(r"Ran (\d+) tests?", output)
                    if ran and result.returncode == 0:
                        passed = int(ran.group(1))
                ok = result.returncode == 0 and failed == 0
            except subprocess.TimeoutExpired as error:
                output, passed, failed, ok = f"timeout: {error}", 0, 0, False
            scripts_passed += int(ok)
            checks_passed += passed
            checks_failed += failed
            print(f"{'PASS' if ok else 'FAIL'} {path.name}: {passed} checks passed, {failed} checks failed", flush=True)
            if not ok:
                print(output, flush=True)
    print(f"Scripts: {scripts_passed} passed, {len(files) - scripts_passed} failed")
    print(f"Reported checks: {checks_passed} passed, {checks_failed} failed")
    if not args.manual:
        for name, reason in MANUAL.items():
            print(f"NOT RUN {name}: {reason}")
    return int(scripts_passed != len(files))


if __name__ == "__main__":
    raise SystemExit(main())
