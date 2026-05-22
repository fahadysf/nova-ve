# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
#
# Atomic Bridge-Cloud marker writer.  Sourced (not executed) by both the
# provision script and the post-boot service script.  Provides exactly
# one function, ``_write_marker_atomic``, so every state transition
# (``naming-flipped``, ``complete``, ``rollback-applied``,
# ``rollback-applied-emergency``, ``unsupported-renderer``) goes through
# the same temp+chmod+chown+rename path.
#
# Plain ``printf >$MARKER`` writes are forbidden — they leave a partial
# file on a crash mid-write.  See .omc/plans/bridge-cloud-feature.md
# §4.1 for the full contract.

_NOVA_VE_MARKER_DIR="${_NOVA_VE_MARKER_DIR:-/etc/nova-ve}"
_NOVA_VE_MARKER_PATH="${_NOVA_VE_MARKER_PATH:-${_NOVA_VE_MARKER_DIR}/bridge-cloud.state}"

# _write_marker_atomic <state>
#
# Writes ``<state>`` to the marker file via mktemp + mv -f so an observer
# never sees a partial file.  Ensures mode 0600 root:root.  Returns
# non-zero on any failure.
_write_marker_atomic() {
    local state="$1"
    local marker="${_NOVA_VE_MARKER_PATH}"
    local tmp
    mkdir -p "${_NOVA_VE_MARKER_DIR}" || return 1
    tmp="$(mktemp -p "${_NOVA_VE_MARKER_DIR}" .bridge-cloud.state.XXXXXX)" || return 1
    printf '%s\n' "${state}" > "${tmp}" || { rm -f "${tmp}"; return 1; }
    chmod 0600 "${tmp}" || { rm -f "${tmp}"; return 1; }
    if command -v chown >/dev/null 2>&1; then
        chown root:root "${tmp}" 2>/dev/null || true
    fi
    mv -f "${tmp}" "${marker}" || { rm -f "${tmp}"; return 1; }
}
