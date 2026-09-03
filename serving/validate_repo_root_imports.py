#!/usr/bin/env python3
"""Required gate: prove production-shaped repo-root import of serving/dashboard modules.

This gate proves that modules under serving/ and dashboard/ can be cleanly imported
when only the repository root is on PYTHONPATH (matching production systemd service execution
such as taey-dashboard.service), as well as when executed directly from the serving/ directory.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVING = ROOT / "serving"

STUB_HEADER = (
    "import importlib.util, types, sys\n"
    "if importlib.util.find_spec('redis') is None:\n"
    "    _r = types.ModuleType('redis')\n"
    "    _r.Redis = object\n"
    "    sys.modules['redis'] = _r\n"
)


def require(condition: bool, detail: str) -> None:
    if not condition:
        raise AssertionError(detail)


def prove_repo_root_imports() -> None:
    """Verify clean import with strictly repository root on sys.path."""
    code = (
        STUB_HEADER +
        "import sys\n"
        "import serving.council_prompt_receipt as r\n"
        "import serving.taey_seat as s\n"
        "from dashboard import native_council\n"
        "assert r.bind_outbound_request_bytes is not None\n"
        "print('PASS: repo-root imports successful')\n"
    )
    res = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(ROOT),
        env={"PYTHONPATH": str(ROOT), "PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
    )
    require(
        res.returncode == 0,
        f"Production-shaped repo-root import failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}",
    )


def prove_serving_direct_imports() -> None:
    """Verify clean import when executed directly within serving directory."""
    code = (
        STUB_HEADER +
        "import sys\n"
        "import council_prompt_receipt as r\n"
        "import taey_seat as s\n"
        "assert r.bind_outbound_request_bytes is not None\n"
        "print('PASS: serving-direct imports successful')\n"
    )
    res = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(SERVING),
        env={"PYTHONPATH": str(SERVING), "PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
    )
    require(
        res.returncode == 0,
        f"Serving-direct import failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}",
    )


def prove_unnamespaced_import_fails_red_without_serving_path() -> None:
    """Prove gate turns RED when an unnamespaced import is attempted without serving on sys.path."""
    code = "import outbound_request_codec"
    res = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(ROOT),
        env={"PYTHONPATH": str(ROOT), "PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
    )
    require(
        res.returncode != 0 and "ModuleNotFoundError" in res.stderr,
        "Expected unnamespaced import to fail RED with ModuleNotFoundError",
    )


def main() -> int:
    prove_repo_root_imports()
    prove_serving_direct_imports()
    prove_unnamespaced_import_fails_red_without_serving_path()
    print("PASS: production-shaped repo-root and direct imports verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
