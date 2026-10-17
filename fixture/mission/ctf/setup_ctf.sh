#!/usr/bin/env bash
# Idempotent setup of the cFS Test Framework (CTF v1.11) for this mission.
#
# Creates, next to this script:
#   CTF/    clone of github.com/nasa/CTF at the pinned commit (tag v1.11)
#   venv/   Python 3.12 virtualenv with loosened requirements plus shims
# Re-running only repairs what is missing.
#
# Environment overrides:
#   CTF_PYTHON   python interpreter to use (default: python3.12)
#   CTF_REPO     upstream git URL (default: https://github.com/nasa/CTF)
#   CTF_CACHE_DIR  local clone used as the clone source (default: /workspace/agent-experiments-cache/CTF)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CTF_DIR="$HERE/CTF"
VENV_DIR="$HERE/venv"
SHIMS_DIR="$HERE/shims"
CTF_REPO="${CTF_REPO:-https://github.com/nasa/CTF}"
CTF_TAG="v1.11"
CTF_COMMIT="1cd6cae29d934e2cd15975a71aea966f9108c3ec"
PYTHON="${CTF_PYTHON:-python3.12}"
CACHE_DIR="${CTF_CACHE_DIR:-/workspace/agent-experiments-cache/CTF}"

die() { echo "setup_ctf.sh: ERROR: $*" >&2; exit 1; }

# 1. Interpreter. CTF's pinned requirements do not build on 3.13+, and the
#    shims in shims/ are validated against 3.12 only.
command -v "$PYTHON" >/dev/null 2>&1 \
  || die "'$PYTHON' not found. Install Python 3.12 (e.g. 'dnf install python3.12' or 'apt install python3.12 python3.12-venv') or set CTF_PYTHON=/path/to/python3.12. No system packages are installed by this script."
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)' \
  || die "'$PYTHON' is $("$PYTHON" -V 2>&1), but Python 3.12 is required."

# 2. CTF source at the pinned commit.
if [ ! -f "$CTF_DIR/ctf" ]; then
  command -v git >/dev/null 2>&1 || die "git not found; it is needed to clone CTF."
  # GitHub is contacted only to populate the cache; checkouts clone from the cache.
  if [ "$(git -C "$CACHE_DIR" rev-parse HEAD 2>/dev/null || true)" != "$CTF_COMMIT" ]; then
    rm -rf "$CACHE_DIR"
    mkdir -p "$(dirname "$CACHE_DIR")"
    git clone --quiet "$CTF_REPO" "$CACHE_DIR" || die "cannot clone $CTF_REPO into $CACHE_DIR (network access needed once)."
    git -C "$CACHE_DIR" -c advice.detachedHead=false checkout --quiet "$CTF_COMMIT" \
      || die "cannot check out $CTF_COMMIT ($CTF_TAG) in $CACHE_DIR."
  fi
  rm -rf "$CTF_DIR"
  git clone --quiet "$CACHE_DIR" "$CTF_DIR" || die "cannot clone cache $CACHE_DIR."
  git -C "$CTF_DIR" -c advice.detachedHead=false checkout --quiet "$CTF_COMMIT" \
    || die "cannot check out $CTF_COMMIT ($CTF_TAG) in $CTF_DIR."
fi
ACTUAL="$(git -C "$CTF_DIR" rev-parse HEAD 2>/dev/null || echo unknown)"
[ "$ACTUAL" = "$CTF_COMMIT" ] \
  || echo "setup_ctf.sh: WARNING: $CTF_DIR is at $ACTUAL, expected $CTF_TAG ($CTF_COMMIT)." >&2

# 3. Virtualenv and loosened requirements. A marker file records completion
#    so an interrupted install is redone on the next run.
if [ ! -x "$VENV_DIR/bin/python" ]; then
  rm -rf "$VENV_DIR"
  "$PYTHON" -m venv "$VENV_DIR" || die "cannot create virtualenv (is the python3.12-venv package installed?)."
fi
REQ_STAMP="$VENV_DIR/.ctf_requirements_installed"
REQ_KEY="$(cat "$SHIMS_DIR/requirements.txt" "$SHIMS_DIR/constraints.txt")"
if [ ! -f "$REQ_STAMP" ] || [ "$(cat "$REQ_STAMP")" != "$REQ_KEY" ]; then
  # requirements.txt lists the loosened (unpinned) top-level packages; constraints.txt
  # fixes every version to the set this fixture was validated with.
  "$VENV_DIR/bin/python" -m pip install --quiet --disable-pip-version-check -r "$SHIMS_DIR/requirements.txt" -c "$SHIMS_DIR/constraints.txt" \
    || die "installing $SHIMS_DIR/requirements.txt failed (network access needed)."
  printf '%s' "$REQ_KEY" > "$REQ_STAMP"
fi

# 4. Shims: 'demjson' (legacy name for demjson3) and the 'six' module that
#    invoke 2.x no longer vendors but fabric 3 / CTF still reach for.
SITE="$("$VENV_DIR/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
cp "$SHIMS_DIR/demjson.py" "$SITE/demjson.py"
INVOKE_VENDOR="$SITE/invoke/vendor"
[ -d "$INVOKE_VENDOR" ] || die "invoke is not installed correctly: $INVOKE_VENDOR missing."
cp "$SHIMS_DIR/six.py" "$INVOKE_VENDOR/six.py"

# 5. Final check.
(cd "$CTF_DIR" && "$VENV_DIR/bin/python" ctf --help >/dev/null) \
  || die "'ctf --help' fails after setup; run it by hand in $CTF_DIR to see the error."
echo "CTF ready: $VENV_DIR/bin/python $CTF_DIR/ctf"
