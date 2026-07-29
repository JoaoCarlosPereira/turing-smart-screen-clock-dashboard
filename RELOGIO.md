# Relógio na tela Turing IPS 3,5"

Este complemento exibe um painel tecnológico com hora local, data e estado da
integração na tela Turing `USB35INCHIPSV2`. Ele também observa o barramento
D-Bus da sessão e espelha por alguns segundos as notificações exibidas pelo
Pop!_OS/COSMIC. O processo roda como serviço do usuário, inicia com a sessão
gráfica e tenta novamente a cada três segundos caso a tela seja desconectada ou
a porta serial ainda não esteja disponível.

## Hardware identificado

- Fabricante/produto: `Turing UsbMonitor`
- USB ID: `1a86:5722`
- Serial: `USB35INCHIPSV2`
- Porta habitual no Linux: `/dev/ttyACM0`
- Revisão usada pelo programa: `A`, Turing 3,5"
- Resolução: 320 × 480 em retrato ou 480 × 320 em paisagem

A tela usa USB serial para receber imagens; ela não aparece como um segundo
monitor no ambiente gráfico.

## Conexão e diagnóstico

Conecte a tela por um cabo USB que transmita dados. Confirme a detecção:

```bash
lsusb | grep '1a86:5722'
ls -l /dev/ttyACM*
journalctl -k --since '2 minutes ago' --no-pager
```

O usuário precisa pertencer ao grupo `dialout`:

```bash
sudo usermod -aG dialout "$USER"
```

Saia e entre novamente na sessão depois desse comando. Para aplicar o grupo
somente a um comando na sessão atual, use `sg dialout -c 'comando'`.

Erros USB `error -71` normalmente indicam cabo, porta, hub ou alimentação.
Experimente outro cabo de dados e conecte diretamente a outra porta USB.

## Preparação do Python

No Ubuntu/Pop!_OS:

```bash
sudo apt install python3-venv
cd /mnt/Dados/dsv/turing-smart-screen-python
python3 -m venv .venv
.venv/bin/pip install -r requirements-clock.txt
```

## Modos de exibição

O `clock-display.py` suporta quatro modos de exibição com detecção automática:

### 1. MAIN (padrão) — Painel de relógio, clima e notificações

Exibe a hora local, data, previsão do tempo (Open-Meteo, local fixo SMO/SC
nos coords — **sem rótulo de local nem fonte na UI**) e notificações do desktop.
Sem notificação ativa, a área inferior mostra temperatura grande + condição,
meta compacta (máx·mín / **chuva restante do dia**·umidade·vento) e uma **timeline horária de chuva**
(12 barras de 2h: 00·02·…·22, marcador do bloco atual).
O % de **Chuva** é o máximo diário escalado pela massa de probabilidade das horas
ainda à frente (hora atual inclusive) — ex.: dia 100% e 70% do “peso” já passou → ~30%.
Com notificação, o alerta ocupa essa área (retenção de **15 minutos** no painel).
O painel da hora é compacto (data sem ano + dia da semana elevado) para dar mais
espaço ao clima/notificações.

```
┌─────────────────────────────────────────────────────────────────────┐
│                                                                     │
│   15:20:33                      22 JUL                             │
│                                 2026                               │
│                                 WEDNESDAY                          │
│  ─────────────────────────────────────────────────────────────────  │
│  ┌───────────────────────────────────────────────────────────────┐ │
│  │  ☀  24°                                   Céu limpo         │ │
│  │      máx 25° · mín 16°          Chuva 40% · Um 69% · 3 km/h  │ │
│  │  Chuva                                                        │ │
│  │  ▁▂▃▅▇█▅▃▂▁▁▁▂▃▄▅▆▅▃▂▁▁▁▁   (0h  6h  12h  18h)              │ │
│  └───────────────────────────────────────────────────────────────┘ │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

Fonte do clima (só no backend): [Open-Meteo](https://open-meteo.com/) (sem API
key), coords SMO/SC, hourly `precipitation_probability`, cache ~12 minutos.
UI do clima **não** exibe local nem atribuição.

### 2. MULTIMEDIA — Info de mídia (estilo Spotify)

Quando detectar que algum aplicativo está reproduzindo mídia via MPRIS2
(Spotify, YouTube, VLC, etc.), a tela muda automaticamente para exibir:

- Capa do álbum
- Nome da faixa
- Nome do artista
- Nome do álbum
- Barra de progresso com tempo decorrido

```
┌─────────────────────────────────────────────────────────────────────┐
│ ██████████████████████████████████████████████████████████████████  │
│                                                                     │
│  [Capa do Álbum]  SPOTIFY                                          │
│                     Me Amar Amanhã                                 │
│                     Matheus & Kauan                                │
│                     Matheus & Kauan (2024)                         │
│                                                                     │
│                    ━━━━━━━━━━░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░  │
│                     0:39                            2:46          │
│                          ♪ │▌ ▌                                  │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

Suporta Spotify (Desktop/AppImage/Flatpak), YouTube Music, VLC, Rhythmbox,
e qualquer aplicativo que exponha a interface MPRIS2 via D-Bus.

### 3. GAMER — Overlay de jogos

Quando detectar que um jogo está em execução, a tela muda para exibir:

- Nome do jogo
- FPS em tempo real
- Uso de CPU/GPU
- Temperatura da GPU
- Uso de memória

```
┌─────────────────────────────────────────────────────────────────────┐
│ ██████████████████████████████████████████████████████████████████  │
│                                                                     │
│  [Arte]  COUNTER-STRIKE 2                                          │
│          PID: 12345  Process: cs2.exe                              │
│                                                                     │
│  ┌──────┐┌──────┐┌──────┐┌──────┐┌──────┐                         │
│  │ FPS  ││ CPU  ││ GPU  ││ TEMP ││  MEM │                         │
│  │142   ││ 45%  ││ 88%  ││ 62°C ││2.4GB │                         │
│  └──────┘└──────┘└──────┘└──────┘└──────┘                         │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

Jogos conhecidos (processo no Linux/Windows): CS2, Valorant, Rocket League,
Apex Legends, Fortnite, Minecraft, GTA V, Elden Ring, Dota 2, entre outros.
Também detecta **Steam Remote Play** (`streaming_client`) e sessões ativas do
**Moonlight** (Snap/Flatpak/nativo) — jogos, **Desktop remoto** e Steam Big
Picture — enquanto houver tráfego GameStream com o host Sunshine. O launcher
aberto sem stream não troca para o modo GAMER.

#### Jogo real no Desktop remoto (helper no Windows)

Quando o Moonlight transmite o **Desktop**, rode **uma vez** no PC Windows:

1. Copie `tools/host-game-helper`
2. Dê dois cliques em `start-host-helper.bat`  
   ou `install-host-helper.ps1` (inicia no logon)

O helper **anuncia sozinho na LAN** (UDP). O Mini-PC escuta — sem IP, sem
cadastrar jogos no Sunshine. Aceite a rede Privada se o Windows perguntar.
Detalhes: `tools/host-game-helper/README.md`.

### 4. LOCKED — Sessão bloqueada

Quando o sistema operacional estiver bloqueado (tela de login / lock screen),
a telinha entra no modo LOCKED automaticamente:

- Brilho cai para **10%**
- Exibe apenas a **hora** (centrada) + ícone de cadeado
- Fundo e cores seguem o **tema do desktop** (wallpaper + accent COSMIC/Pop!_OS)
- Notificações **nunca** são exibidas no painel enquanto estiver bloqueado
  (a fila é drenada em silêncio; overlays ativos são cancelados ao entrar em LOCKED)

```
┌─────────────────────────────────────────────────────────────────────┐
│                                                                     │
│                           🔒                                        │
│                                                                     │
│                        15:20:33                                     │
│                       BLOQUEADO                                     │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

Detecção via `logind` (`LockedHint`) com fallback para
`org.gnome.ScreenSaver`. Prioridade máxima: bloqueia mesmo com jogo ou mídia
ativos. Ao desbloquear, volta ao modo apropriado (MAIN / MULTIMEDIA / GAMER).

### Forçar um modo

Para usar um modo específico sem detecção automática:

```bash
# Execução manual
sg dialout -c '.venv/bin/python clock-display.py --mode main'
sg dialout -c '.venv/bin/python clock-display.py --mode multimedia'
sg dialout -c '.venv/bin/python clock-display.py --mode gamer'
sg dialout -c '.venv/bin/python clock-display.py --mode locked'
```

Na execução sem `--mode`, o programa verifica periodicamente:
1. **Sessão bloqueada?** → LOCKED (poll ~1s, brilho 10%)
2. **Jogo em execução?** → GAMER
3. **Mídia reproduzindo?** → MULTIMEDIA
4. **Nada?** → MAIN (relógio + notificações)

A troca é automática e não requer reinício do serviço.

## Executar manualmente

Pare o serviço antes para evitar que dois processos disputem a porta serial:

```bash
systemctl --user stop turing-clock.service
cd /mnt/Dados/dsv/turing-smart-screen-python
sg dialout -c '.venv/bin/python clock-display.py'
```

Finalize o modo manual com `Ctrl+C`.

## Disco Dados no boot

O projeto vive no volume `Dados`. O caminho canônico é `/mnt/Dados` (via
`/etc/fstab`). **Não use** `/run/media/...`: o udisks muda esse caminho e os
serviços ficam apontando para um diretório inexistente.

Monte o disco cedo, em caminho fixo (pede sudo):

```bash
cd /mnt/Dados/dsv/turing-smart-screen-python
./setup-dados-mount.sh
```

Isso garante `/mnt/Dados` no `/etc/fstab` (`nofail` se o disco estiver ausente),
cria o symlink de compatibilidade `/mnt/dados` → `/mnt/Dados` e monta agora.
Depois reinstale os serviços a partir de `/mnt/Dados/...`.

## Instalar e iniciar automaticamente

```bash
cd /mnt/Dados/dsv/turing-smart-screen-python
./install-clock-service.sh
```

O instalador copia launchers estáveis para
`~/.local/share/turing-smart-screen/` (filesystem home), gera a unidade em
`~/.config/systemd/user/` e a inicia. Os launchers resolvem o projeto em
`/mnt/Dados` em runtime — a unidade **nunca** aponta para `/run/media`.
A instalação falha se `/mnt/Dados/...` não estiver disponível.

Comandos úteis:

```bash
systemctl --user status turing-clock.service
systemctl --user restart turing-clock.service
systemctl --user stop turing-clock.service
journalctl --user -u turing-clock.service -f
```

Para desativar o início automático:

```bash
systemctl --user disable --now turing-clock.service
```

## Recuperação automática

O `clock-display.py` trata sozinho porta ausente e serial travada (RTS/CTS):

- abre a porta com `write_timeout` para não ficar bloqueado para sempre
- se a escrita serial falhar ou estourar o timeout, aplica unstick DTR/RTS,
  redetecta a porta (`AUTO`), envia reset e reinsere o painel
- enquanto a tela estiver desconectada, tenta de novo a cada três segundos

A unidade systemd usa `Restart=always` e `RestartSec=3` como rede de
segurança. `TimeoutStopSec=5` e `KillMode=mixed` evitam parada longa se o
processo ainda estiver preso na serial.

## Notificações do Pop!_OS

O programa executa `dbus-monitor` somente na sessão do usuário e observa as
chamadas padrão `org.freedesktop.Notifications.Notify`. Ele não substitui nem
bloqueia o `cosmic-notifications`: o aviso continua aparecendo normalmente no
desktop e uma cópia ocupa a tela IPS por alguns segundos. Em seguida, um resumo
permanece na área abaixo do relógio (retenção **15 min**) até expirar ou ser
substituído. Domínios no início da mensagem, como `web.whatsapp.com`, são
removidos da cópia apresentada na tela IPS. O horário local de chegada
acompanha a notificação em destaque e o resumo persistente.

**Privacidade WhatsApp:** notificações do app WhatsApp **e do WhatsApp Web no
Chrome/Chromium** (detectadas por `whatsapp` / `web.whatsapp.com` no app,
título, corpo ou ícone) exibem só o remetente/grupo e o texto genérico
**Nova notificação** — o corpo da mensagem nunca aparece no overlay nem no painel
MAIN. O desktop continua mostrando o conteúdo completo normalmente.

Título e corpo de outros aplicativos podem conter conteúdo privado. A tela é
apenas visual: ações e botões da notificação continuam disponíveis somente no
desktop.

## Personalização

Edite `clock-display.py` para alterar cores, fontes, brilho, orientação e
conteúdo. O brilho padrão é 100% (`BRIGHTNESS`); telas revisão A podem aquecer
em níveis altos. Depois de editar, aplique a mudança com:

```bash
systemctl --user restart turing-clock.service
```

Não execute `simple-program.py`, `main.py` e `clock-display.py` simultaneamente:
somente um processo deve controlar a porta serial de cada vez.
