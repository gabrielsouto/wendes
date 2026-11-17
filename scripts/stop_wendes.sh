#!/bin/sh
# ExecStop do wendes@.service: pede só ao processo Python que encerre a reunião
# e gere a ata, sem derrubar antes o Xvfb e o Chromium.
#
# Sem isto, o "systemctl stop" manda SIGTERM a todo o cgroup ao mesmo tempo:
# o Chromium e o Xvfb morrem junto, e a reunião do modo run termina como
# "interrompida". Com o ExecStop, o systemd só manda o SIGTERM geral depois
# que este script retorna, isto é, depois que o Python terminou a ata.
#
# Uso: stop_wendes.sh <MAINPID>   (o MAINPID é o xvfb-run de run_wendes.sh)
set -u
main="${1:-}"
[ -n "$main" ] || exit 0          # serviço já saiu sozinho: nada a fazer
pid=$(pgrep -P "$main" -f 'bin/wendes' | head -n 1)
[ -n "$pid" ] || exit 0
kill -TERM "$pid" 2>/dev/null || exit 0
# Espera o Python terminar (retranscrição + ata); o limite é o TimeoutStopSec.
while kill -0 "$pid" 2>/dev/null; do
    sleep 1
done
