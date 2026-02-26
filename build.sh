#!/usr/bin/env bash
set -o errexit

echo "=== Installing dependencies ==="
pip install --upgrade pip
pip install -e .

echo "=== Running migrations ==="
alembic upgrade head

echo "=== Build complete ==="
