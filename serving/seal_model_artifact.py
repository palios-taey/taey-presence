#!/usr/bin/env python3
"""Seal one stopped model artifact for serving-host identity attestation."""

from __future__ import annotations

import errno
import fcntl
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

from model_identity_attestor import (
    AttestationError,
    SEAL_NAME,
    immutable_regular_files,
    parse_seal,
)


def require_stopped_serving() -> None:
    active = subprocess.run(
        ["systemctl", "is-active", "--quiet", "taey-ep3.service"],
        check=False,
    )
    if active.returncode == 0:
        raise AttestationError("taey-ep3.service must be stopped before sealing")
    container = subprocess.run(
        ["docker", "inspect", "taey-vllm"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if container.returncode == 0:
        raise AttestationError("taey-vllm must be removed before sealing")


def sealable_files(root: Path) -> list[Path]:
    paths: list[Path] = []
    for path in root.rglob("*"):
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise AttestationError(f"artifact contains a symlink: {path.relative_to(root)}")
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise AttestationError(
                f"artifact contains a non-regular entry: {path.relative_to(root)}"
            )
        relative = path.relative_to(root).as_posix()
        if any(character in relative for character in ("\\", "\n", "\r")):
            raise AttestationError(f"artifact path cannot be sealed canonically: {relative!r}")
        paths.append(path)
    if not paths:
        raise AttestationError("artifact contains no regular files")
    return sorted(paths, key=lambda item: item.relative_to(root).as_posix())


def sha256sum(path: Path) -> str:
    result = subprocess.run(
        ["sha256sum", str(path)],
        text=True,
        capture_output=True,
        timeout=1800,
        check=False,
    )
    if result.returncode != 0:
        raise AttestationError(f"could not hash {path}")
    return result.stdout.split(" ", 1)[0]


def main() -> int:
    root_raw = os.environ.get("TAEY_MODEL_PATH", "")
    if not root_raw:
        raise AttestationError("TAEY_MODEL_PATH is required")
    root = Path(root_raw).resolve(strict=True)
    if not root.is_dir():
        raise AttestationError("TAEY_MODEL_PATH is not a directory")
    lock_descriptor = os.open(
        "/run/taey-model-artifact-seal.lock",
        os.O_CREAT | os.O_RDWR,
        0o600,
    )
    try:
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        os.close(lock_descriptor)
        raise AttestationError("model serving or another sealer owns the artifact lock") from error
    require_stopped_serving()
    seal = root / SEAL_NAME
    files = sealable_files(root)
    existing_seal = seal in files
    artifact_files = [path for path in files if path != seal]
    if existing_seal:
        parse_seal(root, files)
    elif not artifact_files:
        raise AttestationError("artifact contains no regular model files")
    manifest_name = os.environ.get(
        "TAEY_MODEL_IDENTITY_MANIFEST", "model.safetensors.index.json"
    )
    manifest_path = Path(manifest_name)
    if manifest_path.is_absolute() or ".." in manifest_path.parts or manifest_name in {"", "."}:
        raise AttestationError("TAEY_MODEL_IDENTITY_MANIFEST must be a safe relative path")
    if root / manifest_name not in artifact_files:
        raise AttestationError(f"artifact manifest is missing: {manifest_name}")
    temporary_name = ""
    installed_seal = False
    original_modes: dict[Path, int] = {}
    try:
        if not existing_seal:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{root.name}.{SEAL_NAME}.", dir=root.parent
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                for path in artifact_files:
                    handle.write(
                        f"{sha256sum(path)}  ./{path.relative_to(root).as_posix()}\n"
                    )
                handle.flush()
                os.fsync(handle.fileno())
            require_stopped_serving()
            os.chmod(temporary_name, 0o444)
            try:
                os.link(temporary_name, seal)
            except FileExistsError as error:
                raise AttestationError(
                    f"{SEAL_NAME} was created concurrently; never overwrite an artifact seal"
                ) from error
            except OSError as error:
                if error.errno == errno.EXDEV:
                    raise AttestationError(
                        "artifact seal staging and model root must share one filesystem"
                    ) from error
                raise
            installed_seal = True
            os.unlink(temporary_name)
            temporary_name = ""
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        paths_to_freeze = list(
            dict.fromkeys(
                [*artifact_files, seal, *sorted(root.rglob("*"), reverse=True), root]
            )
        )
        original_modes = {
            path: stat.S_IMODE(path.stat().st_mode) for path in paths_to_freeze
        }
        for path in paths_to_freeze:
            path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o222)
        parse_seal(root, immutable_regular_files(root))
    except BaseException:
        if installed_seal:
            if root in original_modes:
                root.chmod(original_modes[root])
            for path, mode in reversed(original_modes.items()):
                if path != root and path.exists():
                    path.chmod(mode)
            if seal.exists():
                seal.unlink()
        raise
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)
        os.close(lock_descriptor)
    action = "resumed" if existing_seal else "sealed"
    print(f"{action} {len(artifact_files)} artifact file(s) at {seal}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (AttestationError, OSError, ValueError) as error:
        print(f"FATAL: {error}", file=sys.stderr)
        sys.exit(1)
