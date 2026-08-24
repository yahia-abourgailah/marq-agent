#!/usr/bin/env bash
# [claude] Run what CI runs, the way CI runs it.
#
# Exists because of a real miss. The CI environment was verified once, by
# hand, and then three test files were added without re-checking — two of
# which depended on a populated `.env.development` that CI does not have.
# CI went red on a change whose author had run the whole suite and watched
# it pass.
#
# The difference is not the tests, it is the environment:
#
#   *  a fresh clone, so nothing gitignored is present — no `var/`, no
#      uploaded workspace files, no local database state;
#   *  only `.env.development.example` plus the three placeholders the
#      workflow adds, so no populated credentials;
#   *  no inherited shell environment.
#
# This reproduces all three. It does not reproduce a fresh dependency
# install — that needs the network and several minutes, and it has never
# been the thing that broke.
#
#     ./scripts/ci_local.sh
#
# Keep the placeholder block below in step with .github/workflows/ci.yml.

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${PYTHON:-$repo_root/.venv/bin/python}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

echo "Cloning $repo_root into a scratch directory (committed state only)"
git clone --quiet "$repo_root" "$work/repo"
cd "$work/repo"

# Whatever is checked out here, run the working tree's version of it —
# otherwise this only ever tests the last commit, which is not what anyone
# is about to push.
echo "Overlaying uncommitted changes"
git --git-dir="$repo_root/.git" --work-tree="$repo_root" diff HEAD --binary \
  | git apply --whitespace=nowarn - 2>/dev/null || true

cp .env.development.example .env.development
{
  echo "MODEL_NAME=ci-model"
  echo "MODEL_BASE_URL=http://127.0.0.1:9/v1"
  echo "MODEL_API_KEY=ci-key-not-a-real-credential"
} >> .env.development

echo
echo "== ruff =="
env -i PATH="$PATH" HOME="$HOME" "$python_bin" -m ruff check .

echo
echo "== pytest (hermetic) =="
env -i PATH="$PATH" HOME="$HOME" "$python_bin" -m pytest -m "not integration" -q

echo
echo "Matches what CI will do."
