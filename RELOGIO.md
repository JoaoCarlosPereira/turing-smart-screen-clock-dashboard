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
cd /mnt/dados/dsv/turing-smart-screen-python
python3 -m venv .venv
.venv/bin/pip install -r requirements-clock.txt
```

## Modos de exibição

O `clock-display.py` suporta três modos de exibição com detecção automática:

### 1. MAIN (padrão) — Painel de relógio e notificações

Exibe a hora local, data e notificações do desktop. É o modo original do
programa.

```
┌─────────────────────────────────────────────────────────────────────┐
│ ██████████████████████████████████████████████████████████████████  │
│                                                                     │
│   15:20:33                  22 Jul 2026                            │
│                       Wednesday                                  │
│                                                                     │
│  ┌───────────────────────────────────────────────────────────────┐ │
│  │                                                               │ │
│  │    ♪    Nenhuma notificação                    15:20         │ │
│  │                                                               │ │
│  └───────────────────────────────────────────────────────────────┘ │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

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

### Forçar um modo

Para usar um modo específico sem detecção automática:

```bash
# Execução manual
sg dialout -c '.venv/bin/python clock-display.py --mode main'
sg dialout -c '.venv/bin/python clock-display.py --mode multimedia'
sg dialout -c '.venv/bin/python clock-display.py --mode gamer'
```

Na execução sem `--mode`, o programa verifica a cada 2 segundos:
1. **Jogo em execução?** → GAMER
2. **Mídia reproduzindo?** → MULTIMEDIA
3. **Nada?** → MAIN (relógio + notificações)

A troca é automática e não requer reinício do serviço.

## Executar manualmente

Pare o serviço antes para evitar que dois processos disputem a porta serial:

```bash
systemctl --user stop turing-clock.service
cd /mnt/dados/dsv/turing-smart-screen-python
sg dialout -c '.venv/bin/python clock-display.py'
```

Finalize o modo manual com `Ctrl+C`.

## Instalar e iniciar automaticamente

```bash
cd /mnt/dados/dsv/turing-smart-screen-python
./install-clock-service.sh
```

O instalador cria um link em `~/.config/systemd/user/` para a unidade mantida
em `systemd/turing-clock.service`, habilita o serviço e o inicia imediatamente.

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
desktop e uma cópia ocupa a tela IPS por seis segundos. Em seguida, um resumo
permanece na área abaixo do relógio até que a próxima notificação o substitua.
Nomes de aplicativos não são exibidos e domínios no início da mensagem, como
`web.whatsapp.com`, são removidos da cópia apresentada na tela IPS. O horário
local de chegada acompanha a notificação em destaque e o resumo persistente.

Título e corpo podem conter conteúdo privado de mensageiros, e-mails e outros
aplicativos. A tela é apenas visual: ações e botões da notificação continuam
disponíveis somente no desktop.

## Personalização

Edite `clock-display.py` para alterar cores, fontes, brilho, orientação e
conteúdo. O brilho padrão é 100% (`BRIGHTNESS`); telas revisão A podem aquecer
em níveis altos. Depois de editar, aplique a mudança com:

```bash
systemctl --user restart turing-clock.service
```

Não execute `simple-program.py`, `main.py` e `clock-display.py` simultaneamente:
somente um processo deve controlar a porta serial de cada vez.
