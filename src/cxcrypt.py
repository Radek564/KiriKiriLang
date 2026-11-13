"""KiriKiri Cx encryption scheme.

Some XP3 archives filter every file body through
a title-specific *Cx* filter.  The filter is described by a ``CxScheme``:

* ``m_mask`` / ``m_offset`` -- split point between the two key regions;
* ``PrologOrder`` / ``OddBranchOrder`` / ``EvenBranchOrder`` -- byte arrays
  that select which pseudo-x86 snippets build the key program;
* ``ControlBlock`` -- 1024 uint32 values used by the generated program;
* ``TpmFileName`` -- optional plugin that carries a control block.

The parameters ship with the titles that use them (they are collected in
``schemes/xp3_schemes.json``).  This module implements the filter itself,
including the ``CxProgram`` random code generator and the ``CxProgramNana``
variant, so bodies can be decoded and re-encoded without the game.

The filter is a symmetric XOR transform: ``encrypt`` is ``decrypt``.
"""
import json
import os
import struct

U32 = 0xFFFFFFFF

# Cx bytecode opcodes.
(
    NOP, RETN, MOV_EDI_ARG, PUSH_EBX, POP_EBX, PUSH_ECX, POP_ECX,
    MOV_EAX_EBX, MOV_EBX_EAX, MOV_ECX_EBX, MOV_EAX_CONTROL_BLOCK,
    MOV_EAX_EDI, MOV_EAX_INDIRECT, ADD_EAX_EBX, SUB_EAX_EBX,
    IMUL_EAX_EBX, AND_ECX_0F, SHR_EBX_1, SHL_EAX_1, SHR_EAX_CL,
    SHL_EAX_CL, OR_EAX_EBX, NOT_EAX, NEG_EAX, DEC_EAX, INC_EAX,
) = range(26)

IMMED = 0x100
MOV_EAX_IMMED = IMMED + 1
AND_EBX_IMMED = IMMED + 2
AND_EAX_IMMED = IMMED + 3
XOR_EAX_IMMED = IMMED + 4
ADD_EAX_IMMED = IMMED + 5
SUB_EAX_IMMED = IMMED + 6

LENGTH_LIMIT = 0x80


class CxError(Exception):
    pass


# ---------------------------------------------------------------------------
# Generic entry helpers shared by every crypt implementation
# ---------------------------------------------------------------------------
class Crypt(object):
    """Base class for XP3 body filters."""

    hash_after_crypt = False
    startup_tjs_not_encrypted = False
    obfuscated_index = False
    body_encrypted = True

    def init(self, archive=None, reader=None, base_dir=None):
        """Load extra parameters that live inside the archive / game folder."""

    # -- entry accessors --------------------------------------------------
    @staticmethod
    def entry_hash(entry):
        value = getattr(entry, 'adler', None)
        if value is None:
            value = getattr(entry, 'hash', 0)
        return int(value or 0) & U32

    @staticmethod
    def entry_size(entry):
        return int(getattr(entry, 'original_size', 0) or 0)

    # -- interface --------------------------------------------------------
    def decrypt(self, entry, offset, buf, pos, count):
        raise NotImplementedError

    def encrypt(self, entry, offset, buf, pos, count):
        self.decrypt(entry, offset, buf, pos, count)

    def decrypt_all(self, entry, data):
        out = bytearray(data)
        if out:
            self.decrypt(entry, 0, out, 0, len(out))
        return bytes(out)

    def encrypt_all(self, entry, data):
        out = bytearray(data)
        if out:
            self.encrypt(entry, 0, out, 0, len(out))
        return bytes(out)

    def entry_read_filter(self, entry, data):
        """Post-process a decoded body (SimpleEncrypt / LZ4 / mdf wrappers)."""
        if len(data) <= 5 or getattr(entry, 'type', None) == 'audio':
            return data
        if data[:4] == b'RIFF':
            return data
        if data[:4] == b'\x04\x22\x4d\x18':          # LZ4 frame magic
            from lz4.frame import decompress as lz4_decompress
            return lz4_decompress(data)
        if data[:3] == b'\x6d\x64\x66':              # 'mdf' + 3-byte size
            unpacked = data[4] | (data[5] << 8) | (data[6] << 16)
            import zlib
            body = zlib.decompress(data[7:])
            return body[:unpacked] if unpacked else body
        if (int.from_bytes(data[:4], 'little') & 0xFF00FFFF) == 0xFF00FEFE \
                and data[2] < 3 and data[4] == 0xFE:
            import simplecrypt
            return simplecrypt.decrypt(data)
        return data


# ---------------------------------------------------------------------------
# CxProgram
# ---------------------------------------------------------------------------
class CxProgram(object):
    """Generated key program for one seed (0..0x7f)."""

    def __init__(self, seed, control_block):
        self.initial_seed = seed & U32
        self.seed = seed & U32
        self.control_block = control_block
        self.code = []
        self.length = 0

    # -- random -----------------------------------------------------------
    def get_random(self):
        seed = self.seed
        self.seed = (1103515245 * seed + 12345) & U32
        return (self.seed ^ ((seed << 16) & U32) ^ (seed >> 16)) & U32

    def emit_random(self):
        return self.emit_u32(self.get_random())

    # -- emitter ----------------------------------------------------------
    def clear(self):
        # The advanced RNG state is kept here: a failed
        # stage retries with the *next* random values, not from the start.
        self.length = 0
        self.code = []

    def emit_nop(self, count):
        if self.length + count > LENGTH_LIMIT:
            return False
        self.length += count
        return True

    def emit(self, code, length=1):
        if self.length + length > LENGTH_LIMIT:
            return False
        self.length += length
        self.code.append(code)
        return True

    def emit_u32(self, value):
        if self.length + 4 > LENGTH_LIMIT:
            return False
        self.length += 4
        self.code.append(value & U32)
        return True

    # -- execution --------------------------------------------------------
    def execute(self, arg):
        eax = ebx = ecx = 0
        edi = arg & U32
        stack = []
        code = self.code
        i = 0
        n = len(code)
        while i < n:
            op = code[i]
            i += 1
            immed = 0
            if op & IMMED:
                if i >= n:
                    raise CxError('incomplete immediate in Cx program')
                immed = code[i]
                i += 1

            if op == NOP or op == IMMED:
                pass
            elif op == MOV_EDI_ARG:
                edi = arg & U32
            elif op == PUSH_EBX:
                stack.append(ebx)
            elif op == POP_EBX:
                ebx = stack.pop()
            elif op == PUSH_ECX:
                stack.append(ecx)
            elif op == POP_ECX:
                ecx = stack.pop()
            elif op == MOV_EBX_EAX:
                ebx = eax
            elif op == MOV_EAX_EDI:
                eax = edi
            elif op == MOV_ECX_EBX:
                ecx = ebx
            elif op == MOV_EAX_EBX:
                eax = ebx
            elif op == AND_ECX_0F:
                ecx &= 0x0F
            elif op == SHR_EBX_1:
                ebx >>= 1
            elif op == SHL_EAX_1:
                eax = (eax << 1) & U32
            elif op == SHR_EAX_CL:
                eax >>= (ecx & 0x1F)
            elif op == SHL_EAX_CL:
                eax = (eax << (ecx & 0x1F)) & U32
            elif op == OR_EAX_EBX:
                eax |= ebx
            elif op == NOT_EAX:
                eax = (~eax) & U32
            elif op == NEG_EAX:
                eax = (-eax) & U32
            elif op == DEC_EAX:
                eax = (eax - 1) & U32
            elif op == INC_EAX:
                eax = (eax + 1) & U32
            elif op == ADD_EAX_EBX:
                eax = (eax + ebx) & U32
            elif op == SUB_EAX_EBX:
                eax = (eax - ebx) & U32
            elif op == IMUL_EAX_EBX:
                eax = (eax * ebx) & U32
            elif op == ADD_EAX_IMMED:
                eax = (eax + immed) & U32
            elif op == SUB_EAX_IMMED:
                eax = (eax - immed) & U32
            elif op == AND_EBX_IMMED:
                ebx &= immed
            elif op == AND_EAX_IMMED:
                eax &= immed
            elif op == XOR_EAX_IMMED:
                eax ^= immed
            elif op == MOV_EAX_IMMED:
                eax = immed
            elif op == MOV_EAX_INDIRECT:
                if eax >= len(self.control_block):
                    raise CxError('Cx control block index out of bounds')
                eax = (~self.control_block[eax]) & U32
            elif op == RETN:
                if stack:
                    raise CxError('imbalanced Cx stack')
                return eax
            else:
                raise CxError('invalid Cx bytecode %r' % (op,))

        raise CxError('Cx program without RETN')


class CxProgramNana(CxProgram):
    """``CabbageCxCrypt`` / ``NanaCxCrypt`` program generator."""

    def __init__(self, seed, random_seed, control_block):
        CxProgram.__init__(self, seed, control_block)
        self.random_seed = random_seed & U32

    def get_random(self):
        s = (self.seed ^ ((self.seed << 17) & U32)) & U32
        s ^= ((s << 18) | (s >> 15)) & U32
        self.seed = (~s) & U32
        r = (self.random_seed ^ ((self.random_seed << 13) & U32)) & U32
        r ^= r >> 17
        self.random_seed = (r ^ ((r << 5) & U32)) & U32
        return (self.seed ^ self.random_seed) & U32


# ---------------------------------------------------------------------------
# CxEncryption
# ---------------------------------------------------------------------------
class CxEncryption(Crypt):
    """Title-specific Cx filter."""

    def __init__(self, m_mask=0, m_offset=0, PrologOrder=None,
                 OddBranchOrder=None, EvenBranchOrder=None,
                 ControlBlock=None, TpmFileName=None):
        self.m_mask = m_mask & U32
        self.m_offset = m_offset & U32
        self.PrologOrder = list(PrologOrder or [])
        self.OddBranchOrder = list(OddBranchOrder or [])
        self.EvenBranchOrder = list(EvenBranchOrder or [])
        self.ControlBlock = (None if ControlBlock is None else
                             [int(v) & U32 for v in ControlBlock])
        self.TpmFileName = TpmFileName
        self._programs = {}

    # -- readable aliases -------------------------------------------------
    mask = property(lambda self: self.m_mask)
    offset = property(lambda self: self.m_offset)
    prolog_order = property(lambda self: self.PrologOrder)
    odd_branch_order = property(lambda self: self.OddBranchOrder)
    even_branch_order = property(lambda self: self.EvenBranchOrder)
    control_block = property(lambda self: self.ControlBlock)
    tpm_filename = property(lambda self: self.TpmFileName)

    def __str__(self):
        return 'CxEncryption(0x%X, 0x%X)' % (self.m_mask, self.m_offset)

    # -- control block from a TPM plugin ----------------------------------
    TPM_SIGNATURE = b' Encryption control block'

    def load_tpm(self, data):
        """Extract the control block from a KiriKiri ``.tpm`` plugin."""
        sig = self.TPM_SIGNATURE
        end = (len(data) - 0x1000) & ~3
        pos = 0
        while pos < end:
            if data[pos:pos + len(sig)] == sig:
                values = struct.unpack_from('<1024I', data, pos)
                self.ControlBlock = [(~v) & U32 for v in values]
                return True
            pos += 4
        return False

    def init(self, archive=None, reader=None, base_dir=None):
        if self.ControlBlock is not None:
            return
        if not self.TpmFileName or not base_dir:
            raise CxError('Cx control block missing (TpmFileName=%r)'
                          % (self.TpmFileName,))
        name = self.TpmFileName.replace('\\', os.sep)
        candidates = [os.path.join(base_dir, name),
                      os.path.join(base_dir, 'plugin', os.path.basename(name))]
        for path in candidates:
            if os.path.isfile(path):
                with open(path, 'rb') as f:
                    if self.load_tpm(f.read()):
                        return
        raise CxError('cannot find control block in %s' % self.TpmFileName)

    # -- program generation ----------------------------------------------
    def new_program(self, seed):
        return CxProgram(seed, self.ControlBlock)

    def _program(self, seed):
        prog = self._programs.get(seed)
        if prog is None:
            prog = self.generate_program(seed)
            self._programs[seed] = prog
        return prog

    def generate_program(self, seed):
        program = self.new_program(seed)
        for stage in range(5, 0, -1):
            if self._emit_code(program, stage):
                return program
            program.clear()
        raise CxError('overly large Cx program')

    def _emit_code(self, program, stage):
        return (program.emit_nop(5)
                and program.emit(MOV_EDI_ARG, 4)
                and self._emit_body(program, stage)
                and program.emit_nop(5)
                and program.emit(RETN))

    def _emit_body(self, program, stage):
        if stage == 1:
            return self._emit_prolog(program)
        if not program.emit(PUSH_EBX):
            return False
        if program.get_random() & 1:
            if not self._emit_body(program, stage - 1):
                return False
        elif not self._emit_body2(program, stage - 1):
            return False
        if not program.emit(MOV_EBX_EAX, 2):
            return False
        if program.get_random() & 1:
            if not self._emit_body(program, stage - 1):
                return False
        elif not self._emit_body2(program, stage - 1):
            return False
        return self._emit_odd_branch(program) and program.emit(POP_EBX)

    def _emit_body2(self, program, stage):
        if stage == 1:
            return self._emit_prolog(program)
        if program.get_random() & 1:
            rc = self._emit_body(program, stage - 1)
        else:
            rc = self._emit_body2(program, stage - 1)
        return rc and self._emit_even_branch(program)

    def _emit_prolog(self, program):
        choice = self.PrologOrder[program.get_random() % 3]
        if choice == 2:
            return (program.emit_nop(5)
                    and program.emit(MOV_EAX_IMMED, 2)
                    and program.emit_u32(program.get_random() & 0x3FF)
                    and program.emit(MOV_EAX_INDIRECT, 0))
        if choice == 1:
            return program.emit(MOV_EAX_EDI, 2)
        return (program.emit(MOV_EAX_IMMED)
                and program.emit_random())

    def _emit_even_branch(self, program):
        choice = self.EvenBranchOrder[program.get_random() & 7]
        if choice == 0:
            return program.emit(NOT_EAX, 2)
        if choice == 1:
            return program.emit(DEC_EAX)
        if choice == 2:
            return program.emit(NEG_EAX, 2)
        if choice == 3:
            return program.emit(INC_EAX)
        if choice == 4:
            return (program.emit_nop(5)
                    and program.emit(AND_EAX_IMMED)
                    and program.emit_u32(0x3FF)
                    and program.emit(MOV_EAX_INDIRECT, 3))
        if choice == 5:
            return (program.emit(PUSH_EBX)
                    and program.emit(MOV_EBX_EAX, 2)
                    and program.emit(AND_EBX_IMMED, 2)
                    and program.emit_u32(0xAAAAAAAA)
                    and program.emit(AND_EAX_IMMED)
                    and program.emit_u32(0x55555555)
                    and program.emit(SHR_EBX_1, 2)
                    and program.emit(SHL_EAX_1, 2)
                    and program.emit(OR_EAX_EBX, 2)
                    and program.emit(POP_EBX))
        if choice == 6:
            return (program.emit(XOR_EAX_IMMED)
                    and program.emit_random())
        if program.get_random() & 1:
            rc = program.emit(ADD_EAX_IMMED)
        else:
            rc = program.emit(SUB_EAX_IMMED)
        return rc and program.emit_random()

    def _emit_odd_branch(self, program):
        choice = self.OddBranchOrder[program.get_random() % 6]
        if choice == 0:
            return (program.emit(PUSH_ECX)
                    and program.emit(MOV_ECX_EBX, 2)
                    and program.emit(AND_ECX_0F, 3)
                    and program.emit(SHR_EAX_CL, 2)
                    and program.emit(POP_ECX))
        if choice == 1:
            return (program.emit(PUSH_ECX)
                    and program.emit(MOV_ECX_EBX, 2)
                    and program.emit(AND_ECX_0F, 3)
                    and program.emit(SHL_EAX_CL, 2)
                    and program.emit(POP_ECX))
        if choice == 2:
            return program.emit(ADD_EAX_EBX, 2)
        if choice == 3:
            return (program.emit(NEG_EAX, 2)
                    and program.emit(ADD_EAX_EBX, 2))
        if choice == 4:
            return program.emit(IMUL_EAX_EBX, 3)
        return program.emit(SUB_EAX_EBX, 2)

    # -- key derivation ---------------------------------------------------
    def execute_xcode(self, key):
        seed = key & 0x7F
        program = self._program(seed)
        h = key >> 7
        ret1 = program.execute(h)
        ret2 = program.execute((~h) & U32)
        return ret1, ret2

    # field spelling
    ExecuteXCode = execute_xcode

    def base_offset(self, entry_hash):
        return (entry_hash & self.m_mask) + self.m_offset

    GetBaseOffset = base_offset

    def decode(self, key, offset, buffer, pos, count):
        ret1, ret2 = self.execute_xcode(key)
        key1 = (ret2 >> 16) & U32
        key2 = ret2 & 0xFFFF
        key3 = ret1 & 0xFF
        if key1 == key2:
            key2 += 1
        if key3 == 0:
            key3 = 1

        if offset <= key2 < offset + count:
            buffer[pos + key2 - offset] ^= (ret1 >> 16) & 0xFF
        if offset <= key1 < offset + count:
            buffer[pos + key1 - offset] ^= (ret1 >> 8) & 0xFF
        for i in range(count):
            buffer[pos + i] ^= key3

    Decode = decode

    def crypt_core(self, entry_hash, offset, data, pos, count):
        key = entry_hash & U32
        base_offset = self.base_offset(key)
        if offset < base_offset:
            base_length = min(base_offset - offset, count)
            self.decode(key, offset, data, pos, base_length)
            offset += base_length
            pos += base_length
            count -= base_length
        if count > 0:
            key = ((key >> 16) ^ key) & U32
            self.decode(key, offset, data, pos, count)

    CxDecryptCore = crypt_core

    # -- public interface -------------------------------------------------
    def decrypt(self, entry, offset, buf, pos, count):
        self.crypt_core(self.entry_hash(entry), offset, buf, pos, count)

    def encrypt(self, entry, offset, buf, pos, count):
        self.crypt_core(self.entry_hash(entry), offset, buf, pos, count)

    def decrypt_body(self, entry_hash, data):
        out = bytearray(data)
        if out:
            self.crypt_core(entry_hash, 0, out, 0, len(out))
        return bytes(out)

    def encrypt_body(self, entry_hash, data):
        return self.decrypt_body(entry_hash, data)

    # -- constructors -----------------------------------------------------
    @classmethod
    def from_dict(cls, spec):
        return cls(spec.get('m_mask', spec.get('mask', 0)),
                   spec.get('m_offset', spec.get('offset', 0)),
                   spec.get('PrologOrder', spec.get('prolog_order')),
                   spec.get('OddBranchOrder', spec.get('odd_branch_order')),
                   spec.get('EvenBranchOrder', spec.get('even_branch_order')),
                   spec.get('ControlBlock', spec.get('control_block')),
                   spec.get('TpmFileName'))


