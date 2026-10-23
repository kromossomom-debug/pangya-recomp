#!/usr/bin/env python3
"""Find the compiled function(s) behind a divergence by sending ranges of them to the interpreter.

usage: bisect.py GEN_DIR -- COMMAND...
COMMAND is run with PPSSPP_RECOMP_OFF set; it must exit 0 when the run is good and nonzero when
the bug shows up.
"""
import os
import re
import subprocess
import sys


def starts_from(gen):
    s = open(os.path.join(gen, 'funcs.h')).read()
    return sorted(int(x, 16) for x in re.findall(r'void F_([0-9A-F]{8})\(', s))


def run(cmd, starts, lo, hi):
    """Disable functions starts[lo:hi]. Returns True if the run was good."""
    if lo >= hi:
        rng = ''
    else:
        end = starts[hi] if hi < len(starts) else 0x0A000000
        rng = '%08x-%08x' % (starts[lo], end)
    env = dict(os.environ, PPSSPP_RECOMP_OFF=rng or '0-0')
    ok = subprocess.run(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    print('  off %-20s -> %s' % (rng or '(none)', 'good' if ok else 'BAD'), flush=True)
    return ok


def main():
    i = sys.argv.index('--')
    gen, cmd = sys.argv[1], sys.argv[i + 1:]
    starts = starts_from(gen)
    print('%d functions' % len(starts))
    if run(cmd, starts, 0, 0):
        sys.exit('fails to reproduce with everything compiled')
    if not run(cmd, starts, 0, len(starts)):
        sys.exit('still bad with everything interpreted')
    # Invariant: disabling [lo, hi) fixes it.
    lo, hi = 0, len(starts)
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if run(cmd, starts, lo, mid):
            hi = mid
        elif run(cmd, starts, mid, hi):
            lo = mid
        else:
            print('needs functions from both halves of %08x-%08x' % (starts[lo], starts[hi - 1]))
            break
    print('culprit range: %d functions starting at %08x' % (hi - lo, starts[lo]))
    for a in starts[lo:hi][:20]:
        print('  F_%08X' % a)


if __name__ == '__main__':
    main()
