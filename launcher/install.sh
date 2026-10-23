#!/bin/sh
# Installs the game to ~/.local/share/pangya-recomp/app and adds a menu entry.
set -e
HERE=$(dirname "$(readlink -f "$0")")
DEST="${XDG_DATA_HOME:-$HOME/.local/share}/pangya-recomp/app"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"

mkdir -p "$DEST" "$APPS"
cp -r "$HERE/pangya" "$HERE/pangya-bin" "$HERE/assets" "$DEST/"
[ -f "$HERE/icon.png" ] && cp "$HERE/icon.png" "$DEST/"
chmod +x "$DEST/pangya" "$DEST/pangya-bin"

cat > "$APPS/pangya-recomp.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Pangya: Fantasy Golf
Comment=PSP game, recompiled to native Linux code
Exec="$DEST/pangya"
Icon=$DEST/icon.png
Categories=Game;
Terminal=false
EOF

echo "Installed to $DEST"
echo "Menu entry: $APPS/pangya-recomp.desktop"
echo "Steam: Add a Non-Steam Game -> Pangya: Fantasy Golf"
