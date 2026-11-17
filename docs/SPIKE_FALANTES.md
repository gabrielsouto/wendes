# Spike de falantes (Fase 0 do modo assistente)

> **Status: PENDENTE DE MEDIÇÃO.** Os scripts estão prontos, mas os números
> dependem de uma reunião real de teste, com 3 ou mais pessoas, rotulada à
> mão. Até lá, `assistant.speakers.enabled` fica `false` (default) e todo
> segmento sai com o falante "não identificado". Nenhuma funcionalidade do
> modo depende da atribuição.

## Objetivo

Decidir com números se a atribuição de falas por nível de áudio do Jitsi entra
no modo assistente, e com qual `latency_offset_s`.

## Roteiro

1. **Coleta (10 min).** Sob Xvfb e com o áudio virtual configurado:

   ```bash
   scripts/setup_audio.sh
   xvfb-run -a .venv/bin/python scripts/spike_speakers.py --minutes 10 [--url URL]
   ```

   O bot entra mudo, avisa no chat e grava em `data/spike/AAAA-MM-DD_HH-MM/`:
   `recording.wav`, `audio_levels.jsonl` (níveis e dominant speaker com
   `t_audio` = amostras gravadas / 16000, **sem offset**), `diagnostics.json`
   e `participants.json`. No terminal, a cada 10 s, aparecem os eventos por
   segundo de cada participante: é o primeiro sinal de que os níveis chegam
   para todos.

   Na reunião, peça que as pessoas falem em turnos, com algumas interrupções e
   falas curtas ("sim", "isso"), como numa daily de verdade.

2. **Rótulos.** Abra `recording.wav` no Audacity, crie uma faixa de rótulos e
   marque cada fala com o nome da pessoa (Ctrl+B). Exporte em
   *Arquivo → Exportar → Exportar rótulos* como `labels.txt` na mesma pasta
   (formato: início, fim e nome, separados por tab). Use o nome que aparece no
   Jitsi ou o do roster; a comparação ignora acentos e aceita nome parcial.

3. **Avaliação.**

   ```bash
   .venv/bin/python scripts/eval_speakers.py data/spike/AAAA-MM-DD_HH-MM --model medium
   ```

   Transcreve com `word_timestamps=True` (salva `words.json` para não repetir)
   e grava `eval_speakers.md` e `eval_speakers.json` na pasta.

4. **Preencher as seções abaixo** e aplicar a regra de decisão.

## Verificações

| Item | Como verificar | Resultado |
|---|---|---|
| Nomes dos eventos de `JitsiMeetJS.events.conference` | `diagnostics.json` → `eventNames` | _pendente_ |
| `TRACK_AUDIO_LEVEL_CHANGED` existe (senão o fallback `conference.audioLevelsChanged`) | `diagnostics.json` → `listeners` | _pendente_ |
| Os níveis chegam para **todos** os participantes no meet.jit.si (reescrita de SSRC e encaminhamento só dos mais altos podem cortar alguém) | Taxa por participante no terminal; `participants.json` × pids em `audio_levels.jsonl` | _pendente_ |
| `sendPrivateTextMessage` existe | `diagnostics.json` → `sendPrivateTextMessage` | _pendente_ |

## Resultados

Reunião de teste: _data, duração, número de pessoas_ — _pendente_

| Abordagem | Precisão (tempo) | Cobertura (tempo) | Precisão (seg.) | Cobertura (seg.) |
|---|---|---|---|---|
| (a) dominant speaker no ponto médio (heurística antiga) | _pendente_ | | | |
| (b) integração dos níveis, melhor offset | _pendente_ | | | |
| (c) (b) + quebra por palavra quando o dominante muda ≥ 1 s | _pendente_ | | | |

Offset escolhido (`latency_offset_s`): _pendente_ (varredura de −1,0 a +1,0 s em
passos de 0,1).

## Regra de decisão

Se (b) ou (c) atingir **≥ 85% de precisão por tempo de fala**,
`assistant.speakers.enabled: true` vira o default em `config.py` e no
`config.example.yaml`, com o offset medido. Senão, fica `false` e o resto do
modo não muda. Uma evolução possível, fora do escopo, é diarização por
embeddings de voz (pyannote ou similar).

## Recomendação

_Pendente da medição._ Enquanto isso, `speakers.enabled: false`.

## Como os números entram no código

- A matemática medida aqui é a mesma da produção: `wendes.assistant.speakers`
  (`attribute_levels` para (b) e `split_by_speaker` para (c)). O
  `eval_speakers.py` só importa essas funções.
- Na sessão ao vivo, o `SpeakerTracker` grava `t_audio` já com o offset
  descontado; o spike grava o tempo bruto para que o eval possa varrer offsets.
