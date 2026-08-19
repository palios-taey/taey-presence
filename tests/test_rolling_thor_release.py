"""Adversarial tests for the signed, planner-only Thor release contract.

All tests use temporary files and generated Ed25519 keys. They never open SSH,
start systemd units, bake an artifact, or contact a model endpoint.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# Copied byte-for-byte from:
# /home/user/workspace/palios-training/schemas/release/fixtures/hub-decision-promote.valid.json
ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "rolling_thor_training_promote_receipt.json"
SPEC = importlib.util.spec_from_file_location("rolling_thor_release", ROOT / "serving" / "rolling_thor_release.py")
assert SPEC and SPEC.loader
rolling = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = rolling
SPEC.loader.exec_module(rolling)

CANDIDATE = "a" * 64
ROLLBACK = "b" * 64
OLDER = "c" * 64


def canonical_receipt(signer: dict | None = None, **overrides):
    """Load the exact copied shared-training fixture without field translation."""
    value = json.loads(FIXTURE.read_text())
    value["issued_at"] = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    value["subject"]["artifact_sha256"] = CANDIDATE
    value["subject"]["rollback_artifact_sha256"] = ROLLBACK
    if signer is not None:
        value["authority"]["trust_policy_sha256"] = signer["allowed_signers_sha256"]
    value.update(overrides)
    return value


@pytest.fixture
def signer(tmp_path: Path):
    private_key = tmp_path / "family-ed25519"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(private_key)],
        check=True,
    )
    allowed_signers = tmp_path / "allowed-signers"
    allowed_signers.write_text(f"taey-release-router {private_key.with_suffix('.pub').read_text()}")
    return {
        "private_key": private_key,
        "allowed_signers": allowed_signers,
        "allowed_signers_sha256": hashlib.sha256(allowed_signers.read_bytes()).hexdigest(),
        "ledger": tmp_path / "receipt-consumption.json",
    }


def fleet_file(tmp_path: Path, aliases: str = "taey ep3", node1_ssh: str = "taey@thor-one.invalid") -> Path:
    path = tmp_path / "fleet.env"
    path.write_text(
        "\n".join(
            [
                f"TAEY_NODE1_SSH={node1_ssh}",
                "TAEY_NODE2_SSH=taey@thor-two.invalid",
                "TAEY_NODE1_MODELS=/srv/taey/models",
                "TAEY_NODE2_MODELS=/srv/taey/models",
                "TAEY_NODE1_CONSUMERS=none",
                "TAEY_NODE2_CONSUMERS=none",
                f'TAEY_SERVED_NAME="{aliases}"',
                "TAEY_PRIMARY_SERVED_NAME=taey",
            ]
        )
        + "\n"
    )
    return path


def write_signed_receipt(tmp_path: Path, body: dict, signer: dict, name: str = "hub-decision.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(body, sort_keys=True))
    subprocess.run(
        [
            "ssh-keygen",
            "-Y",
            "sign",
            "-f",
            str(signer["private_key"]),
            "-n",
            body["authority"]["signature_namespace"],
            str(path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    return path


def dry_run_args(fleet: Path, decision: Path, signer: dict) -> list[str]:
    return [
        "--fleet-env", str(fleet),
        "--hub-decision-receipt", str(decision),
        "--hub-decision-signature", str(decision) + ".sig",
        "--allowed-signers", str(signer["allowed_signers"]),
        "--receipt-consumption-ledger", str(signer["ledger"]),
        "--artifact-sha256", CANDIDATE,
        "--rollback-artifact-sha256", ROLLBACK,
        "--candidate-source", "/srv/taey/incoming/candidate",
        "--staging-node", "node2",
        "--bake-command", "test -d \"$TAEY_RELEASE_STAGING\"",
        "--verify-command", "test -d \"$TAEY_RELEASE_STAGING\"",
    ]


def test_exact_copied_training_fixture_is_accepted_without_envelope_translation(tmp_path, signer, capsys):
    body = canonical_receipt(signer)
    fleet = fleet_file(tmp_path)
    decision = write_signed_receipt(tmp_path, body, signer)

    assert rolling.main(dry_run_args(fleet, decision, signer)) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["mode"] == "dry-run"
    assert output["campaign_id"] == body["campaign_id"]
    assert output["campaign_spec_sha256"] == body["campaign_spec_sha256"]
    assert output["transition"] == "promote"
    assert output["hub_decision_receipt_sha256"] == hashlib.sha256(decision.read_bytes()).hexdigest()
    assert output["artifact_sha256"] == CANDIDATE
    assert output["rollback_artifact_sha256"] == ROLLBACK
    assert output["consumer_aliases"] == ["taey", "ep3"]
    assert output["execution"].startswith("disabled")


def test_invalid_or_modified_receipt_signature_fails_closed(tmp_path, signer, capsys):
    fleet = fleet_file(tmp_path)
    body = canonical_receipt(signer)
    decision = write_signed_receipt(tmp_path, body, signer)
    decision.write_text(decision.read_text().replace(CANDIDATE, "d" * 64))

    assert rolling.main(dry_run_args(fleet, decision, signer)) == 3
    assert "signature" in capsys.readouterr().err
    assert not signer["ledger"].exists()


def test_forged_receipt_signed_by_untrusted_key_fails_closed(tmp_path, signer, capsys):
    fleet = fleet_file(tmp_path)
    attacker = dict(signer)
    attacker_key = tmp_path / "attacker-ed25519"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(attacker_key)],
        check=True,
    )
    attacker["private_key"] = attacker_key
    body = canonical_receipt(signer)
    decision = write_signed_receipt(tmp_path, body, attacker, "forged.json")

    assert rolling.main(dry_run_args(fleet, decision, signer)) == 3
    assert "signature" in capsys.readouterr().err
    assert not signer["ledger"].exists()


def test_stale_receipt_and_allowed_signer_pin_mismatch_fail_closed(tmp_path, signer, capsys):
    fleet = fleet_file(tmp_path)
    stale = canonical_receipt(signer, issued_at="2000-01-01T00:00:00Z")
    decision = write_signed_receipt(tmp_path, stale, signer, "stale.json")
    assert rolling.main(dry_run_args(fleet, decision, signer)) == 3
    assert "issuance window" in capsys.readouterr().err
    assert not signer["ledger"].exists()

    fresh_body = canonical_receipt(signer, receipt_id="trust-policy-mismatch")
    fresh_body["authority"]["trust_policy_sha256"] = "d" * 64
    fresh = write_signed_receipt(tmp_path, fresh_body, signer, "trust-policy-mismatch.json")
    args = dry_run_args(fleet, fresh, signer)
    assert rolling.main(args) == 3
    assert "trust_policy_sha256" in capsys.readouterr().err
    assert not signer["ledger"].exists()


@pytest.mark.parametrize(
    "mutator",
    [
        lambda body: body["authority"].pop("signer_identity"),
        lambda body: body["authority"].update({"signature_namespace": "different-namespace"}),
    ],
)
def test_authority_requires_six_fields_and_canonical_signature_namespace(tmp_path, signer, capsys, mutator):
    fleet = fleet_file(tmp_path)
    body = canonical_receipt(signer)
    mutator(body)
    decision = write_signed_receipt(tmp_path, body, signer, "invalid-authority.json")

    assert rolling.main(dry_run_args(fleet, decision, signer)) == 3
    assert "authority" in capsys.readouterr().err
    assert not signer["ledger"].exists()


def test_replayed_receipt_id_is_refused_after_consumption(tmp_path, signer, capsys):
    fleet = fleet_file(tmp_path)
    body = canonical_receipt(signer)
    decision = write_signed_receipt(tmp_path, body, signer)
    args = dry_run_args(fleet, decision, signer)

    assert rolling.main(args) == 0
    capsys.readouterr()
    assert rolling.main(args) == 3
    assert "already consumed" in capsys.readouterr().err


@pytest.mark.parametrize(
    "mutator",
    [
        lambda body: body["subject"].update({"consumer_aliases": ["taey", "ep3-candidate-v9"]}),
        lambda body: body["authority"].update({"actor_type": "user"}),
        lambda body: body.update({"authorization_plane": "user-chat"}),
        lambda body: body.update({"evidence": []}),
    ],
)
def test_signed_receipt_cannot_mutate_authority_aliases_or_evidence(tmp_path, signer, capsys, mutator):
    fleet = fleet_file(tmp_path)
    body = canonical_receipt(signer)
    mutator(body)
    decision = write_signed_receipt(tmp_path, body, signer)

    assert rolling.main(dry_run_args(fleet, decision, signer)) == 3
    assert "REFUSE" in capsys.readouterr().err


def test_digest_mismatch_missing_rollback_and_alias_mutation_fail_closed(tmp_path, signer, capsys):
    fleet = fleet_file(tmp_path)
    mismatch = write_signed_receipt(tmp_path, canonical_receipt(signer, subject={
        "artifact_sha256": "d" * 64,
        "rollback_artifact_sha256": ROLLBACK,
        "consumer_aliases": ["taey", "ep3"],
    }), signer, "mismatch.json")
    assert rolling.main(dry_run_args(fleet, mismatch, signer)) == 3
    assert "digest does not match" in capsys.readouterr().err

    missing = canonical_receipt(signer)
    del missing["subject"]["rollback_artifact_sha256"]
    missing_path = write_signed_receipt(tmp_path, missing, signer, "missing.json")
    assert rolling.main(dry_run_args(fleet, missing_path, signer)) == 3
    assert "subject is not exact" in capsys.readouterr().err

    mutated_fleet = fleet_file(tmp_path, aliases="taey ep3-20260819")
    fresh = write_signed_receipt(tmp_path, canonical_receipt(signer, receipt_id="fresh-alias-check"), signer, "alias.json")
    assert rolling.main(dry_run_args(mutated_fleet, fresh, signer)) == 3
    assert "stable aliases" in capsys.readouterr().err


def test_unsafe_ssh_destination_refuses_before_receipt_consumption(tmp_path, signer, capsys):
    fleet = fleet_file(tmp_path, node1_ssh="-oProxyCommand=evil")
    decision = write_signed_receipt(tmp_path, canonical_receipt(signer), signer)

    assert rolling.main(dry_run_args(fleet, decision, signer)) == 3
    assert "safe user@host" in capsys.readouterr().err
    assert not signer["ledger"].exists()


def test_apply_is_explicitly_disabled_without_consuming_receipt(tmp_path, signer, capsys):
    fleet = fleet_file(tmp_path)
    decision = write_signed_receipt(tmp_path, canonical_receipt(signer), signer)

    assert rolling.main(dry_run_args(fleet, decision, signer) + ["--apply"]) == 3
    assert "--apply is disabled" in capsys.readouterr().err
    assert not signer["ledger"].exists()


def test_atomic_pointers_retention_and_finalization_keep_immediate_rollback_previous(tmp_path):
    root = tmp_path / ".taey-release"
    releases = root / "releases"
    staging = root / "staging"
    for digest in (ROLLBACK, OLDER):
        (releases / digest).mkdir(parents=True, exist_ok=True)
    (staging / CANDIDATE).mkdir(parents=True)

    rolling.atomic_symlink(root, "current", f"releases/{ROLLBACK}")
    rolling.atomic_symlink(root, "previous", f"releases/{OLDER}")
    current_target, previous_target = rolling.candidate_pointer_targets(CANDIDATE, ROLLBACK)
    rolling.atomic_symlink(root, "current", current_target)
    rolling.atomic_symlink(root, "previous", previous_target)
    os.replace(staging / CANDIDATE, releases / CANDIDATE)
    rolling.atomic_symlink(root, "current", f"releases/{CANDIDATE}")

    assert rolling.validate_pointer(root, "current") == ("releases", CANDIDATE)
    assert rolling.validate_pointer(root, "previous") == ("releases", ROLLBACK)
    assert rolling.retention_delete_candidates(releases, CANDIDATE, ROLLBACK, set()) == [releases / OLDER]


def test_local_artifact_digest_rejects_symlink_and_special_file(tmp_path):
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    (artifact / "weights.bin").write_bytes(b"weights")
    digest = rolling.local_manifest_digest(artifact)
    assert len(digest) == 64

    os.symlink("weights.bin", artifact / "linked.bin")
    with pytest.raises(rolling.Refusal, match="symlink"):
        rolling.local_manifest_digest(artifact)
    (artifact / "linked.bin").unlink()

    fifo = artifact / "special.fifo"
    os.mkfifo(fifo)
    with pytest.raises(rolling.Refusal, match="special"):
        rolling.local_manifest_digest(artifact)
