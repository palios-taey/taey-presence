#!/usr/bin/env python3
"""Required gate: prove production-shaped repo-root import of serving/dashboard modules.

This gate proves that modules under serving/ and dashboard/ can be cleanly imported
when only the repository root is on PYTHONPATH (matching production systemd service execution
such as taey-dashboard.service), as well as when executed directly from the serving/ directory.
It also proves that package-context selection is deterministic (no broad try/except swallowing)
and isolated from shadow modules on sys.path.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
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
        "assert s.bind_outbound_request_bytes is not None\n"
        "print('PASS: repo-root package imports successful')\n"
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
        "assert s.bind_outbound_request_bytes is not None\n"
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


def prove_shadow_module_isolation_in_package_context() -> None:
    """Prove package-context relative imports do not bind shadow modules on sys.path."""
    with tempfile.TemporaryDirectory() as td:
        shadow_codec = Path(td) / "outbound_request_codec.py"
        shadow_codec.write_text("IS_SHADOW = True\nraise RuntimeError('SHADOW_CODEC_EXECUTED')\n")

        code = (
            STUB_HEADER +
            "import serving.council_prompt_receipt as r\n"
            "assert not hasattr(r, 'IS_SHADOW')\n"
            "print('PASS: shadow module isolated')\n"
        )
        res = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(ROOT),
            env={"PYTHONPATH": f"{td}:{str(ROOT)}", "PATH": os.environ.get("PATH", "")},
            capture_output=True,
            text=True,
        )
        require(
            res.returncode == 0 and "SHADOW_CODEC_EXECUTED" not in res.stderr,
            f"Package-context import bound shadow module on sys.path:\n{res.stderr}",
        )


def prove_transitive_import_error_not_masked() -> None:
    """Prove transitive import errors in submodules propagate faithfully without broad try/except masking."""
    with tempfile.TemporaryDirectory() as td:
        pkg = Path(td) / "testpkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "submod.py").write_text("raise ImportError('CANNOT_IMPORT_INNER_CANARY')\n")
        (pkg / "consumer.py").write_text(
            "if __package__:\n"
            "    from .submod import *\n"
            "else:\n"
            "    from submod import *\n"
        )

        code = "import testpkg.consumer"
        res = subprocess.run(
            [sys.executable, "-c", code],
            cwd=td,
            env={"PYTHONPATH": td, "PATH": os.environ.get("PATH", "")},
            capture_output=True,
            text=True,
        )
        require(
            res.returncode != 0 and "CANNOT_IMPORT_INNER_CANARY" in res.stderr,
            f"Transitive ImportError was masked:\n{res.stderr}",
        )


def prove_no_broad_try_except_import_error_in_serving_consumers() -> None:
    """Prove all touched serving consumers use deterministic __package__ selection, not broad try/except."""
    target_files = (
        SERVING / "council_prompt_receipt.py",
        SERVING / "taey_seat.py",
        SERVING / "soma_proxy.py",
        SERVING / "validate_linkedin_prepare_thinking_policy.py",
        SERVING / "validate_outbound_request_receipt_bytes.py",
    )
    for py_file in target_files:
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in tree.body:
            if isinstance(node, ast.Try):
                for handler in node.handlers:
                    if isinstance(handler.type, ast.Name) and handler.type.id == "ImportError":
                        raise AssertionError(
                            f"Forbidden broad try/except ImportError found in {py_file.name}"
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
    prove_shadow_module_isolation_in_package_context()
    prove_transitive_import_error_not_masked()
    prove_no_broad_try_except_import_error_in_serving_consumers()
    prove_unnamespaced_import_fails_red_without_serving_path()
    print("PASS: production-shaped repo-root and direct imports verified (deterministic package-context)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
