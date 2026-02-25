#!/usr/bin/env bash
set -o errexit
pip install --upgrade pip
pip install -e .
alembic upgrade head
