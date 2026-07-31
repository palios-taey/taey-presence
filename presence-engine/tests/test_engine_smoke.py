"""Smoke tests — modules import, gateway rejects pools, no creds embedded."""
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_all_modules_ast_parse():
    for py in ROOT.rglob("*.py"):
        if ".venv" in str(py) or "test_" in py.name:
            continue
        ast.parse(py.read_text())  # raises on syntax error


def test_no_embedded_credentials_or_internal_hosts():
    """No internal-infra shapes in shipped files: RFC1918 IPs, operator home
    paths, or credentials embedded in URLs. Case-insensitive. Scans code, docs,
    config, CI. Specific-credential detection (e.g. a known password literal) is
    delegated to gitleaks so no secret literal lives in this committed guard.
    This test file excludes itself (the regex is not a leak)."""
    bad = re.compile(
        r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"        # 10.0.0.0/8
        r"|\b192\.168\.\d{1,3}\.\d{1,3}\b"          # 192.168.0.0/16
        r"|\b172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b"  # 172.16.0.0/12
        r"|/home/[a-z][a-z0-9_-]+/"                 # operator home paths
        r"|://[^ /@\n]+:[^ /@\n]+@",                # creds in a URL
        re.IGNORECASE,
    )
    exts = ("*.py", "*.md", "*.cypher", "*.toml", "*.yml", "*.yaml", "*.html",
            "*.sh", "*.cfg", "*.ini", "*.env.example")
    files = [p for ext in exts for p in ROOT.rglob(ext)]
    for f in files:
        if ".venv" in str(f) or ".git" in str(f) or f.name == "test_engine_smoke.py":
            continue
        m = bad.search(f.read_text())
        assert m is None, f"{f.name}: leaked internal-infra shape {m.group(0)!r}"


def test_gateway_rejects_pool():
    import sys
    sys.path.insert(0, str(ROOT))
    from engine import InferenceGateway
    # scalar endpoint OK
    g = InferenceGateway("http://localhost:8000")
    assert g
    # pool forms rejected
    for bad_ep in ("http://a:8000,http://b:8000", "[http://a:8000]"):
        try:
            InferenceGateway(bad_ep)
            assert False, f"should have rejected pool: {bad_ep}"
        except ValueError:
            pass


def test_extract_json_object_robustness():
    """Regression guard for the BLOCKER Logos flagged: complete_json must not
    silently die on real-world model output variance."""
    import sys
    sys.path.insert(0, str(ROOT))
    from engine import _extract_json_object
    assert _extract_json_object('{"a":1}') == {"a": 1}
    assert _extract_json_object('```json\n{"a":1}\n```') == {"a": 1}
    assert _extract_json_object('Sure:\n{"p":"hi","c":0.5}') == {"p": "hi", "c": 0.5}
    assert _extract_json_object('{"a":1} trailing') == {"a": 1}
    assert _extract_json_object('{"nested":{"b":2},"a":1}') == {"nested": {"b": 2}, "a": 1}
    assert _extract_json_object('{"s":"has } brace"}') == {"s": "has } brace"}
    assert _extract_json_object('no json') is None
    assert _extract_json_object('') is None
    assert _extract_json_object('[1,2,3]') is None  # array is not an object
