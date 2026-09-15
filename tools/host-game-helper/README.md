# Host agent (Windows / Ubuntu)

Roda no(s) PC(s) remoto(s) e **avisa sozinho** a telinha no Mini-PC o jogo em
foco, a mídia tocando, se a sessão está bloqueada, e as notificações do
sistema — sem precisar cadastrar nada no lado do Mini-PC nem informar IPs.

Suporta **múltiplos hosts ao mesmo tempo** (ex.: um PC Windows de jogo + um
Ubuntu de trabalho): cada instalação tem seu próprio `host_id` persistente, e
o Mini-PC rastreia cada host separadamente — o mais recentemente atualizado
"ganha" quando mais de um qualifica pro mesmo modo (ex.: dois jogando ao mesmo
tempo).

## Windows

1. Copie a pasta `tools/host-game-helper` para o PC Windows (ou use o repo).
2. Instale Python 3 e marque **Add to PATH**.
3. Dê dois cliques em `start-host-helper.bat`
   **ou** no PowerShell:

```powershell
cd ...\tools\host-game-helper
powershell -ExecutionPolicy Bypass -File .\install-host-helper.ps1
```

O instalador registra início automático no logon e tenta instalar os pacotes
WinRT opcionais (necessários só para mídia/notificações — a detecção de jogo
funciona sempre, mesmo se isso falhar).

Na primeira execução o Windows pode perguntar se o Python pode usar a rede
**Privada** — aceite. Para notificações, também aparece um pedido de
**acesso a notificações** (Windows → Configurações → Notificações) — aceite
uma vez; sem isso, só jogo/mídia são reportados.

## Ubuntu / Linux

1. Copie a pasta `tools/host-game-helper` para o PC Ubuntu.
2. Rode:

```bash
cd tools/host-game-helper
./install-linux-agent.sh
```

Isso instala `linux_reporter.py` como serviço `systemd --user`
(`host-agent.service`), iniciando com a sessão gráfica. Requer `busctl`,
`loginctl`, `dbus-monitor` (padrão em qualquer Ubuntu com systemd) — nenhuma
dependência Python extra.

Detecção de jogo no Linux é só por processo em execução (sem título de janela
em foco no Wayland) — a mesma estratégia de fallback que o agente Windows usa
quando o foco está num terminal.

## Como funciona

Cada agente anuncia a cada ~1s um pacote UDP na LAN (`broadcast` + multicast
`239.255.87.87:8787`) com jogo/mídia/bloqueio, e um pacote extra sempre que uma
notificação nova aparece. O `turing-clock` no Mini-PC escuta nessa porta e:

- **GAMER**: usa o jogo relatado por qualquer host quando reconhecido pela
  lista local de jogos (o Mini-PC é sempre a autoridade — texto recebido é só
  um candidato, nunca confiado cegamente).
- **MULTIMEDIA**: usa a mídia de um host remoto quando nada está tocando
  localmente no Mini-PC.
- **LOCKED**: usa o bloqueio de um host remoto quando a própria sessão local
  não está bloqueada.
- **Notificações**: aparecem no mesmo overlay/retenção das notificações
  locais do Mini-PC.

Não há configuração obrigatória no Mini-PC — a porta é sempre `8787`. Para
forçar um fallback HTTP específico (raramente necessário), defina em
`config.yaml`:

```yaml
# HOST_GAME_HELPER_URL: "http://192.168.1.50:8787"
```

Deixe comentado/`AUTO` para o modo automático (recomendado).

## Opcional (debug, só Windows)

```powershell
py -3 .\foreground_reporter.py --http
curl http://127.0.0.1:8787/foreground
```

`--http` só é útil para testar no próprio PC; o caminho normal é o broadcast
UDP.

## Privacidade

O conteúdo das notificações (podem incluir mensagens de apps como WhatsApp
Web/Discord) trafega **em texto puro** por UDP broadcast/multicast na rede
local — qualquer dispositivo na mesma LAN consegue capturar. Isso é aceitável
numa rede doméstica confiável; não use isso numa rede compartilhada/pública.
