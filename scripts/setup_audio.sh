#!/bin/sh
# Cria os dispositivos virtuais de áudio do wendes (idempotente).
#   bot_mic_sink : null sink onde o TTS é tocado (paplay)
#   bot_mic      : source remapeada do monitor do bot_mic_sink — "microfone" do Chromium
#   bot_out      : null sink onde o Chromium toca o áudio da sala; captura-se de bot_out.monitor
set -eu

have_module() {
    pactl list short modules | awk -v n="$1" -v a="$2" '$2 == n && index($0, a) > 0' | grep -q .
}

have_module module-null-sink "sink_name=bot_mic_sink" || \
    pactl load-module module-null-sink sink_name=bot_mic_sink sink_properties=device.description=BotMicSink >/dev/null

have_module module-remap-source "source_name=bot_mic" || \
    pactl load-module module-remap-source master=bot_mic_sink.monitor source_name=bot_mic source_properties=device.description=BotMic >/dev/null

have_module module-null-sink "sink_name=bot_out" || \
    pactl load-module module-null-sink sink_name=bot_out sink_properties=device.description=BotOut >/dev/null

echo "Dispositivos de áudio do wendes:"
pactl list short sinks | grep -E 'bot_(mic_sink|out)' || true
pactl list short sources | grep -E 'bot_mic|bot_out' || true
