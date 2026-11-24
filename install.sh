#!/bin/sh
# t-off installer:  curl -fsSL https://raw.githubusercontent.com/sonozaki7/t-off/main/install.sh | sh
# Installs toff to ~/.local/share/t-off (linked from ~/.local/bin/toff) and turns T3 Code's telemetry off.
# Read it first if you like: it downloads two files and runs `toff optout`.
#   TOFF_NO_APPLY=1  install without changing any settings
#   TOFF_REF=<tag>   install a specific version instead of main
set -eu

base="${TOFF_BASE_URL:-https://raw.githubusercontent.com/sonozaki7/t-off/${TOFF_REF:-main}}"
data="${XDG_DATA_HOME:-$HOME/.local/share}/t-off"
bin="${TOFF_BIN_DIR:-$HOME/.local/bin}"

command -v curl >/dev/null 2>&1 || {
  echo "t-off: curl is required" >&2
  exit 1
}
mkdir -p "$data/lab" "$bin"
for file in toff lab/probe.py; do
  curl -fsSL "$base/$file" -o "$data/$file.tmp"
  mv "$data/$file.tmp" "$data/$file"
done
chmod +x "$data/toff"
ln -sf "$data/toff" "$bin/toff"
echo "Installed toff $("$data/toff" version | cut -d' ' -f2) to $bin/toff"
case ":$PATH:" in
  *":$bin:"*) ;;
  *) echo "Note: $bin is not on your PATH. Run $bin/toff, or add $bin to PATH." ;;
esac

if [ -z "${TOFF_NO_APPLY:-}" ]; then
  echo
  "$data/toff" optout
fi
