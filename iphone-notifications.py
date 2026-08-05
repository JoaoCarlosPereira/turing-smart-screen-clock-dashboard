#!/usr/bin/env python3
"""Expose iPhone MAP message events as freedesktop desktop notifications."""

from __future__ import annotations

import argparse
import logging
import signal
import subprocess
from typing import Any

import dbus
import dbus.mainloop.glib
from gi.repository import GLib


LOG = logging.getLogger("iphone-notifications")
OBEX_SERVICE = "org.bluez.obex"
OBEX_ROOT = "/org/bluez/obex"
MAP_INTERFACE = "org.bluez.obex.MessageAccess1"
MESSAGE_INTERFACE = "org.bluez.obex.Message1"
OBJECT_MANAGER = "org.freedesktop.DBus.ObjectManager"


class IPhoneMessageBridge:
    def __init__(self, address: str, interval: int) -> None:
        self.address = address
        self.interval = interval
        self.bus = dbus.SessionBus()
        self.client: dbus.Interface | None = None
        self.session_path: dbus.ObjectPath | None = None
        self.message_access: dbus.Interface | None = None
        self.known_messages: set[str] = set()
        self.baselined = False

        self.bus.add_signal_receiver(
            self._interfaces_added,
            dbus_interface=OBJECT_MANAGER,
            signal_name="InterfacesAdded",
            bus_name=OBEX_SERVICE,
        )

    def close(self) -> None:
        if self.client is not None and self.session_path is not None:
            try:
                self.client.RemoveSession(self.session_path)
            except dbus.DBusException:
                pass
        self.session_path = None
        self.message_access = None

    def connect(self) -> bool:
        self.close()
        try:
            root = self.bus.get_object(OBEX_SERVICE, OBEX_ROOT)
            self.client = dbus.Interface(root, "org.bluez.obex.Client1")
            self.session_path = self.client.CreateSession(
                self.address, {"Target": "map"}, timeout=20
            )
            session = self.bus.get_object(OBEX_SERVICE, self.session_path)
            self.message_access = dbus.Interface(session, MAP_INTERFACE)
            self.message_access.SetFolder("telecom")
            self.message_access.SetFolder("msg")
            LOG.info("Sessão de notificações conectada ao iPhone")
            return True
        except dbus.DBusException as exc:
            LOG.warning("Não foi possível abrir a sessão de mensagens: %s", exc)
            self.close()
            return False

    @staticmethod
    def _plain(properties: Any) -> dict[str, Any]:
        return {str(key): value for key, value in properties.items()}

    def _notify(self, properties: dict[str, Any]) -> None:
        props = self._plain(properties)
        if str(props.get("Direction", "incoming")) != "incoming":
            return

        sender = str(props.get("Sender") or props.get("SenderAddress") or "iPhone")
        subject = str(props.get("Subject") or "Nova mensagem")
        subprocess.run(
            [
                "notify-send",
                "--app-name=iPhone",
                "--icon=phone",
                f"Mensagem de {sender}",
                subject,
            ],
            check=False,
            timeout=5,
        )
        LOG.info("Notificação exibida: mensagem de %s", sender)

    def _interfaces_added(self, path: dbus.ObjectPath, interfaces: Any) -> None:
        path_text = str(path)
        if self.session_path is None or not path_text.startswith(str(self.session_path)):
            return
        if MESSAGE_INTERFACE not in interfaces or path_text in self.known_messages:
            return

        self.known_messages.add(path_text)
        if self.baselined:
            self._notify(interfaces[MESSAGE_INTERFACE])

    def poll(self) -> bool:
        if self.message_access is None and not self.connect():
            return True

        try:
            messages = self.message_access.ListMessages(
                "inbox",
                {
                    "MaxCount": dbus.UInt16(50),
                    "SubjectLength": dbus.Byte(255),
                    "Fields": dbus.Array(
                        ["subject", "timestamp", "sender", "sender-address", "read"],
                        signature="s",
                    ),
                },
                timeout=20,
            )
            current: set[str] = set()
            for path, properties in messages.items():
                path_text = str(path)
                current.add(path_text)
                if self.baselined and path_text not in self.known_messages:
                    self._notify(properties)
            self.known_messages.update(current)
            self.baselined = True
        except dbus.DBusException as exc:
            LOG.warning("Sessão MAP perdida; será reconectada: %s", exc)
            self.close()
        return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Mostra mensagens do iPhone nas notificações do desktop"
    )
    parser.add_argument("address", help="endereço Bluetooth do iPhone")
    parser.add_argument("--interval", type=int, default=10)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

    bridge = IPhoneMessageBridge(args.address, max(5, args.interval))
    loop = GLib.MainLoop()

    def stop(*_: object) -> None:
        bridge.close()
        loop.quit()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    bridge.poll()
    GLib.timeout_add_seconds(bridge.interval, bridge.poll)
    try:
        loop.run()
    finally:
        bridge.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
