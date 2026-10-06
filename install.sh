#!/usr/bin/env bash
# Installs the OrcaVision extension into Orca's user-extension folder and
# approves it. Orca loads it the next time it starts; to restart Orca now,
# run: orca --replace &
#
# Run this again after changing the extension: any change alters the
# extension's hash, and Orca will not load it until it is approved again.
set -euo pipefail

src="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/orcavision"
dest="${XDG_DATA_HOME:-$HOME/.local/share}/orca/extensions/orcavision"

mkdir -p "$dest"
rm -f "$dest"/*.py
cp "$src"/*.py "$dest"/
orca --approve-extension orcavision
echo "Installed OrcaVision in $dest. Restart Orca to load it."
