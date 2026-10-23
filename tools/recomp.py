#!/usr/bin/env python3
"""Static recompiler: PSP (Allegrex MIPS) module image -> C++ for the PPSSPP Recomp runtime.

Input is a dump made by the runtime itself (PPSSPP_RECOMP_DUMP=dir): the relocated module
image, its metadata, and the interpreter's decode of every text word (ops.txt), so the
instruction identity always agrees with PPSSPP's tables.

Model (see Core/Recomp/Recomp.h):
  * Code is split into regions at function starts. Each region becomes one C++ function with a
    switch over its entry labels at the top, so it can be resumed at any label.
  * All guest state stays in MIPSState. Calls are native C++ calls; after one returns, the caller
    continues only if mips->pc is the expected return address, otherwise it returns too and the
    dispatcher picks up from mips->pc. That makes thread switches, interrupts and timeslice ends
    work without any host stack switching.
  * Loop heads check the downcount and leave when it runs out.
  * Anything not handled here (VFPU ops, rare system ops) calls the interpreter for that one
    instruction; code that can't be compiled at all is left to the interpreter by not giving
    it an entry.
"""

import argparse
import os
import struct
import sys
from collections import defaultdict

# ---------------------------------------------------------------- input


def load_dump(d):
    meta = {}
    imports = []
    for line in open(os.path.join(d, 'meta.txt')):
        k, _, v = line.strip().partition(' ')
        if k == 'import':
            a, nid, mod = v.split()
            imports.append((int(a, 16), int(nid, 16), mod))
        elif k == 'name':
            meta[k] = v
        else:
            meta[k] = int(v, 16)
    image = open(os.path.join(d, 'image.bin'), 'rb').read()
    ops = []
    for line in open(os.path.join(d, 'ops.txt')):
        name, flags, cycles = line.split()
        ops.append((name, int(flags, 16), int(cycles)))
    return meta, imports, image, ops


# MIPSInfo flags (Core/MIPS/MIPSTables.h)
IS_CONDBRANCH = 0x80
IS_JUMP = 0x100
LIKELY = 0x40
DELAYSLOT = 0x10
IS_VFPU = 0x40000000
IS_SYSCALL = 0x200000000000


def s16(x):
    return x - 0x10000 if x & 0x8000 else x


class Insn:
    __slots__ = ('pc', 'w', 'name', 'flags', 'cycles')

    def __init__(self, pc, w, name, flags, cycles):
        self.pc, self.w, self.name, self.flags, self.cycles = pc, w, name, flags, cycles

    op = property(lambda s: s.w >> 26)
    rs = property(lambda s: (s.w >> 21) & 31)
    rt = property(lambda s: (s.w >> 16) & 31)
    rd = property(lambda s: (s.w >> 11) & 31)
    sa = property(lambda s: (s.w >> 6) & 31)
    funct = property(lambda s: s.w & 63)
    imm = property(lambda s: s.w & 0xFFFF)
    simm = property(lambda s: s16(s.w & 0xFFFF))

    @property
    def is_branch(self):
        return bool(self.flags & (IS_CONDBRANCH | IS_JUMP))

    def branch_target(self):
        """Static target of a branch/jump, or None for register jumps."""
        if self.op in (2, 3):
            return (self.pc & 0xF0000000) | ((self.w & 0x03FFFFFF) << 2)
        if self.flags & IS_CONDBRANCH:
            return (self.pc + 4 + (self.simm << 2)) & 0xFFFFFFFF
        return None


# ---------------------------------------------------------------- analysis


class Program:
    def __init__(self, meta, imports, image, ops):
        self.meta = meta
        self.image = image
        self.base = meta['image_start']
        self.text_start = meta['text_start']
        self.text_end = meta['text_end']
        self.code_end = meta['stub_start']  # compiled range is [text_start, code_end)
        self.imports = {a: (nid, mod) for a, nid, mod in imports}
        self.insns = {}
        for i, (name, flags, cycles) in enumerate(ops):
            pc = self.text_start + 4 * i
            if pc >= self.code_end:
                break
            self.insns[pc] = Insn(pc, self.word(pc), name, flags, cycles)

    def word(self, addr):
        o = addr - self.base
        return struct.unpack_from('<I', self.image, o)[0]

    def in_code(self, a):
        return self.text_start <= a < self.code_end and (a & 3) == 0

    def in_stubs(self, a):
        return self.code_end <= a < self.text_end

    def analyze(self):
        insns = self.insns
        starts = {self.meta['entry']}
        labels = set()
        ptrs = set()
        loop_heads = set()
        bad = set()  # pcs that can't be compiled (branch in a delay slot, ...)

        for pc, ins in insns.items():
            if not ins.is_branch:
                continue
            t = ins.branch_target()
            ds = insns.get(pc + 4)
            if ds is not None and ds.is_branch:
                bad.add(pc)
            if ins.op == 3:  # jal
                if self.in_code(t):
                    starts.add(t)
                labels.add(pc + 8)
            elif ins.op == 0 and ins.funct == 9:  # jalr
                labels.add(pc + 8)
            elif ins.op == 1 and ins.rt in (16, 17, 18, 19):  # bltzal & co.
                labels.add(pc + 8)
                if self.in_code(t):
                    labels.add(t)
            elif t is not None and self.in_code(t):
                labels.add(t)
                if t <= pc:
                    loop_heads.add(t)
            if ins.name == 'break':
                labels.add(pc + 4)

        for pc, ins in insns.items():
            if ins.name == 'break':
                labels.add(pc + 4)
            # Syscalls only appear in import stubs, which aren't compiled; any other one (and
            # anything else that redirects the PC behind our back) goes to the interpreter.
            if ins.flags & IS_SYSCALL or ins.name in ('eret', 'halt', 'unk'):
                bad.add(pc)
                labels.add(pc + 4)

        # Code pointers stored in data (function pointers, vtables, jump tables).
        for a in range(self.text_end, self.meta['image_end'] - 3, 4):
            v = self.word(a)
            if self.in_code(v):
                ptrs.add(v)
        # Code addresses built in registers (lui + addiu/ori), e.g. thread entries and callbacks.
        for pc, ins in insns.items():
            if ins.op != 15:
                continue
            hi = ins.imm << 16
            for k in range(1, 12):
                nx = insns.get(pc + 4 * k)
                if nx is None:
                    break
                if nx.op in (9, 13) and nx.rs == ins.rt:
                    v = (hi + nx.simm) & 0xFFFFFFFF if nx.op == 9 else hi | nx.imm
                    if self.in_code(v):
                        ptrs.add(v)
                if nx.is_branch and k > 1:
                    break
        # A pointer right after a function's final jr ra is most likely a function itself.
        for p in ptrs:
            prev = insns.get(p - 8)
            if prev is not None and prev.w == 0x03E00008:
                starts.add(p)
        labels |= ptrs
        labels |= starts
        labels = {l for l in labels if self.in_code(l)}
        self.starts = sorted(s for s in starts if self.in_code(s))
        self.labels = labels
        self.ptrs = ptrs
        self.loop_heads = loop_heads
        self.bad = bad

        # Regions: [start_i, start_i+1).
        self.regions = []
        ss = self.starts
        if not ss or ss[0] != self.text_start:
            ss = [self.text_start] + ss
            self.starts = ss
        for i, s in enumerate(ss):
            e = ss[i + 1] if i + 1 < len(ss) else self.code_end
            self.regions.append((s, e))
        import bisect
        self._bisect = bisect

        # When a delay slot is also a label (or starts the next region), the branch's
        # fallthrough has to skip over it to pc + 8, so that needs a label too.
        region_starts = set(ss)
        for pc, ins in insns.items():
            if ins.is_branch and (pc + 4 in self.labels or pc + 4 in region_starts) and self.in_code(pc + 8):
                self.labels.add(pc + 8)

    def region_for(self, a):
        i = self._bisect.bisect_right(self.starts, a) - 1
        return self.regions[i]


# ---------------------------------------------------------------- code generation


def fn_name(a):
    return 'F_%08X' % a


def lbl(a):
    return 'L_%08X' % a


def r(n):
    return '0u' if n == 0 else 'R(%d)' % n


class RegionGen:
    def __init__(self, prog, start, end):
        self.p = prog
        self.start, self.end = start, end
        self.out = []
        self.pending = 0  # cycles not yet charged
        self.local_labels = set()
        self.entries = []

    def emit(self, s):
        self.out.append(s)

    def flush(self):
        if self.pending:
            self.emit('\tm->downcount -= %d;' % self.pending)
            self.pending = 0

    def is_entry(self, a):
        return a in self.p.labels and a not in self.p.bad and self.start <= a < self.end

    # A transfer of control to a static address.
    def goto(self, t, indent='\t'):
        p = self.p
        if self.start <= t < self.end and self.is_entry(t):
            return indent + 'goto %s;' % lbl(t)
        if p.in_code(t) and t in p.labels and t not in p.bad:
            rs, _ = p.region_for(t)
            return indent + '{ m->pc = 0x%08X; %s(m); return; }' % (t, fn_name(rs))
        return indent + '{ m->pc = 0x%08X; return; }' % t

    def call(self, t, ret):
        p = self.p
        if p.in_code(t) and t in p.starts:
            self.emit('\tm->pc = 0x%08X; %s(m); RC_CHECK_RET(0x%08X);' % (t, fn_name(t), ret))
        elif p.in_stubs(t):
            nid, mod = p.imports.get(t, (0, '?'))
            self.emit('\tif (!Recomp_TryStub(m, 0x%08X)) { m->pc = 0x%08X; return; }  // %s %08x' % (t, t, mod, nid))
            self.emit('\tRC_CHECK_RET(0x%08X);' % ret)
        else:
            self.emit('\t{ m->pc = 0x%08X; return; }' % t)

    # ---- straight-line instructions

    def simple(self, ins):
        """Returns C code for a non-branch instruction, or None to interpret it."""
        n, op = ins.name, ins.op
        rs, rt, rd, sa = ins.rs, ins.rt, ins.rd, ins.sa
        imm, simm = ins.imm, ins.simm
        w = ins.w
        RS, RT = r(rs), r(rt)

        if op == 0:
            f = ins.funct
            if f in (0, 2, 3, 4, 6, 7):
                if rd == 0:
                    return ''
                if f == 0:
                    e = '%s << %d' % (RT, sa)
                elif f == 2:
                    e = ('__rotr(%s, %d)' % (RT, sa)) if rs & 1 else ('%s >> %d' % (RT, sa))
                elif f == 3:
                    e = '(u32)((s32)%s >> %d)' % (RT, sa)
                elif f == 4:
                    e = '%s << (%s & 0x1F)' % (RT, RS)
                elif f == 6:
                    e = ('__rotr(%s, %s)' % (RT, RS)) if sa & 1 else ('%s >> (%s & 0x1F)' % (RT, RS))
                else:
                    e = '(u32)((s32)%s >> (%s & 0x1F))' % (RT, RS)
                return 'R(%d) = %s;' % (rd, e)
            if f in (10, 11):
                if rd == 0:
                    return ''
                c = '==' if f == 10 else '!='
                return 'if (%s %s 0) R(%d) = %s;' % (RT, c, rd, RS)
            if f == 15:  # sync
                return ''
            if f == 16:
                return '' if rd == 0 else 'R(%d) = m->hi;' % rd
            if f == 17:
                return 'm->hi = %s;' % RS
            if f == 18:
                return '' if rd == 0 else 'R(%d) = m->lo;' % rd
            if f == 19:
                return 'm->lo = %s;' % RS
            if f in (22, 23):
                if rd == 0:
                    return ''
                return 'R(%d) = clz32(%s%s);' % (rd, '~' if f == 23 else '', RS)
            if f == 24:
                return 'RC_mult(m, %s, %s);' % (RS, RT)
            if f == 25:
                return 'RC_multu(m, %s, %s);' % (RS, RT)
            if f == 26:
                return 'RC_div(m, %s, %s);' % (RS, RT)
            if f == 27:
                return 'RC_divu(m, %s, %s);' % (RS, RT)
            if f in (28, 29, 46, 47):
                return 'RC_madd(m, %s, %s, %s, %s);' % (RS, RT, 'true' if f >= 46 else 'false',
                                                          'true' if f in (28, 46) else 'false')
            if 32 <= f <= 45 and f not in (40, 41):
                if rd == 0:
                    return ''
                e = {
                    32: '%s + %s', 33: '%s + %s', 34: '%s - %s', 35: '%s - %s',
                    36: '%s & %s', 37: '%s | %s', 38: '%s ^ %s', 39: '~(%s | %s)',
                    42: '(u32)((s32)%s < (s32)%s)', 43: '(u32)(%s < %s)',
                }.get(f)
                if e:
                    return 'R(%d) = %s;' % (rd, e % (RS, RT))
                if f == 44:
                    return 'R(%d) = ((s32)%s > (s32)%s) ? %s : %s;' % (rd, RS, RT, RS, RT)
                if f == 45:
                    return 'R(%d) = ((s32)%s < (s32)%s) ? %s : %s;' % (rd, RS, RT, RS, RT)
            return None

        if 8 <= op <= 15:
            if rt == 0:
                return ''
            if op in (8, 9):
                return 'R(%d) = %s + 0x%08Xu;' % (rt, RS, simm & 0xFFFFFFFF)
            if op == 10:
                return 'R(%d) = (u32)((s32)%s < %d);' % (rt, RS, simm)
            if op == 11:
                return 'R(%d) = (u32)(%s < 0x%08Xu);' % (rt, RS, simm & 0xFFFFFFFF)
            if op == 12:
                return 'R(%d) = %s & 0x%04Xu;' % (rt, RS, imm)
            if op == 13:
                return 'R(%d) = %s | 0x%04Xu;' % (rt, RS, imm)
            if op == 14:
                return 'R(%d) = %s ^ 0x%04Xu;' % (rt, RS, imm)
            return 'R(%d) = 0x%08Xu;' % (rt, imm << 16)

        addr = '(%s + 0x%08Xu)' % (RS, simm & 0xFFFFFFFF)
        if 32 <= op <= 46:
            loads = {32: 'LB', 33: 'LH', 35: 'LW', 36: 'LBU', 37: 'LHU'}
            stores = {40: 'SB', 41: 'SH', 43: 'SW'}
            if op in loads:
                return '' if rt == 0 else 'R(%d) = %s(%s);' % (rt, loads[op], addr)
            if op in stores:
                return '%s(%s, %s);' % (stores[op], addr, RT)
            if op == 34:
                return '' if rt == 0 else 'R(%d) = RC_lwl(mem, %s, %s);' % (rt, addr, RT)
            if op == 38:
                return '' if rt == 0 else 'R(%d) = RC_lwr(mem, %s, %s);' % (rt, addr, RT)
            if op == 42:
                return 'RC_swl(mem, %s, %s);' % (addr, RT)
            if op == 46:
                return 'RC_swr(mem, %s, %s);' % (addr, RT)
            return None
        if op == 49:  # lwc1
            return 'FI(%d) = LW(%s);' % (rt, addr)
        if op == 57:  # swc1
            return 'SW(%s, FI(%d));' % (addr, rt)

        if op == 31:  # special3
            f = ins.funct
            if rt == 0:
                return ''
            if f == 0:  # ext
                size = rd + 1
                mask = 0xFFFFFFFF >> (32 - size)
                return 'R(%d) = (%s >> %d) & 0x%08Xu;' % (rt, RS, sa, mask)
            if f == 4:  # ins
                return 'R(%d) = RC_ins(%s, %s, %d, %d);' % (rt, RT, RS, sa, rd)
            if f == 0x20:  # bshfl
                if rd == 0:
                    return ''
                if sa & 0x10:
                    if sa & 4:
                        return 'R(%d) = ReverseBits32(%s);' % (rd, RT)
                    if sa & 8:
                        return 'R(%d) = (u32)(s32)(s16)%s;' % (rd, RT)
                    return 'R(%d) = (u32)(s32)(s8)%s;' % (rd, RT)
                if sa & 1:
                    return 'R(%d) = swap32(%s);' % (rd, RT)
                return 'R(%d) = ((%s & 0xFF00FF00) >> 8) | ((%s & 0x00FF00FF) << 8);' % (rd, RT, RT)
            return None

        if op == 17:  # cop1
            fmt = rs
            fs, ft, fd = rd, rt, sa
            if fmt == 0:
                return '' if rt == 0 else 'R(%d) = FI(%d);' % (rt, fs)
            if fmt == 2:
                return '' if rt == 0 else 'R(%d) = RC_cfc1(m, %d);' % (rt, fs)
            if fmt == 4:
                return 'FI(%d) = %s;' % (fs, RT)
            if fmt == 6:
                return 'RC_ctc1(m, %d, %s);' % (fs, RT)
            if fmt == 16:
                f = ins.funct
                if f == 0:
                    return 'F(%d) = F(%d) + F(%d);' % (fd, fs, ft)
                if f == 1:
                    return 'F(%d) = F(%d) - F(%d);' % (fd, fs, ft)
                if f == 2:
                    return 'RC_mul_s(m, %d, %d, %d);' % (fd, fs, ft)
                if f == 3:
                    return 'RC_div_s(m, %d, %d, %d);' % (fd, fs, ft)
                if f == 4:
                    return 'RC_sqrt_s(m, %d, %d);' % (fd, fs)
                if f == 5:
                    return 'F(%d) = fabsf(F(%d));' % (fd, fs)
                if f == 6:
                    return 'F(%d) = F(%d);' % (fd, fs)
                if f == 7:
                    return 'F(%d) = -F(%d);' % (fd, fs)
                if 12 <= f <= 15:
                    mode = {12: 0, 13: 1, 14: 2, 15: 3}[f]
                    return 'FS(%d) = RC_f2i(F(%d), %d);' % (fd, fs, mode)
                if f == 36:
                    return 'FS(%d) = RC_f2i(F(%d), m->fcr31 & 3);' % (fd, fs)
                if 48 <= f <= 63:
                    return 'm->fpcond = RC_fcmp(%d, F(%d), F(%d));' % (f & 15, fs, ft)
                return None
            if fmt == 20 and ins.funct == 32:
                return 'F(%d) = (float)FS(%d);' % (fd, fs)
            return None
        return None

    # ---- branches

    def branch_cond(self, ins):
        """C condition for a conditional branch, evaluated before its delay slot."""
        op, rs, rt = ins.op, ins.rs, ins.rt
        RS, RT = r(rs), r(rt)
        if op in (4, 20):
            return '%s == %s' % (RS, RT)
        if op in (5, 21):
            return '%s != %s' % (RS, RT)
        if op in (6, 22):
            return '(s32)%s <= 0' % RS
        if op in (7, 23):
            return '(s32)%s > 0' % RS
        if op == 1:
            k = rt & 3
            return '(s32)%s %s 0' % (RS, '<' if k in (0, 2) else '>=')
        if op == 17 and rs == 8:  # bc1
            return 'm->fpcond' if (rt & 1) else '!m->fpcond'
        if op == 18 and rs == 8:  # bvf/bvt (cop2 BC)
            imm3 = (ins.w >> 18) & 7
            v = '((m->vfpuCtrl[VFPU_CTRL_CC] >> %d) & 1)' % imm3
            return v if (rt & 1) else '!%s' % v
        return None

    def is_likely(self, ins):
        op = ins.op
        if op in (20, 21, 22, 23):
            return True
        if op == 1:
            return (ins.rt & 3) >= 2
        if op in (17, 18):
            return (ins.rt & 2) != 0
        return False

    def delay_code(self, pc):
        ds = self.p.insns.get(pc + 4)
        if ds is None:
            return ['\t// delay slot outside compiled range']
        self.pending += ds.cycles
        c = self.simple(ds)
        if c is None:
            return ['\tRecomp_Interpret(m, 0x%08X, 0x%08X);  // %s' % (ds.pc, ds.w, ds.name)]
        return ['\t' + c] if c else []

    def gen_branch(self, ins):
        pc = ins.pc
        t = ins.branch_target()
        op = ins.op
        self.pending += ins.cycles

        if op == 2:  # j
            ds = self.delay_code(pc)
            self.flush()
            self.out += ds
            self.emit(self.goto(t))
            return
        if op == 3:  # jal
            self.emit('\tR(31) = 0x%08X;' % (pc + 8))
            ds = self.delay_code(pc)
            self.flush()
            self.out += ds
            self.call(t, pc + 8)
            return
        if op == 0 and ins.funct == 8:  # jr
            self.emit('\t{ const u32 t = %s;' % r(ins.rs))
            ds = self.delay_code(pc)
            self.flush()
            self.out += ds
            if ins.rs == 31:
                self.emit('\tm->pc = t; return; }')
            else:
                cases = sorted(a for a in self.p.ptrs if self.start <= a < self.end and self.is_entry(a))
                if cases:
                    self.emit('\tswitch (t) {')
                    for a in cases:
                        self.emit('\tcase 0x%08X: goto %s;' % (a, lbl(a)))
                    self.emit('\tdefault: break;')
                    self.emit('\t}')
                self.emit('\tm->pc = t; { RecompFunc f = Recomp_Lookup(t); if (f) f(m); } return; }')
            return
        if op == 0 and ins.funct == 9:  # jalr
            self.emit('\t{ const u32 t = %s;' % r(ins.rs))
            if ins.rd != 0:
                self.emit('\tR(%d) = 0x%08X;' % (ins.rd, pc + 8))
            ds = self.delay_code(pc)
            self.flush()
            self.out += ds
            self.emit('\tRC_CallIndirect(m, t); RC_CHECK_RET(0x%08X); }' % (pc + 8))
            return

        cond = self.branch_cond(ins)
        if cond is None or t is None:
            raise ValueError('unhandled branch %s at %08x' % (ins.name, pc))
        link = op == 1 and ins.rt >= 16
        likely = self.is_likely(ins)
        self.emit('\t{ const bool c = %s;' % cond)
        if link:
            self.emit('\tR(31) = 0x%08X;' % (pc + 8))
        ds = self.delay_code(pc)
        self.flush()
        if likely:
            self.emit('\tif (c) {')
            self.out += ['\t' + x for x in ds]
            self.emit(self.goto(t, '\t\t'))
            self.emit('\t} }')
        else:
            self.out += ds
            self.emit('\tif (c) ' + self.goto(t, '').strip())
            self.emit('\t}')

    # ---- region

    def generate(self):
        p = self.p
        pcs = [a for a in range(self.start, self.end, 4)]
        entries = [a for a in pcs if self.is_entry(a)]
        self.entries = entries
        body = []
        self.out = body
        inlined_slot = None  # delay slot already emitted inside the preceding branch
        for pc in pcs:
            ins = p.insns[pc]
            is_lbl = pc in p.labels
            if pc == inlined_slot:
                inlined_slot = None
                if not is_lbl:
                    continue
                # Something also jumps straight to this delay slot. The branch before it falls
                # through past the slot; the label below starts a copy of it as ordinary code.
                self.emit(self.goto(pc + 4))
            if is_lbl or pc in p.bad:
                self.flush()
            if pc in p.bad:
                # Left to the interpreter (no entry, so the dispatcher interprets it).
                self.emit('\t{ m->pc = 0x%08X; return; }' % pc)
                continue
            if is_lbl:
                self.emit('%s:' % lbl(pc))
                if pc == self.start:
                    self.emit('\tRC_HOOK_CHECK(0x%08X);' % pc)
                if pc in p.loop_heads:
                    self.emit('\tif (m->downcount < 0) { m->pc = 0x%08X; return; }' % pc)
            if ins.is_branch:
                self.gen_branch(ins)
                inlined_slot = pc + 4
                continue
            self.pending += ins.cycles
            c = self.simple(ins)
            if c is None:
                self.emit('\tRecomp_Interpret(m, 0x%08X, 0x%08X);  // %s' % (pc, ins.w, ins.name))
                if ins.name == 'break':
                    self.flush()
                    self.emit('\tif (!RC_RUNNING) { m->pc = 0x%08X; return; }' % (pc + 4))
            elif c:
                self.emit('\t' + c)
        self.flush()
        # Falling off the end of the region continues in the next one (past a delay slot that
        # belongs to our last branch, if any).
        self.emit(self.goto(self.end + 4 if inlined_slot == self.end else self.end))
        text = '\n'.join(body)

        head = ['void %s(MIPSState *m) {' % fn_name(self.start),
                '\tif (Recomp_IsOff(0x%08X)) return;' % self.start,
                '\tu8 *const mem = Memory::base;',
                '\tswitch (m->pc) {']
        for a in entries:
            head.append('\tcase 0x%08X: goto %s;' % (a, lbl(a)))
        head.append('\tdefault: Recomp_BadEntry(m, 0x%08X); return;' % self.start)
        head.append('\t}')
        return '\n'.join(head) + '\n' + text + '\n}\n'


def fnv1a(data):
    h = 0xcbf29ce484222325
    for b in data:
        h ^= b
        h = (h * 0x100000001b3) & 0xFFFFFFFFFFFFFFFF
    return h


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('dump')
    ap.add_argument('out')
    ap.add_argument('--insns-per-file', type=int, default=24000)
    args = ap.parse_args()

    meta, imports, image, ops = load_dump(args.dump)
    p = Program(meta, imports, image, ops)
    p.analyze()
    os.makedirs(args.out, exist_ok=True)

    hs, he = p.text_start, p.code_end
    h = fnv1a(image[hs - p.base:he - p.base])
    if h != meta['hash']:
        sys.exit('hash mismatch: dump says %016x, image gives %016x' % (meta['hash'], h))

    rt_src = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'recomp_rt.h')
    open(os.path.join(args.out, 'recomp_rt.h'), 'w').write(open(rt_src).read())

    protos = ['#pragma once', '#include "recomp_rt.h"', '']
    protos += ['void %s(MIPSState *m);' % fn_name(s) for s, _ in p.regions]
    open(os.path.join(args.out, 'funcs.h'), 'w').write('\n'.join(protos) + '\n')

    all_entries = []
    files = []
    cur, cur_n = [], 0
    stats = defaultdict(int)
    for s, e in p.regions:
        g = RegionGen(p, s, e)
        code = g.generate()
        all_entries += [(a, s) for a in g.entries]
        cur.append(code)
        cur_n += (e - s) // 4
        if cur_n >= args.insns_per_file:
            files.append(cur)
            cur, cur_n = [], 0
        stats['interp'] += code.count('Recomp_Interpret(')
    if cur:
        files.append(cur)

    for i, chunk in enumerate(files):
        with open(os.path.join(args.out, 'code_%03d.cpp' % i), 'w') as f:
            f.write('// Generated by tools/recomp.py - do not edit.\n#include "funcs.h"\n\n')
            f.write('\n'.join(chunk))

    with open(os.path.join(args.out, 'table.cpp'), 'w') as f:
        f.write('// Generated by tools/recomp.py - do not edit.\n#include "funcs.h"\n\n')
        f.write('static const RecompEntry entries[] = {\n')
        for a, s in sorted(all_entries):
            f.write('\t{0x%08X, 0x%08X, %s},\n' % (a, s, fn_name(s)))
        f.write('};\n\n')
        f.write('const RecompImage *Recomp_GetImage() {\n')
        f.write('\tstatic const RecompImage image = {"%s", 0x%08X, 0x%08X, 0x%08X, 0x%08X, 0x%016XULL, entries, %d};\n'
                % (meta['name'], p.text_start, p.code_end - p.text_start, hs, he, h, len(all_entries)))
        f.write('\treturn &image;\n}\n')

    print('%d instructions, %d regions, %d entries, %d loop heads, %d uncompilable, %d interpreted sites, %d files'
          % (len(p.insns), len(p.regions), len(all_entries), len(p.loop_heads), len(p.bad), stats['interp'], len(files)))


if __name__ == '__main__':
    main()
