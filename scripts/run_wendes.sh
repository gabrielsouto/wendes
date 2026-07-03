#!/bin/sh
# Executa o wendes sob Xvfb com o áudio virtual garantido.
set -eu
cd "$(dirname "$0")/.."
scripts/setup_audio.sh >/dev/null
exec xvfb-run -a .venv/bin/wendes "${@:-run}"
