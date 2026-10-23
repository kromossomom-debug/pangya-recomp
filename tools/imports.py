# List PRX imports (.lib.stub) and resolve NID names from PPSSPP HLE tables.
import re, struct, sys, glob, json
elf_path, ppsspp = sys.argv[1], sys.argv[2]
d = open(elf_path, 'rb').read()
shoff, = struct.unpack_from('<I', d, 0x20); shnum, shstr = struct.unpack_from('<HH', d, 0x30)
secs = []
for i in range(shnum):
    s = struct.unpack_from('<10I', d, shoff + i*40); secs.append(s)
so = secs[shstr][4]
def name(o): e = d.index(b'\0', so+o); return d[so+o:e].decode()
S = {name(s[0]): s for s in secs}
# vaddr -> file offset (text/data share one PT_LOAD: off = addr + text_off)
base_off = S['.text'][4] - S['.text'][3]
rd = lambda a, n: d[a+base_off:a+base_off+n]
cstr = lambda a: rd(a, 64).split(b'\0')[0].decode()
names = {}
for f in glob.glob(ppsspp + '/Core/HLE/*.cpp'):
    for m in re.finditer(r'\{\s*(0[xX][0-9A-Fa-f]{8})\s*,[^{}]*?"([A-Za-z_0-9]+)"', open(f, errors='ignore').read()):
        names.setdefault(int(m.group(1), 16), m.group(2))
st = S['.lib.stub']; a = st[3]; out = {}
while a < st[3] + st[5]:
    nm, ver, attr, entsz, nvar, nfunc, nids, funcs = struct.unpack_from('<IHHBBHII', rd(a, 20))
    lib = cstr(nm); out[lib] = []
    for k in range(nfunc):
        nid, = struct.unpack('<I', rd(nids + 4*k, 4))
        out[lib].append((hex(nid), names.get(nid, '?'), hex(funcs + 8*k)))
    a += entsz * 4
tot = sum(len(v) for v in out.values()); unk = sum(1 for v in out.values() for x in v if x[1] == '?')
print(f'{len(out)} libs, {tot} imported functions, {unk} unnamed')
for lib, fs in out.items():
    print(f'\n[{lib}] ({len(fs)})'); print('  ' + ', '.join(f[1] if f[1] != '?' else f[0] for f in fs))
json.dump(out, open(elf_path + '.imports.json', 'w'), indent=1)
