#!/usr/bin/env bash
set -euo pipefail

# Pin the VyOS rolling-nightly build. To bump: change the tag, re-run.
VYOS_RELEASE_TAG="${VYOS_RELEASE_TAG:-2026.04.13-0034-rolling}"
VYOS_PUBKEY="${VYOS_PUBKEY:-RWSIhkR/dkM2DSaBRniv/bbbAf8hmDqdbOEmgXkf1RxRoxzodgKcDyGq}"

IMAGES_ROOT="${NOVA_VE_IMAGES_ROOT:-/var/lib/nova-ve/images}"
TEMPLATE_DIR="${IMAGES_ROOT}/qemu/vyos-${VYOS_RELEASE_TAG}"
ISO_NAME="vyos-${VYOS_RELEASE_TAG}-generic-amd64.iso"
ISO_URL="https://github.com/vyos/vyos-nightly-build/releases/download/${VYOS_RELEASE_TAG}/${ISO_NAME}"
SIG_URL="${ISO_URL}.minisig"
EXPECTED_SIZE=658505728

OWNER_USER="${NOVA_VE_OWNER_USER:-ubuntu}"
OWNER_GROUP="${NOVA_VE_OWNER_GROUP:-ubuntu}"

install -d -m 0755 "${TEMPLATE_DIR}"

ISO_PATH="${TEMPLATE_DIR}/${ISO_NAME}"
SIG_PATH="${ISO_PATH}.minisig"
CDROM_PATH="${TEMPLATE_DIR}/cdrom.iso"

if [[ -f "${ISO_PATH}" && "$(stat -c%s "${ISO_PATH}")" -eq "${EXPECTED_SIZE}" ]]; then
  echo "[install-vyos] ${ISO_NAME} already present at expected size, skipping download."
else
  echo "[install-vyos] Downloading ${ISO_NAME} (~628 MiB)..."
  curl -fL --progress-bar -o "${ISO_PATH}.partial" "${ISO_URL}"
  mv "${ISO_PATH}.partial" "${ISO_PATH}"
fi

echo "[install-vyos] Downloading detached signature..."
curl -fsSL -o "${SIG_PATH}.partial" "${SIG_URL}"
mv "${SIG_PATH}.partial" "${SIG_PATH}"

if command -v minisign >/dev/null 2>&1; then
  echo "[install-vyos] Verifying ISO with minisign + VyOS public key..."
  minisign -V -P "${VYOS_PUBKEY}" -m "${ISO_PATH}" -x "${SIG_PATH}"
else
  echo "[install-vyos] WARNING: minisign not installed; skipping signature verification (size check only)."
  actual_size="$(stat -c%s "${ISO_PATH}")"
  if [[ "${actual_size}" -ne "${EXPECTED_SIZE}" ]]; then
    echo "[install-vyos] ISO size mismatch: got ${actual_size}, expected ${EXPECTED_SIZE}" >&2
    exit 1
  fi
fi

ln -sf "${ISO_NAME}" "${CDROM_PATH}"

chown -R "${OWNER_USER}:${OWNER_GROUP}" "${TEMPLATE_DIR}"
chmod 0644 "${ISO_PATH}" "${SIG_PATH}"

echo "[install-vyos] Done."
echo "[install-vyos]   Template directory: ${TEMPLATE_DIR}"
echo "[install-vyos]   Image key (use in node 'image' field): vyos-${VYOS_RELEASE_TAG}"
ls -lh "${TEMPLATE_DIR}"
