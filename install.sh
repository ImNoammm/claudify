#!/usr/bin/env bash
# Installs claudify for the current user. Run it from a checkout, or pipe it from GitHub:
#   curl -fsSL https://raw.githubusercontent.com/ImNoammm/claudify/main/install.sh | bash
# Any argument is passed on to "claudify install" (for example the path to a Claude AppImage).
set -euo pipefail

REPO="https://github.com/ImNoammm/claudify"
DEST="${XDG_DATA_HOME:-$HOME/.local/share}/claudify"
BIN_DIR="$HOME/.local/bin"
CSS_DIR="${CLAUDIFY_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/claudify/css}"

die() { echo "claudify: $*" >&2; exit 1; }
say() { echo "claudify: $*"; }

[[ $(uname -s) == Linux ]] || die "claudify only supports Linux"
[[ $EUID -ne 0 ]] || die "run this as your normal user, not root (it asks for sudo only when it needs it)"
command -v python3 >/dev/null || die "python3 is required. Install it with your package manager and run this again."

SRC=""
if [[ -n ${BASH_SOURCE[0]:-} && -f $(dirname "${BASH_SOURCE[0]}")/lib/loader.js ]]; then
  SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
else
  command -v tar >/dev/null || die "tar is required"
  TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
  say "downloading claudify"
  if command -v curl >/dev/null; then curl -fsSL "$REPO/archive/refs/heads/main.tar.gz" -o "$TMP/src.tar.gz"
  elif command -v wget >/dev/null; then wget -qO "$TMP/src.tar.gz" "$REPO/archive/refs/heads/main.tar.gz"
  else die "curl or wget is required"
  fi
  tar -xzf "$TMP/src.tar.gz" -C "$TMP"
  SRC="$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
fi

if [[ $SRC != "$DEST" ]]; then
  say "copying files to $DEST"
  rm -rf "$DEST.new"
  mkdir -p "$DEST.new"
  cp -r "$SRC"/{claudify,install.sh,lib,mcp,plugins,assets,docs} "$DEST.new"/
  cp "$SRC"/{README.md,LICENSE} "$DEST.new"/ 2>/dev/null || true
  rm -rf "$DEST"; mv "$DEST.new" "$DEST"
fi
chmod +x "$DEST/claudify" "$DEST/install.sh"
mkdir -p "$BIN_DIR"
ln -sfn "$DEST/claudify" "$BIN_DIR/claudify"

# ship the bundled theme, and update it when this release has a newer version
version() { sed -n 's/^ \* @version //p' "$1" 2>/dev/null | head -n 1; }
mkdir -p "$CSS_DIR"
theme="$CSS_DIR/theme.css"; [[ -f $theme.off ]] && theme="$theme.off"
if [[ ! -f $theme ]] || [[ $(printf '%s\n%s\n' "$(version "$theme")" "$(version "$DEST/plugins/theme.css")" | sort -V | tail -n 1) != "$(version "$theme")" ]]; then
  cp "$DEST/plugins/theme.css" "$theme"
  say "installed the Claudify Theme $(version "$theme")"
fi

"$DEST/claudify" install "$@"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) say "add $BIN_DIR to your PATH to use the claudify command" ;;
esac
say "done. Restart Claude Desktop if it is open."
