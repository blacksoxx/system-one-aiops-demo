#!/usr/bin/env sh
# Serve Laya's fine-tuned "typed-decisions" checkpoint with a Jev-compatible API.
#
#   ./scripts/serve-laya.sh            CPU or whatever LAYA_DEVICE says
#   LAYA_DEVICE=cuda ./scripts/serve-laya.sh
#
# First run downloads the checkpoint (about 1.6 GB) into $HF_HOME.
# The server listens on http://0.0.0.0:8000 ; the API is POST /v1/systemone.
set -eu
cd "$(dirname "$0")/.."

if [ ! -x .venv/bin/laya-serve ]; then
  echo "Creating .venv and installing laya[serve] (one-time)..."
  python3 -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet "laya[serve]"
fi

export HF_HOME="${HF_HOME:-$PWD/.hf}"
export USE_TF=0
export LAYA_MODELS="${LAYA_MODELS:-typed-decisions}"
export LAYA_DEFAULT_MODEL="${LAYA_DEFAULT_MODEL:-typed-decisions}"
export LAYA_PRELOAD="${LAYA_PRELOAD:-1}"
export LAYA_HOST="${LAYA_HOST:-0.0.0.0}"
export LAYA_PORT="${LAYA_PORT:-8000}"

echo "laya-serve · model $LAYA_DEFAULT_MODEL · device ${LAYA_DEVICE:-auto} · http://$LAYA_HOST:$LAYA_PORT/v1/systemone"
exec .venv/bin/laya-serve
