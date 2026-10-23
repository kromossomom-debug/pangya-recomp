#!/bin/sh
# Builds the native port from your own copy of the game.
#
#   ./build.sh "/path/to/Pangya - Fantasy Golf (USA).iso"
#
# Steps: fetch PPSSPP at the tested commit and apply the runtime patch, dump the game module as
# PPSSPP loads it, recompile it to C++, build, and package into dist/.
#
# Needs a Linux toolchain with cmake, ninja, gcc/g++, SDL3 + SDL3_ttf, GLEW, Vulkan headers,
# python3 and git. On SteamOS, run it inside a distrobox (see README); set BUILD_IN to the prefix,
# for example BUILD_IN="distrobox enter psp-build --".
set -e
ROOT=$(dirname "$(readlink -f "$0")")
ISO="${1:-$HOME/Downloads/Pangya - Fantasy Golf (USA).iso}"
IN="${BUILD_IN:-}"
PPSSPP="$ROOT/runtime/ppsspp"
BASE=$(sed -n 's/^base: //p' "$ROOT/runtime/PPSSPP_BASE.txt")

if [ ! -f "$ISO" ]; then
	echo "ISO not found: $ISO" >&2
	exit 1
fi

# 1. PPSSPP at the tested commit, plus the Recomp runtime.
if [ ! -d "$PPSSPP/.git" ]; then
	git clone https://github.com/hrydgard/ppsspp.git "$PPSSPP"
	git -C "$PPSSPP" checkout "$BASE"
	git -C "$PPSSPP" submodule update --init --recursive --depth 1
	git -C "$PPSSPP" apply "$ROOT/runtime/ppsspp-recomp.patch"
fi

CMAKE_OPTS="-G Ninja -DCMAKE_BUILD_TYPE=RelWithDebInfo -DHEADLESS=ON -DUSE_DISCORD=OFF -DUSE_MINIUPNPC=OFF -DRECOMP_GLIBC_COMPAT=ON -DCMAKE_C_COMPILER=gcc -DCMAKE_CXX_COMPILER=g++"

# 2. Runtime without game code, to dump the module.
$IN cmake -S "$PPSSPP" -B "$ROOT/build" $CMAKE_OPTS -DRECOMP_GAME_DIR=
$IN ninja -C "$ROOT/build" PPSSPPHeadless
mkdir -p "$ROOT/work/dump"
PPSSPP_RECOMP_DISABLE=1 PPSSPP_RECOMP_DUMP="$ROOT/work/dump" "$ROOT/build/PPSSPPHeadless" \
	--cpu=interpreter --graphics=null --timeout-emulated=1 --timeout-wall=30 "$ISO" >/dev/null 2>&1 || true
[ -f "$ROOT/work/dump/ops.txt" ] || { echo "module dump failed" >&2; exit 1; }

# 3. MIPS -> C++.
python3 -I "$ROOT/tools/recomp.py" "$ROOT/work/dump" "$ROOT/gen"

# 4. Build with the generated code linked in.
$IN cmake -S "$PPSSPP" -B "$ROOT/build" -DRECOMP_GAME_DIR="$ROOT/gen"
$IN ninja -C "$ROOT/build" PPSSPPSDL PPSSPPHeadless

# 5. Package.
mkdir -p "$ROOT/dist"
strip -o "$ROOT/dist/pangya-bin" "$ROOT/build/PPSSPPSDL"
rm -rf "$ROOT/dist/assets" && cp -r "$ROOT/build/assets" "$ROOT/dist/"
cp "$ROOT/launcher/pangya" "$ROOT/launcher/install.sh" "$ROOT/dist/"
# The game's own icon, from your ISO (optional).
if command -v 7z >/dev/null 2>&1; then
	7z e -y -o"$ROOT/dist" "$ISO" PSP_GAME/ICON0.PNG >/dev/null 2>&1 && mv -f "$ROOT/dist/ICON0.PNG" "$ROOT/dist/icon.png" || true
fi
echo "done: $ROOT/dist/pangya"
