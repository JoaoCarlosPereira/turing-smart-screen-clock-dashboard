# Host game helper (Windows / Sunshine PC)

Roda no PC do jogo e **avisa sozinho** a telinha no Mini-PC qual janela está em
foco (ex.: Palworld durante stream Moonlight de Desktop).

Não precisa cadastrar jogos no Sunshine, nem colocar o IP do Mini-PC.

## Uso rápido

1. Copie a pasta `tools/host-game-helper` para o PC Windows (ou use o repo).
2. Instale Python 3 e marque **Add to PATH**.
3. Dê dois cliques em `start-host-helper.bat`  
   **ou** no PowerShell:

```powershell
cd ...\tools\host-game-helper
powershell -ExecutionPolicy Bypass -File .\install-host-helper.ps1
```

O instalador registra início automático no logon.

Na primeira execução o Windows pode perguntar se o Python pode usar a rede
**Privada** — aceite.

## Como funciona

O helper anuncia a cada ~1s um pacote UDP na LAN (`broadcast` + multicast
`239.255.87.87:8787`). O `turing-clock` no Mini-PC escuta e, quando há sessão
Moonlight, usa o título/exe para o modo GAMER.

O Mini-PC já vem com `HOST_GAME_HELPER_URL: AUTO` — não precisa mudar nada lá.

## Opcional (debug)

```powershell
py -3 .\foreground_reporter.py --http
curl http://127.0.0.1:8787/foreground
```

`--http` só é útil para testar no próprio PC; o caminho normal é só o broadcast UDP.
