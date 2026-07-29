#!/usr/bin/env bash
# Monta o volume Dados cedo no boot em /mnt/Dados (evita atraso do udisks em /run/media).
# Requer sudo. Depois rode: ./install-clock-service.sh
set -euo pipefail

UUID=dae44910-d99f-4e9c-a2d9-025f6b8b2055
MOUNT_POINT=/mnt/Dados
FSTAB_LINE="UUID=$UUID $MOUNT_POINT ext4 defaults,nofail 0 2"

if [[ "${EUID}" -ne 0 ]]; then
  exec sudo -- "$0" "$@"
fi

mkdir -p "$MOUNT_POINT"

if ! grep -q "$UUID" /etc/fstab; then
  printf '%s\n' "$FSTAB_LINE" >>/etc/fstab
  printf 'Entrada adicionada ao /etc/fstab:\n  %s\n' "$FSTAB_LINE"
else
  printf 'Entrada já existe no /etc/fstab:\n'
  grep "$UUID" /etc/fstab
  # Normalize legacy /mnt/dados entries to the canonical mount.
  if grep -q "$UUID.*/mnt/dados[[:space:]]" /etc/fstab; then
    sed -i "s|$UUID[[:space:]]\+/mnt/dados[[:space:]]|$UUID $MOUNT_POINT |" /etc/fstab
    printf 'fstab atualizado para montar em %s\n' "$MOUNT_POINT"
  fi
fi

if findmnt -n "$MOUNT_POINT" >/dev/null 2>&1; then
  printf '%s já montado:\n' "$MOUNT_POINT"
  findmnt "$MOUNT_POINT"
elif findmnt -n -S "UUID=$UUID" >/dev/null 2>&1; then
  src="$(findmnt -n -o TARGET -S "UUID=$UUID" | head -1)"
  printf 'Fazendo bind %s -> %s (esta sessão)\n' "$src" "$MOUNT_POINT"
  mount --bind "$src" "$MOUNT_POINT"
else
  mount "$MOUNT_POINT"
fi

# Compatibility symlink for older docs/scripts that still mention /mnt/dados.
if [[ ! -e /mnt/dados ]]; then
  ln -s "$MOUNT_POINT" /mnt/dados
  printf 'Symlink de compatibilidade: /mnt/dados -> %s\n' "$MOUNT_POINT"
elif [[ -L /mnt/dados ]]; then
  ln -sfn "$MOUNT_POINT" /mnt/dados
fi

printf '\nOK. Próximo boot monta Dados cedo em %s.\n' "$MOUNT_POINT"
printf 'Reinstale os serviços:\n'
printf '  cd %s/dsv/turing-smart-screen-python\n' "$MOUNT_POINT"
printf '  ./install-clock-service.sh\n'
printf '  ./install-bluetooth-autoconnect-service.sh\n'
printf '  ./install-iphone-notifications-service.sh\n'
