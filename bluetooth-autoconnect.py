#!/usr/bin/env python3
"""Keep all paired BlueZ devices connected.

This is intentionally a small, dependency-free companion process.  A device
that is switched off or out of range does not prevent attempts for the other
paired devices.
"""

from __future__ import annotations

import argparse
import logging
import re
import shutil
import signal
import subprocess
import threading


LOG = logging.getLogger("bluetooth-autoconnect")
DEVICE_RE = re.compile(
    r"^Device\s+([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})(?:\s+(.*))?$"
)
STOP = threading.Event()


def bluetoothctl(*arguments: str, timeout: float = 10) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bluetoothctl", *arguments],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def paired_devices() -> list[tuple[str, str]]:
    result = bluetoothctl("devices", "Paired")
    if result.returncode != 0:
        LOG.warning("Não foi possível listar dispositivos pareados: %s", result.stderr.strip())
        return []

    devices = []
    for line in result.stdout.splitlines():
        match = DEVICE_RE.match(line.strip())
        if match:
            address, name = match.groups()
            devices.append((address.upper(), name or address.upper()))
    return devices


def is_connected(address: str) -> bool:
    result = bluetoothctl("info", address)
    return result.returncode == 0 and re.search(
        r"^\s*Connected:\s+yes\s*$", result.stdout, re.MULTILINE
    ) is not None


def connect(address: str, name: str) -> None:
    if is_connected(address):
        return

    LOG.info("Conectando %s (%s)...", name, address)
    try:
        result = bluetoothctl("connect", address, timeout=20)
    except subprocess.TimeoutExpired:
        LOG.warning("Tempo esgotado ao conectar %s (%s)", name, address)
        return

    if result.returncode == 0 and is_connected(address):
        LOG.info("%s conectado", name)
    else:
        detail = (result.stderr or result.stdout).strip().splitlines()
        LOG.warning(
            "Não foi possível conectar %s (%s): %s",
            name,
            address,
            detail[-1] if detail else "erro desconhecido",
        )


def reconnect_all() -> None:
    try:
        bluetoothctl("power", "on")
        devices = paired_devices()
        if not devices:
            LOG.debug("Nenhum dispositivo Bluetooth pareado")
        for address, name in devices:
            if STOP.is_set():
                break
            connect(address, name)
    except (OSError, subprocess.TimeoutExpired) as exc:
        LOG.warning("Bluetooth indisponível: %s", exc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconecta dispositivos Bluetooth pareados")
    parser.add_argument(
        "--interval",
        type=max_one,
        default=15,
        metavar="SECONDS",
        help="intervalo entre verificações (padrão: 15)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="faz apenas uma verificação e encerra",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if shutil.which("bluetoothctl") is None:
        LOG.error("bluetoothctl não está instalado")
        return 1

    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())

    reconnect_all()
    while not args.once and not STOP.wait(args.interval):
        reconnect_all()
    return 0


def max_one(value: str) -> float:
    interval = float(value)
    if interval < 1:
        raise argparse.ArgumentTypeError("o intervalo deve ser de pelo menos 1 segundo")
    return interval


if __name__ == "__main__":
    raise SystemExit(main())
