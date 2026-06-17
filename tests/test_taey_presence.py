"""Consolidated smoke + guard tests for the taey-presence package.

Static-only: imports, parser robustness, gateway pool-rejection, soma facet
clamping, probe shape, and the no-internal-infra-leak guard across the repo.
A live run against a real model/Redis is the behavioral oracle (not here).
"""
import ast
import importlib
import importlib.util
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_soma():
    spec = importlib.util.spec_from_file_location("soma_daemon", ROOT / "soma" / "soma_daemon.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_all_packages_import():
    for mod in ("presence.engine", "presence.face.expression", "presence.prediction.predictor",
                "presence.interrupt.interrupter", "presence.dcm.inter_instance",
                "presence.retrieval.backend", "dashboard.server"):
        importlib.import_module(mod)


def test_extract_json_object_robustness():
    from presence.engine import _extract_json_object
    assert _extract_json_object('{"a":1}') == {"a": 1}
    assert _extract_json_object('```json\n{"a":1}\n```') == {"a": 1}
    assert _extract_json_object('Sure:\n{"p":"hi"}') == {"p": "hi"}
    assert _extract_json_object('{"a":1} trailing') == {"a": 1}
    assert _extract_json_object('{"s":"has } brace"}') == {"s": "has } brace"}
    assert _extract_json_object('no json') is None
    assert _extract_json_object('[1,2,3]') is None


def test_gateway_rejects_pool():
    from presence.engine import InferenceGateway
    assert InferenceGateway("http://localhost:8000")
    for bad in ("http://a:8000,http://b:8000", "[http://a:8000]"):
        try:
            InferenceGateway(bad)
            assert False, f"should reject pool: {bad}"
        except ValueError:
            pass


def test_soma_facets_clamp():
    soma = _load_soma()
    f = soma.compute_facets(
        {"gpu_util_pct": 999, "power_w": 9999, "power_max_w": 1, "mem_used_mb": 9e9,
         "mem_total_mb": 1, "clock_mhz": 9999, "clock_max_mhz": 1, "gpu_temp_c": 999},
        cpu_temp=20.0, loadavg=99.0)
    assert set(f) == {"fluency", "clarity", "vitality", "presence", "warmth",
                      "capacity", "flow", "coherence"}
    assert all(0.0 <= v <= 1.0 for v in f.values())


def test_validation_probes_parse():
    pdir = ROOT / "validation" / "probes"
    for name in ("cannot_lie_probes.jsonl", "structured_output_probes.jsonl"):
        rows = [json.loads(ln) for ln in (pdir / name).read_text().splitlines() if ln.strip()]
        assert rows and all("id" in d for d in rows)


def test_no_internal_infra_shapes():
    """RFC1918 IPs / operator home paths / creds-in-URL must not ship. Case-
    insensitive. Specific-credential detection is delegated to gitleaks; no
    secret literal lives in this guard."""
    bad = re.compile(
        r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b|\b192\.168\.\d{1,3}\.\d{1,3}\b"
        r"|\b172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b|/home/[a-z][a-z0-9_-]+/"
        r"|://[^ /@\n]+:[^ /@\n]+@",
        re.IGNORECASE,
    )
    exts = ("*.py", "*.md", "*.cypher", "*.toml", "*.yml", "*.yaml", "*.html",
            "*.sh", "*.cfg", "*.ini", "*.example", "*.jsonl")
    for f in (p for ext in exts for p in ROOT.rglob(ext)):
        if any(x in str(f) for x in (".venv", ".git", "test_taey_presence.py")):
            continue
        m = bad.search(f.read_text(errors="ignore"))
        assert m is None, f"{f.relative_to(ROOT)}: leaked infra shape {m.group(0)!r}"


def test_no_python_syntax_errors():
    for py in ROOT.rglob("*.py"):
        if ".venv" in str(py):
            continue
        ast.parse(py.read_text())
