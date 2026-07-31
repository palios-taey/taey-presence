#!/usr/bin/env python3
from __future__ import annotations

import base64
import hashlib
import json
import os
import posixpath
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
DEFAULT_INDEX = HERE / "index.json"

EXIT_ACCEPT = 0
EXIT_REFUSE = 3
EXIT_CHECKER_ERROR = 1

INDEX_PATH_ENV = "TAEY_RECEIPT_INDEX_PATH"
LIVE_INDEX_PATH_ENV = "TAEY_RECEIPT_LIVE_INDEX_PATH"
FIXTURE_ROOT_ENV = "TAEY_RECEIPT_FIXTURE_ROOT"

REFUSE_INDEX_STALE = "index-stale"
REFUSE_NOT_IN_INDEX = "not-in-index"
REFUSE_BINDING_MISMATCH = "binding-mismatch"
REFUSE_NO_RECEIPT = "no-receipt"
REFUSE_UNREACHABLE_SHA = "unreachable-sha"
REFUSE_GATE_NOT_GREEN = "gate-not-green"
REFUSE_UNTRUSTED_ACTOR = "untrusted-actor"
REFUSE_NOT_LIVE = "not-live"
REASON_CHECKER_ERROR = "checker-error"


class Refusal(Exception):
    def __init__(self, reason: str, receipt_sha256: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.receipt_sha256 = receipt_sha256


class CheckerError(Exception):
    pass


class MissingRemote(Exception):
    pass


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def json_line(verdict: str, surface_id: str, reason: str, receipt_sha256: str = "") -> str:
    return json.dumps(
        {
            "verdict": verdict,
            "surface_id": surface_id,
            "reason": reason,
            "checked_at": now_utc(),
            "receipt_sha256": receipt_sha256,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def output(verdict: str, surface_id: str, reason: str, receipt_sha256: str = "") -> None:
    print(json_line(verdict, surface_id, reason, receipt_sha256))


def repo_key(repo: str) -> str:
    return repo.replace("/", "__")


def fixture_root() -> Path | None:
    raw = os.environ.get(FIXTURE_ROOT_ENV)
    return Path(raw).resolve() if raw else None


def read_json_path(path: Path) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
        doc = json.loads(raw)
    except OSError as exc:
        raise CheckerError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise CheckerError(f"{path} is not JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise CheckerError(f"{path} root is not an object")
    return doc, raw


def adopted_index_path() -> Path:
    return Path(os.environ.get(INDEX_PATH_ENV, DEFAULT_INDEX)).resolve()


def load_adopted_index() -> tuple[dict[str, Any], bytes]:
    return read_json_path(adopted_index_path())


def request_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "taey-receipt-check",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_url(url: str) -> bytes:
    req = urllib.request.Request(url, headers=request_headers())
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise MissingRemote(url) from exc
        raise CheckerError(f"HTTP {exc.code} while fetching {url}") from exc
    except urllib.error.URLError as exc:
        raise CheckerError(f"cannot fetch {url}: {exc}") from exc


def fetch_json_url(url: str) -> dict[str, Any]:
    try:
        data = json.loads(fetch_url(url))
    except json.JSONDecodeError as exc:
        raise CheckerError(f"{url} did not return JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise CheckerError(f"{url} JSON root is not an object")
    return data


def fixture_file(root: Path, *parts: str) -> Path:
    return root.joinpath(*parts)


def fetch_live_index(index: dict[str, Any]) -> bytes:
    override = os.environ.get(LIVE_INDEX_PATH_ENV)
    if override:
        try:
            return Path(override).read_bytes()
        except OSError as exc:
            raise CheckerError(f"cannot read live index override {override}: {exc}") from exc
    live_url = index.get("live_url")
    if not isinstance(live_url, str) or not live_url:
        raise CheckerError("adopted index has no live_url")
    return fetch_url(live_url)


def fetch_repo_file(repo: str, ref: str, rel_path: str) -> bytes:
    root = fixture_root()
    if root:
        target = fixture_file(root, "contents", repo_key(repo), ref, *Path(rel_path).parts)
        if not target.is_file():
            raise MissingRemote(str(target))
        return target.read_bytes()

    quoted_path = urllib.parse.quote(rel_path, safe="/")
    quoted_ref = urllib.parse.quote(ref, safe="")
    url = f"https://api.github.com/repos/{repo}/contents/{quoted_path}?ref={quoted_ref}"
    data = fetch_json_url(url)
    content = data.get("content")
    encoding = data.get("encoding")
    if encoding != "base64" or not isinstance(content, str):
        raise CheckerError(f"GitHub contents response for {repo}:{rel_path}@{ref} has no base64 content")
    try:
        return base64.b64decode(content, validate=False)
    except ValueError as exc:
        raise CheckerError(f"GitHub contents response for {repo}:{rel_path}@{ref} is invalid base64") from exc


def fetch_repo_json(repo: str, ref: str, rel_path: str) -> dict[str, Any]:
    raw = fetch_repo_file(repo, ref, rel_path)
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CheckerError(f"{repo}:{rel_path}@{ref} is not JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise CheckerError(f"{repo}:{rel_path}@{ref} JSON root is not an object")
    return obj


def github_statuses(repo: str, sha: str) -> list[dict[str, Any]]:
    root = fixture_root()
    if root:
        path = fixture_file(root, "statuses", repo_key(repo), f"{sha}.json")
        if not path.exists():
            return []
        data = json.loads(path.read_text())
        if not isinstance(data, list):
            raise CheckerError(f"{path} root is not a list")
        return data
    quoted_sha = urllib.parse.quote(sha, safe="")
    url = f"https://api.github.com/repos/{repo}/commits/{quoted_sha}/statuses"
    data = json.loads(fetch_url(url))
    if not isinstance(data, list):
        raise CheckerError(f"{url} JSON root is not a list")
    return data


def github_check_runs(repo: str, sha: str) -> list[dict[str, Any]]:
    root = fixture_root()
    if root:
        path = fixture_file(root, "check_runs", repo_key(repo), f"{sha}.json")
        if not path.exists():
            return []
        data = json.loads(path.read_text())
    else:
        quoted_sha = urllib.parse.quote(sha, safe="")
        url = f"https://api.github.com/repos/{repo}/commits/{quoted_sha}/check-runs"
        data = fetch_json_url(url)
    runs = data.get("check_runs") if isinstance(data, dict) else data
    if not isinstance(runs, list):
        raise CheckerError(f"check-runs payload for {repo}@{sha} is not a list")
    return runs


def artifact_reachable_from_default(repo: str, sha: str) -> bool:
    root = fixture_root()
    if root:
        path = fixture_file(root, "reachable", f"{repo_key(repo)}.json")
        if not path.exists():
            return False
        data = json.loads(path.read_text())
        if not isinstance(data, list):
            raise CheckerError(f"{path} root is not a list")
        return sha in data

    repo_info = fetch_json_url(f"https://api.github.com/repos/{repo}")
    default_branch = repo_info.get("default_branch")
    if not isinstance(default_branch, str) or not default_branch:
        raise CheckerError(f"cannot determine default branch for {repo}")
    return compare_head_reaches_or_equals_base(repo, sha, default_branch)


def compare_head_reaches_or_equals_base(repo: str, base_ref: str, head_ref: str) -> bool:
    base = urllib.parse.quote(base_ref, safe="")
    head = urllib.parse.quote(head_ref, safe="")
    try:
        compare = fetch_json_url(f"https://api.github.com/repos/{repo}/compare/{base}...{head}")
    except MissingRemote:
        return False
    # GitHub reports HEAD relative to BASE: base...head is "ahead" when head contains base.
    return compare.get("status") in {"ahead", "identical"}


def commit_ancestor_or_equal(repo: str, ancestor_sha: str, descendant_sha: str) -> bool:
    if ancestor_sha == descendant_sha:
        return True

    root = fixture_root()
    if root:
        path = fixture_file(root, "ancestors", f"{repo_key(repo)}.json")
        if not path.exists():
            return False
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise CheckerError(f"{path} root is not an object")
        ancestors = data.get(descendant_sha, [])
        if not isinstance(ancestors, list):
            raise CheckerError(f"{path} entry for {descendant_sha} is not a list")
        return ancestor_sha in ancestors

    return compare_head_reaches_or_equals_base(repo, ancestor_sha, descendant_sha)


def production_entries(index: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    present = set(index.get("sections_present") or [])
    entries: list[tuple[str, dict[str, Any]]] = []
    sections = index.get("sections") or {}
    if not isinstance(sections, dict):
        return entries
    for section_name in sorted(present):
        section = sections.get(section_name) or {}
        for cap in section.get("capabilities", []):
            if isinstance(cap, dict) and cap.get("status") == "production":
                entries.append((section_name, cap))
    return entries


def find_entry(index: dict[str, Any], surface_id: str) -> tuple[str, dict[str, Any]]:
    for section_name, cap in production_entries(index):
        if cap.get("id") == surface_id:
            return section_name, cap
    raise Refusal(REFUSE_NOT_IN_INDEX)


def canonical_entry_ref(section_name: str, surface_id: str) -> str:
    return f"sections.{section_name}.capabilities.{surface_id}"


def index_ref_matches(ref: Any, section_name: str, surface_id: str) -> bool:
    if not isinstance(ref, str):
        return False
    canonical = canonical_entry_ref(section_name, surface_id)
    return ref in {
        surface_id,
        canonical,
        f"$.{canonical}",
        f"/sections/{section_name}/capabilities/{surface_id}",
    }


def validate_receipt_bindings(
    _index: dict[str, Any],
    section_name: str,
    entry: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    repo = (entry.get("repo") or {}).get("name")
    pinned_sha = (entry.get("repo") or {}).get("pinned_sha")
    receipts = entry.get("receipts") or {}
    receipt_path = receipts.get("liveness")
    expected_receipt_sha = receipts.get("liveness_sha256")

    if not isinstance(expected_receipt_sha, str) or not expected_receipt_sha:
        raise Refusal(REFUSE_BINDING_MISMATCH)
    if not all(isinstance(x, str) and x for x in (repo, pinned_sha, receipt_path)):
        raise Refusal(REFUSE_BINDING_MISMATCH)

    try:
        receipt_raw = fetch_repo_file(repo, pinned_sha, receipt_path)
    except MissingRemote as exc:
        raise Refusal(REFUSE_NO_RECEIPT) from exc
    actual_receipt_sha = sha256_bytes(receipt_raw)
    if actual_receipt_sha != expected_receipt_sha:
        raise Refusal(REFUSE_BINDING_MISMATCH, actual_receipt_sha)

    try:
        receipt = json.loads(receipt_raw)
    except json.JSONDecodeError as exc:
        raise Refusal(REFUSE_BINDING_MISMATCH, actual_receipt_sha) from exc
    if not isinstance(receipt, dict) or receipt.get("receipt_version") != 2:
        raise Refusal(REFUSE_BINDING_MISMATCH, actual_receipt_sha)

    liveness = receipt.get("liveness") or {}
    entry_liveness = entry.get("liveness") or {}
    entry_manifest = entry.get("artifact_manifest") or {}
    manifest_path = entry_manifest.get("path")
    manifest_sha = entry_manifest.get("sha256")
    compiled_at_commit = receipt.get("compiled_at_commit")

    checks = [
        receipt.get("surface_id") == entry.get("id"),
        receipt.get("repo") == repo,
        receipt.get("artifact_commit_sha") == entry.get("artifact_commit_sha"),
        receipt.get("artifact_manifest_sha256") == manifest_sha,
        liveness.get("probe_cmd") == entry_liveness.get("probe_cmd"),
        liveness.get("expect") == entry_liveness.get("expect"),
        index_ref_matches(receipt.get("index_entry_ref"), section_name, str(entry.get("id"))),
    ]
    if not all(checks):
        raise Refusal(REFUSE_BINDING_MISMATCH, actual_receipt_sha)
    if not isinstance(compiled_at_commit, str) or not compiled_at_commit:
        raise Refusal(REFUSE_BINDING_MISMATCH, actual_receipt_sha)
    if not commit_ancestor_or_equal(repo, compiled_at_commit, pinned_sha):
        raise Refusal(REFUSE_BINDING_MISMATCH, actual_receipt_sha)
    if not isinstance(manifest_path, str) or not manifest_path or not isinstance(manifest_sha, str):
        raise Refusal(REFUSE_BINDING_MISMATCH, actual_receipt_sha)

    try:
        manifest_raw = fetch_repo_file(repo, pinned_sha, manifest_path)
    except MissingRemote as exc:
        raise Refusal(REFUSE_BINDING_MISMATCH, actual_receipt_sha) from exc
    if sha256_bytes(manifest_raw) != manifest_sha:
        raise Refusal(REFUSE_BINDING_MISMATCH, actual_receipt_sha)

    return receipt, actual_receipt_sha


def validate_gates_manifest(manifest: dict[str, Any]) -> tuple[list[str], set[str], set[str]]:
    if set(manifest) != {"manifest_version", "required_contexts", "trusted_actors"}:
        raise CheckerError("gates manifest schema is not exact")
    if manifest.get("manifest_version") != 1:
        raise CheckerError("gates manifest version must be 1")
    contexts = manifest.get("required_contexts")
    actors = manifest.get("trusted_actors")
    if (
        not isinstance(contexts, list)
        or not contexts
        or not all(isinstance(x, str) and x for x in contexts)
        or not isinstance(actors, dict)
        or set(actors) != {"apps", "logins"}
    ):
        raise CheckerError("gates manifest schema is invalid")
    apps = actors.get("apps")
    logins = actors.get("logins")
    if not (
        isinstance(apps, list)
        and isinstance(logins, list)
        and all(isinstance(x, str) and x for x in apps)
        and all(isinstance(x, str) and x for x in logins)
    ):
        raise CheckerError("gates manifest trusted_actors schema is invalid")
    return contexts, set(apps), set(logins)


def gate_actor_state(
    context: str,
    statuses: list[dict[str, Any]],
    check_runs: list[dict[str, Any]],
    trusted_apps: set[str],
    trusted_logins: set[str],
) -> str:
    saw_success_untrusted = False

    for status in statuses:
        if status.get("context") != context or status.get("state") != "success":
            continue
        creator = status.get("creator") or {}
        login = creator.get("login")
        if isinstance(login, str) and login in trusted_logins:
            return "trusted"
        saw_success_untrusted = True

    for run in check_runs:
        if (
            run.get("name") != context
            or run.get("status") != "completed"
            or run.get("conclusion") != "success"
        ):
            continue
        app = run.get("app") or {}
        slug = app.get("slug")
        if isinstance(slug, str) and slug in trusted_apps:
            return "trusted"
        saw_success_untrusted = True

    return "untrusted" if saw_success_untrusted else "not-green"


def validate_gates(entry: dict[str, Any], receipt: dict[str, Any], receipt_sha: str) -> None:
    repo = (entry.get("repo") or {}).get("name")
    artifact_sha = entry.get("artifact_commit_sha")
    ref = receipt.get("gates_manifest_ref")
    if not all(isinstance(x, str) and x for x in (repo, artifact_sha, ref)):
        raise Refusal(REFUSE_GATE_NOT_GREEN, receipt_sha)
    try:
        manifest = fetch_repo_json(repo, artifact_sha, ref)
    except MissingRemote as exc:
        raise Refusal(REFUSE_GATE_NOT_GREEN, receipt_sha) from exc

    contexts, trusted_apps, trusted_logins = validate_gates_manifest(manifest)
    statuses = github_statuses(repo, artifact_sha)
    check_runs = github_check_runs(repo, artifact_sha)

    for context in contexts:
        state = gate_actor_state(context, statuses, check_runs, trusted_apps, trusted_logins)
        if state == "trusted":
            continue
        if state == "untrusted":
            raise Refusal(REFUSE_UNTRUSTED_ACTOR, receipt_sha)
        raise Refusal(REFUSE_GATE_NOT_GREEN, receipt_sha)


def validate_reachable(entry: dict[str, Any], receipt_sha: str) -> None:
    repo = (entry.get("repo") or {}).get("name")
    artifact_sha = entry.get("artifact_commit_sha")
    if not isinstance(repo, str) or not isinstance(artifact_sha, str) or not artifact_sha:
        raise Refusal(REFUSE_UNREACHABLE_SHA, receipt_sha)
    if not artifact_reachable_from_default(repo, artifact_sha):
        raise Refusal(REFUSE_UNREACHABLE_SHA, receipt_sha)


def run_probe(cmd: str) -> bytes:
    try:
        proc = subprocess.run(
            ["bash", "-c", cmd],
            cwd=REPO_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise Refusal(REFUSE_NOT_LIVE) from exc
    if proc.returncode != 0:
        raise Refusal(REFUSE_NOT_LIVE)
    return proc.stdout


def validate_liveness(entry: dict[str, Any], receipt_sha: str) -> None:
    liveness = entry.get("liveness") or {}
    cmd = liveness.get("probe_cmd")
    expect = liveness.get("expect") or {}
    lang = expect.get("lang")
    predicate = expect.get("predicate")
    if not all(isinstance(x, str) and x for x in (cmd, lang, predicate)):
        raise Refusal(REFUSE_NOT_LIVE, receipt_sha)

    stdout = run_probe(cmd)
    if lang == "jq":
        try:
            proc = subprocess.run(
                ["jq", "-e", predicate],
                input=stdout,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=30,
            )
        except FileNotFoundError as exc:
            raise CheckerError("jq is required for jq liveness predicates") from exc
        except subprocess.TimeoutExpired as exc:
            raise Refusal(REFUSE_NOT_LIVE, receipt_sha) from exc
        if proc.returncode != 0:
            err = proc.stderr.decode(errors="replace")
            if "syntax error" in err or "compile error" in err:
                raise CheckerError("jq predicate cannot be evaluated")
            raise Refusal(REFUSE_NOT_LIVE, receipt_sha)
        return

    if lang == "text":
        try:
            proc = subprocess.run(
                ["grep", "-qE", predicate],
                input=stdout,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=30,
            )
        except subprocess.TimeoutExpired as exc:
            raise Refusal(REFUSE_NOT_LIVE, receipt_sha) from exc
        if proc.returncode == 2:
            raise CheckerError("text predicate is not a valid POSIX ERE")
        if proc.returncode != 0:
            raise Refusal(REFUSE_NOT_LIVE, receipt_sha)
        return

    raise CheckerError(f"unsupported liveness predicate language: {lang}")


def check_surface(surface_id: str) -> str:
    index, index_raw = load_adopted_index()

    live_raw = fetch_live_index(index)
    if sha256_bytes(live_raw) != sha256_bytes(index_raw):
        raise Refusal(REFUSE_INDEX_STALE)

    section_name, entry = find_entry(index, surface_id)
    receipt, receipt_sha = validate_receipt_bindings(index, section_name, entry)
    validate_reachable(entry, receipt_sha)
    validate_gates(entry, receipt, receipt_sha)
    validate_liveness(entry, receipt_sha)
    return receipt_sha


def walk_strings(node: Any) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for value in node.values():
            found.extend(walk_strings(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(walk_strings(value))
    elif isinstance(node, str):
        found.append(node)
    return found


def normalize_lookup_value(raw: str) -> str:
    value = raw.strip()
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme and parsed.netloc:
        scheme = parsed.scheme.lower()
        netloc = parsed.netloc.lower()
        path = posixpath.normpath(parsed.path or "/")
        if path == ".":
            path = "/"
        if path != "/" and raw.endswith("/"):
            path = path.rstrip("/")
        return urllib.parse.urlunsplit((scheme, netloc, path, parsed.query, parsed.fragment))
    if "/" in value or value.startswith("."):
        leading = value.startswith("/")
        path = posixpath.normpath(value)
        if path == ".":
            path = ""
        if not leading:
            path = path.lstrip("/")
        return path.rstrip("/") if path != "/" else path
    return value


def candidate_strings(capability: dict[str, Any]) -> set[str]:
    out = set()
    for value in walk_strings(capability):
        out.add(normalize_lookup_value(value))
    return out


def resolve_target(target: str) -> str:
    index, _ = load_adopted_index()
    needle = normalize_lookup_value(target)
    matches = []
    for _, cap in production_entries(index):
        if needle in candidate_strings(cap):
            matches.append(str(cap.get("id")))
    unique = sorted(set(matches))
    if len(unique) == 1:
        return unique[0]
    raise Refusal(REFUSE_NOT_IN_INDEX)


def main_check(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        output("REFUSE", "", REASON_CHECKER_ERROR)
        return EXIT_CHECKER_ERROR
    surface_id = args[0]
    try:
        receipt_sha = check_surface(surface_id)
    except Refusal as exc:
        output("REFUSE", surface_id, exc.reason, exc.receipt_sha256)
        return EXIT_REFUSE
    except Exception:
        output("REFUSE", surface_id, REASON_CHECKER_ERROR)
        return EXIT_CHECKER_ERROR
    output("ACCEPT", surface_id, "accepted", receipt_sha)
    return EXIT_ACCEPT


def main_resolve(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        output("REFUSE", "", REASON_CHECKER_ERROR)
        return EXIT_CHECKER_ERROR
    try:
        surface_id = resolve_target(args[0])
    except Refusal as exc:
        output("REFUSE", "", exc.reason)
        return EXIT_REFUSE
    except Exception:
        output("REFUSE", "", REASON_CHECKER_ERROR)
        return EXIT_CHECKER_ERROR
    print(json.dumps({"surface_id": surface_id}, separators=(",", ":"), sort_keys=True))
    return EXIT_ACCEPT


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        output("REFUSE", "", REASON_CHECKER_ERROR)
        return EXIT_CHECKER_ERROR
    command, rest = args[0], args[1:]
    if command == "check":
        return main_check(rest)
    if command == "resolve":
        return main_resolve(rest)
    output("REFUSE", "", REASON_CHECKER_ERROR)
    return EXIT_CHECKER_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
