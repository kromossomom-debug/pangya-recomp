#!/bin/sh
# Bisect predicate: boots the game headless for a while; fails on a CPU exception or early stop.
# usage: check_run.sh BUILD_DIR ISO [EMULATED_SECONDS]
BUILD="$1"
ISO="$2"
SECS="${3:-45}"
OUT=$("$BUILD/PPSSPPHeadless" --cpu=interpreter --graphics=null --timeout-wall=120 --timeout-emulated="$SECS" "$ISO" 2>&1)
case "$OUT" in
	*"Invalid jump"*|*"Invalid read"*|*"Invalid write"*|*"Bad"*"exception"*) exit 1 ;;
	*"TIMEOUT (emulated)"*) exit 0 ;;
	*) exit 2 ;;
esac
