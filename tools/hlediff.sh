#!/bin/sh
# Normalize a PPSSPP debug log down to thread + HLE message, for diffing two runs.
sed -E 's/^[0-9:]+ //; s/\+[0-9]+us//; s/(starting)?[Tt]icks=[0-9]+//g; s/[A-Za-z0-9_/]+\.(cpp|h):[0-9]+ //' "$1" | grep -E "^\S+ +[DIWE]\[(sce|HLE|CPU)"
