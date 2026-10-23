# Opcode-class histogram of .text (Allegrex), counting VFPU / FPU / syscall / jr usage.
import struct, sys, collections
d = open(sys.argv[1], 'rb').read()
off, size = 0x74, 0x32fc20
c = collections.Counter(); jal = 0; jr = 0; jalr = 0
for i in range(0, size, 4):
    w, = struct.unpack_from('<I', d, off + i); op = w >> 26
    if op in (0x18, 0x19, 0x1B, 0x34, 0x37, 0x3C, 0x32, 0x36, 0x3A, 0x3E, 0x35, 0x3D) or (op == 0x12): c['vfpu'] += 1
    elif op == 0x11 or op in (0x31, 0x39): c['fpu'] += 1
    elif op == 3: jal += 1
    elif op == 0 and (w & 0x3f) == 8: jr += 1
    elif op == 0 and (w & 0x3f) == 9: jalr += 1
    c['total'] += 1
print(dict(c), 'jal', jal, 'jr', jr, 'jalr', jalr)
