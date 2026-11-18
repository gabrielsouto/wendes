# F10 — Página na tela da reunião (`/tela`) — EXPERIMENTAL

**Status: implementado atrás de `assistant.screen_share: false`; PENDENTE DE VALIDAÇÃO no meet.jit.si.**
Os testes automáticos usam fakes do Playwright e só cobrem o fluxo de comandos; nenhum deles prova que o Jitsi aceita a captura.

## Como funciona

1. Com `assistant.screen_share: true`, o Chromium sobe com:
   - `--auto-select-desktop-capture-source="Wendes - pagina da reuniao"`
   - `--auto-select-tab-capture-source-by-title="Wendes - pagina da reuniao"` (Chromium mais novo)
   - `--enable-usermedia-screen-capturing` e `--allow-http-screen-capture`
2. `/tela` (admin) abre uma segunda aba no mesmo navegador com a página ao vivo
   (`http://127.0.0.1:<porta>/m/<run>?token=…`, 1920×1080). Um init script trava o
   título em `Wendes - pagina da reuniao`, para a seleção automática achar a fonte.
3. Na aba do Jitsi: `APP.conference.toggleScreenSharing(true)`. O bot espera até 8 s
   por `APP.conference.isSharingScreen`; sem confirmação, fecha a aba e responde
   sugerindo `/link`.
4. `/tela off` desliga e fecha a aba. O encerramento da sessão também desliga.

Código: [src/wendes/jitsi/screen.py](../src/wendes/jitsi/screen.py); comando em
`AssistantSession.cmd_tela`.

## Teste manual

1. Xvfb com tela grande: `xvfb-run -a -s "-screen 0 1920x1080x24" .venv/bin/wendes assist`
   (o `scripts/run_wendes.sh` usa o padrão do xvfb-run; ajuste se a captura vier cortada).
2. `assistant.screen_share: true`, autentique-se (`/auth` no privado) e mande `/tela`.
3. Confira, de outro participante: o tile do bot mostra a página; notas e artefatos atualizam;
   `/tela off` volta ao tile normal; `/fim` encerra sem travar.
4. Registre aqui: versão do Chromium, servidor Jitsi, se o seletor abriu (falha das flags),
   e se o Jitsi pediu extensão/permite captura em HTTP.

Se não funcionar de forma confiável, o recurso fica desligado e documentado como abandonado
(a spec o trata como opcional). `/link` continua sendo o caminho suportado.
