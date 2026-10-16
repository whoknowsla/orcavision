#!/usr/bin/env bash
# Installs the OrcaVision extension into Orca's user extensions folder and
# approves it. Orca loads it the next time it starts; to restart Orca now,
# run: orca --replace &
#
# Run this again after changing the extension: any change alters the
# extension's hash, and Orca will not load it until it is approved again.
#
# Usage:
#   ./install.sh               install (or replace) and approve
#   ./install.sh --uninstall   revoke approval and remove the installed copy

set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source_dir="$repo_dir/orcavision"
target_root="${XDG_DATA_HOME:-$HOME/.local/share}/orca/extensions"
target_dir="$target_root/orcavision"

confirm() {
    printf '%s [y/N] ' "$1"
    read -r reply
    case "$reply" in
        [yY]*) return 0 ;;
        *) return 1 ;;
    esac
}

if [ "${1:-}" = "--uninstall" ]; then
    if [ ! -e "$target_dir" ]; then
        echo "OrcaVision is not installed in $target_dir."
        exit 0
    fi
    if ! confirm "Remove OrcaVision from $target_dir?"; then
        echo "Nothing was changed."
        exit 0
    fi
    if command -v orca >/dev/null 2>&1; then
        orca --revoke-extension orcavision || true
    fi
    rm -rf -- "$target_dir"
    echo "Removed OrcaVision. Its settings in Orca and the API keys it stored" \
        "(in your keyring, KWallet or systemd-creds) were left alone."
    exit 0
fi

if [ -n "${1:-}" ]; then
    echo "Unknown option: $1 (use --uninstall, or no option to install)" >&2
    exit 2
fi

if [ ! -f "$source_dir/__init__.py" ]; then
    echo "Cannot find orcavision/__init__.py next to this script." >&2
    exit 1
fi

if ! command -v orca >/dev/null 2>&1; then
    echo "Orca is not on PATH; install Orca 51 or later first." >&2
    exit 1
fi

if [ -e "$target_dir" ]; then
    if ! confirm "Replace the existing extension at $target_dir?"; then
        echo "Nothing was changed."
        exit 0
    fi
    rm -rf -- "$target_dir"
fi

mkdir -p "$target_root"
cp -r -- "$source_dir" "$target_dir"
find "$target_dir" -name '__pycache__' -type d -prune -exec rm -rf -- {} +

orca --approve-extension orcavision
echo "Installed OrcaVision in $target_dir. Restart Orca to load it."
