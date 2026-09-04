#!/usr/bin/env bash
set -o errexit

echo "=== Installing dependencies (locked) ==="
pip install --upgrade pip
# requirements.txt is the lockfile (regenerate with: uv pip compile pyproject.toml -o requirements.txt).
# Installing it first pins every transitive dependency; the editable install then
# adds only the app package itself.
pip install -r requirements.txt
pip install -e . --no-deps

echo "=== Running migrations ==="
alembic upgrade head

echo "=== Build complete ==="
