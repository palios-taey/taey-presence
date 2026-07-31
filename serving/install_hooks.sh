#!/usr/bin/env bash
# Install the public-clean gate as a local pre-commit hook. CI is the real enforcement; this just
# gives you the same answer before you push instead of after.
set -Eeuo pipefail
root="$(git rev-parse --show-toplevel)"
hook="$root/.git/hooks/pre-commit"
cat > "$hook" <<'HOOK'
#!/usr/bin/env bash
set -Eeuo pipefail
root="$(git rev-parse --show-toplevel)"
bash "$root/serving/check_public_clean.sh" "$root" || {
  echo "pre-commit: public-clean gate failed. Fix, or commit with --no-verify if you are certain." >&2
  exit 1
}
HOOK
chmod +x "$hook"
printf 'installed %s\n' "$hook"
