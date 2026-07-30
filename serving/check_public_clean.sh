#!/usr/bin/env bash
# check_public_clean.sh — enforce the public-clean bar mechanically ON THE REPOSITORY.
#
# SCOPE, stated exactly: this reads the git repository — tracked files, commit history, and the
# working tree. It does NOT inspect anything deployed: not a running service, not a systemd unit,
# not a node's filesystem. A repo that passes this can still be running something else entirely,
# which is a real and separate failure class (verify that with a live probe, not with this).
#
# WHY THIS EXISTS. The bar was understood by everyone and enforced by nothing, so it was violated
# repeatedly by people who could recite it — including this file's author, who hardcoded two
# hostnames and two home directories into a promotion tool hours after writing the rule down. A
# rule with no gate is a rule that holds only while someone remembers it. This is the gate.
#
# WHAT THE BAR IS (operator-set, exact):
#   FILE PATHS ARE FINE. Sharing directory structure is not a leak, and this gate must never flag
#   `/home/<user>/...`. That is deliberate: an earlier instinct to scrub paths wasted effort and was
#   explicitly overruled. If you came here to add a path check, don't.
#   WHAT DOES FAIL, precisely — the claims here are narrowed to what is actually implemented,
#   because a gate that promises more coverage than it has is worse than a narrow honest one:
#     - routable IPv4 literals in executable code (loopback excluded; comment-only lines excluded)
#     - a PYTHON os.environ.get() whose default is a routable IPv4
#     - a missing committed site-config template, or an unignored real one
#     - secrets, scanned BOTH over full history AND over the current working tree
#   NOT covered, deliberately and stated so nobody assumes it: DNS hostnames and host:port literals
#   are NOT detected (too many legitimate .example/docs uses to separate mechanically), and non-Python
#   silent defaults are NOT detected.
#
# Exit 0 clean, 1 violations found, 2 could not run. Prints file:line for every hit so the finding
# is checkable rather than a claim.
set -Eeuo pipefail

ROOT="${1:-.}"
fails=0

hdr() { printf '\n== %s ==\n' "$*"; }
bad() { printf '  FAIL %s\n' "$*"; fails=$((fails + 1)); }
ok()  { printf '  ok   %s\n' "$*"; }

# Executable code only. Documentation, findings write-ups and comments citing evidence in other
# repos legitimately contain addresses — a LIMITS note that cites `other/file.py:26  HOST = "10.x"`
# is evidence, and generalising it would destroy the citation.
code_files() {
  git -C "$ROOT" ls-files 2>/dev/null \
    | grep -E '\.(py|sh|bash|service|yml|yaml|toml|json)$' \
    | grep -vE '(^|/)(tests?|examples?)/' || true
}

hdr "hardcoded IPv4 / host:port literals in executable code"
hits=""
while IFS= read -r f; do
  [ -n "$f" ] || continue
  # Skip comment-only lines: a citation or a worked example in a comment is not configuration.
  h=$(grep -nE '([0-9]{1,3}\.){3}[0-9]{1,3}' "$ROOT/$f" 2>/dev/null \
      | grep -vE '^[0-9]+:\s*#' \
      | grep -vE '127\.0\.0\.1|0\.0\.0\.0|localhost' || true)
  [ -n "$h" ] && hits="$hits\n$f:\n$(printf '%s' "$h" | sed 's/^/    /')"
done < <(code_files)
if [ -n "$hits" ]; then
  bad "routable IP literals found in executable code — move them to env with a fleet.env.example entry"
  printf '%b\n' "$hits"
else
  ok "no routable IPv4 literals in executable code (loopback and comment-only lines excluded)"
fi

hdr "required env vars fail loud rather than silent-defaulting"
# Loopback is a CORRECT default for a service whose peer is normally on the same host — flagging it
# would push people to invent required-env ceremony for something that already works out of the box.
# What must never be silently defaulted is a ROUTABLE address: that points a fresh install at
# somebody else's machine, which is the failure this whole bar exists to prevent.
soft=$(grep -rnE 'os\.environ\.get\("(TAEY|VLLM)_[A-Z_]*(HOST|SSH|URL|ADDR)[A-Z_]*",\s*"[^"]*([0-9]{1,3}\.){3}[0-9]{1,3}' \
        "$ROOT" --include=*.py 2>/dev/null \
        | grep -vE '127\.0\.0\.1|0\.0\.0\.0' || true)
if [ -n "$soft" ]; then
  bad "a host/address env var silently defaults to a ROUTABLE address — a fresh install would point at another machine"
  printf '%s\n' "$soft" | sed 's/^/    /'
else
  ok "no host/address env var silently defaults to a routable address (loopback defaults are fine)"
fi

hdr "site config template is committed and the real file is ignored"
if git -C "$ROOT" ls-files --error-unmatch serving/fleet.env.example >/dev/null 2>&1; then
  ok "serving/fleet.env.example is committed"
else
  bad "serving/fleet.env.example is missing — a released system needs one documented place to configure"
fi
if git -C "$ROOT" check-ignore -q serving/fleet.env 2>/dev/null; then
  ok "serving/fleet.env is gitignored"
else
  bad "serving/fleet.env is NOT gitignored — site config would be committed"
fi

hdr "secrets"
if ! command -v gitleaks >/dev/null 2>&1; then
  # A gate that PASSES when it could not run certifies an unscanned tree. Exit 2 means "could not
  # determine", which is a different and honest outcome from "clean".
  printf '  CANNOT RUN  gitleaks is not installed — the tree has NOT been scanned\n'
  printf '\nPUBLIC-CLEAN: INDETERMINATE (secret scan could not run)\n'
  exit 2
fi
# Two scans, because they cover different things and the earlier version claimed the wrong one:
# --log-opts=--all walks COMMITTED HISTORY (a secret removed from HEAD but alive in an old commit),
# --no-git reads the CURRENT FILESYSTEM (an uncommitted secret sitting in the working tree right now).
if gitleaks detect --source "$ROOT" --log-opts="--all" --redact --no-banner >/dev/null 2>&1; then
  ok "gitleaks: no leaks across full commit history"
else
  bad "gitleaks: findings in COMMIT HISTORY — gitleaks detect --source $ROOT --log-opts=--all --redact"
fi
if gitleaks detect --source "$ROOT" --no-git --redact --no-banner >/dev/null 2>&1; then
  ok "gitleaks: no leaks in the current working tree (including uncommitted files)"
else
  bad "gitleaks: findings in the WORKING TREE — gitleaks detect --source $ROOT --no-git --redact"
fi

hdr "file paths"
ok "NOT CHECKED, deliberately — sharing directory structure is fine and must not be flagged"

printf '\n'
if [ "$fails" -eq 0 ]; then printf 'PUBLIC-CLEAN: PASS\n'; exit 0; fi
printf 'PUBLIC-CLEAN: %d VIOLATION(S)\n' "$fails"; exit 1
