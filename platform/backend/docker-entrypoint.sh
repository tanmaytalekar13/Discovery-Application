#!/bin/sh
set -eu

echo "Running ArcadeDB bootstrap..."
python -m app.db.bootstrap

echo "Starting API server..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
