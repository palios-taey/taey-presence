import argparse
import hashlib
import json
import os
import stat
import sys
import tempfile
from pathlib import Path


TARGET = "/home/mira/.peer-worktrees/infra-codex-vslice-collect"
sys.path.insert(0, TARGET)

from fleet_orchestrator import cli_taey_delegate as delegate


root = Path(tempfile.mkdtemp(prefix="conductor-codex-reaudit-"))
real_replace = delegate.os.replace
real_fsync = delegate.os.fsync


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


print(f"fixture={root}")

artifact = root / "mutated-at-replace.bin"
output = root / "mutation-manifest.json"
artifact.write_bytes(b"A" * 4096)


def mutate_then_replace(source, destination):
    artifact.write_bytes(b"B" * 4096)
    return real_replace(source, destination)


delegate.os.replace = mutate_then_replace
try:
    rc = delegate.cmd_collect(argparse.Namespace(files=[str(artifact)], output=str(output)))
finally:
    delegate.os.replace = real_replace
manifest = json.loads(output.read_text())
record = manifest["artifacts"][0]
print("\nCASE 1: same-size uncooperative write after final stability sweep")
print(f"exit={rc}")
print(f"recorded_sha={record['sha256']}")
print(f"disk_sha={digest(artifact)}")
print(f"recorded_matches_disk={record['sha256'] == digest(artifact)}")
print(f"recorded_bytes={record['bytes']} disk_bytes={artifact.stat().st_size}")

artifact = root / "renamed-at-replace.bin"
output = root / "rename-manifest.json"
artifact.write_bytes(b"artifact-survives-only-on-open-fd")


def rename_then_replace(source, destination):
    artifact.rename(output)
    return real_replace(source, destination)


delegate.os.replace = rename_then_replace
try:
    rc = delegate.cmd_collect(argparse.Namespace(files=[str(artifact)], output=str(output)))
finally:
    delegate.os.replace = real_replace
manifest = json.loads(output.read_text())
record = manifest["artifacts"][0]
print("\nCASE 2: artifact rename after final stability sweep")
print(f"exit={rc}")
print(f"recorded_path={record['path']}")
print(f"recorded_exists={record['exists']}")
print(f"disk_exists_after_success={artifact.exists()}")

artifact = root / "fsync-observation.bin"
output = root / "fsync-manifest.json"
artifact.write_bytes(b"fsync-observation")
fsync_targets = []


def record_fsync(fd):
    mode = os.fstat(fd).st_mode
    fsync_targets.append("directory" if stat.S_ISDIR(mode) else "regular")
    return real_fsync(fd)


delegate.os.fsync = record_fsync
try:
    rc = delegate.cmd_collect(argparse.Namespace(files=[str(artifact)], output=str(output)))
finally:
    delegate.os.fsync = real_fsync
print("\nCASE 3: successful transaction fsync targets")
print(f"exit={rc}")
print(f"fsync_targets={fsync_targets}")
print(f"directory_fsync_observed={'directory' in fsync_targets}")
