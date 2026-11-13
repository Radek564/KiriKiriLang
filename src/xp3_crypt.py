"""Ports of every known KiriKiri XP3 crypt scheme.

This module is a 1:1 reimplementation of the XP3 filters shipped with
The known filter families:

* the simple byte filters from ``CryptAlgorithms.cs``;
* ``CzCrypt`` (cZLIB / AES), ``ChainReactionCrypt`` (list.bin), ``CroixCrypt``;
* ``MoreCrypt`` filename scrambling;
* the Cx family: ``CxEncryption``, ``SenrenCxCrypt``, ``CabbageCxCrypt``,
  ``NanaCxCrypt``, ``RiddleCxCrypt``;
* ``HxCrypt`` / ``HxCryptLite`` including the Hx special index, ChaCha index
  decryptor and Hx program generators.

Every filter works on a stored body in place::

    crypt = make_crypt(spec)
    plain = crypt.decrypt_all(entry, raw)

``entry`` may be any object exposing ``adler`` (also accepted: ``hash``),
``original_size`` and ``name`` -- :class:`xp3.Entry` fits.

Scheme parameters are not hardcoded here.  ``_work/dump_schemes.py`` exports
the scheme database into ``schemes/xp3_schemes.json``; :func:`load_scheme`
turns one entry into a ready filter.
"""
import inspect
import json
import os
import struct
import zlib

from cxcrypt import (Crypt, CxEncryption, CxProgram, CxProgramNana, CxError,
                     U32)

M64 = 0xFFFFFFFFFFFFFFFF


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def rotl32(value, shift):
    shift &= 31
    value &= U32
    if not shift:
        return value
    return ((value << shift) | (value >> (32 - shift))) & U32


def rotr32(value, shift):
    shift &= 31
    value &= U32
    if not shift:
        return value
    return ((value >> shift) | (value << (32 - shift))) & U32


def rotl8(value, shift):
    shift &= 7
    value &= 0xFF
    if not shift:
        return value
    return ((value << shift) | (value >> (8 - shift))) & 0xFF


def rotr8(value, shift):
    shift &= 7
    value &= 0xFF
    if not shift:
        return value
    return ((value >> shift) | (value << (8 - shift))) & 0xFF


def _byte(value):
    return value & 0xFF


def _u32(data, pos):
    return struct.unpack_from('<I', data, pos)[0]


def _cp932(text):
    try:
        return text.encode('cp932')
    except UnicodeEncodeError:
        return text.encode('cp932', 'replace')


# ---------------------------------------------------------------------------
# CryptAlgorithms.cs
# ---------------------------------------------------------------------------
class NoCrypt(Crypt):
    body_encrypted = False

    def decrypt(self, entry, offset, buf, pos, count):
        pass

    def encrypt(self, entry, offset, buf, pos, count):
        pass


class FateCrypt(Crypt):
    hash_after_crypt = True

    def decrypt(self, entry, offset, buf, pos, count):
        for i in range(count):
            buf[pos + i] ^= 0x36
        if offset > 0x2EA29:
            return
        if offset + count > 0x2EA29:
            buf[pos + 0x2EA29 - offset] ^= 3
        if offset > 0x13:
            return
        if offset + count > 0x13:
            buf[pos + 0x13 - offset] ^= 1

    encrypt = decrypt


class MizukakeCrypt(Crypt):
    hash_after_crypt = True

    def decrypt(self, entry, offset, buf, pos, count):
        if offset <= 0x103 < offset + count:
            buf[pos + 0x103 - offset] = (buf[pos + 0x103 - offset] - 1) & 0xFF
        for i in range(count):
            buf[pos + i] ^= 0xB6
        if offset > 0x3F82:
            return
        if offset + count > 0x3F82:
            buf[pos + 0x3F82 - offset] ^= 1
        if offset > 0x83:
            return
        if offset + count > 0x83:
            buf[pos + 0x83 - offset] ^= 3

    def encrypt(self, entry, offset, buf, pos, count):
        for i in range(count):
            buf[pos + i] ^= 0xB6
        if offset <= 0x3F82 < offset + count:
            buf[pos + 0x3F82 - offset] ^= 1
        if offset <= 0x83 < offset + count:
            buf[pos + 0x83 - offset] ^= 3
        if offset <= 0x103 < offset + count:
            buf[pos + 0x103 - offset] = (buf[pos + 0x103 - offset] + 1) & 0xFF


class HashCrypt(Crypt):
    def decrypt(self, entry, offset, buf, pos, count):
        key = self.entry_hash(entry) & 0xFF
        for i in range(count):
            buf[pos + i] ^= key

    encrypt = decrypt


class XorCrypt(Crypt):
    def __init__(self, m_key=0):
        self.m_key = m_key & 0xFF

    def decrypt(self, entry, offset, buf, pos, count):
        for i in range(count):
            buf[pos + i] ^= self.m_key

    encrypt = decrypt


class FlyingShineCrypt(Crypt):
    @staticmethod
    def _adjust(hash_value):
        shift = hash_value & 0xFF
        if shift == 0:
            shift = 0x0F
        key = (hash_value >> 8) & 0xFF
        if key == 0:
            key = 0xF0
        return key, shift

    def decrypt(self, entry, offset, buf, pos, count):
        xor, shift = self._adjust(self.entry_hash(entry))
        for i in range(count):
            buf[pos + i] = rotr8(buf[pos + i] ^ xor, shift)

    def encrypt(self, entry, offset, buf, pos, count):
        xor, shift = self._adjust(self.entry_hash(entry))
        for i in range(count):
            buf[pos + i] = rotl8(buf[pos + i], shift) ^ xor


class SeitenCrypt(Crypt):
    def decrypt(self, entry, offset, buf, pos, count):
        hash_value = self.entry_hash(entry)
        for i in range(count):
            key = (hash_value ^ ((offset + i) & U32)) & U32
            value = buf[pos + i]
            if key & 2:
                ecx = key & 0x18
                value ^= _byte((key >> ecx) | (key >> (ecx & 8)))
            if key & 4:
                value = (value + _byte(key)) & 0xFF
            if key & 8:
                value = (value - _byte(key >> (key & 0x10))) & 0xFF
            buf[pos + i] = value

    def encrypt(self, entry, offset, buf, pos, count):
        hash_value = self.entry_hash(entry)
        for i in range(count):
            key = (hash_value ^ ((offset + i) & U32)) & U32
            value = buf[pos + i]
            if key & 8:
                value = (value + _byte(key >> (key & 0x10))) & 0xFF
            if key & 4:
                value = (value - _byte(key)) & 0xFF
            if key & 2:
                ecx = key & 0x18
                value ^= _byte((key >> ecx) | (key >> (ecx & 8)))
            buf[pos + i] = value


class OkibaCrypt(Crypt):
    @staticmethod
    def _key(value):
        return ((((value & 0xFF0000) << 8) | ((value & 0xFF000000) >> 8)
                 | ((value & 0xFF00) >> 8) | ((value & 0xFF) << 8)) & U32)

    def decrypt(self, entry, offset, buf, pos, count):
        hash_value = self.entry_hash(entry)
        i = 0
        if offset < 0x65:
            key = (hash_value >> 4) & 0xFF
            limit = min(count, 0x65 - offset)
            while i < limit:
                buf[pos + i] ^= key
                offset += 1
                i += 1
        if i < count:
            offset -= 0x65
            key = self._key(hash_value)
            while i < count:
                buf[pos + i] ^= _byte(key >> (8 * (offset & 3)))
                offset += 1
                i += 1

    encrypt = decrypt


class DieselmineCrypt(Crypt):
    def decrypt(self, entry, offset, buf, pos, count):
        key = self.entry_hash(entry) & 0xFF
        for i in range(count):
            if offset < 123:
                buf[pos + i] ^= _byte(21 * key)
            elif offset < 246:
                buf[pos + i] = (buf[pos + i] + _byte(-32 * key)) & 0xFF
            elif offset < 369:
                buf[pos + i] ^= _byte(43 * key)
            else:
                buf[pos + i] = (buf[pos + i] + _byte(-54 * key)) & 0xFF
            offset += 1

    def encrypt(self, entry, offset, buf, pos, count):
        key = self.entry_hash(entry) & 0xFF
        for i in range(count):
            if offset < 123:
                buf[pos + i] ^= _byte(21 * key)
            elif offset < 246:
                buf[pos + i] = (buf[pos + i] - _byte(-32 * key)) & 0xFF
            elif offset < 369:
                buf[pos + i] ^= _byte(43 * key)
            else:
                buf[pos + i] = (buf[pos + i] - _byte(-54 * key)) & 0xFF
            offset += 1


class DameganeCrypt(Crypt):
    def decrypt(self, entry, offset, buf, pos, count):
        hash_value = self.entry_hash(entry) & 0xFF
        for i in range(count):
            buf[pos + i] ^= hash_value if (offset & 1) else (offset & 0xFF)
            offset += 1

    encrypt = decrypt


class NephriteCrypt(Crypt):
    def decrypt(self, entry, offset, buf, pos, count):
        hash_value = self.entry_hash(entry) & 0xFF
        for i in range(count):
            buf[pos + i] ^= (offset & 0xFF) if (offset & 1) else hash_value
            offset += 1

    encrypt = decrypt


class AlteredPinkCrypt(Crypt):
    KEY_TABLE = bytes([
        0x43, 0xF8, 0xAD, 0x08, 0xDF, 0xB7, 0x26, 0x44, 0xF0, 0xD9, 0xE9,
        0x24, 0x1A, 0xC1, 0xEE, 0xB4, 0x11, 0x4B, 0xE4, 0xAF, 0x01, 0x5B,
        0xF0, 0xAB, 0x6A, 0x70, 0x78, 0x84, 0xB0, 0x78, 0x4F, 0xED, 0x39,
        0x52, 0x69, 0xAF, 0xC4, 0x92, 0x2A, 0x21, 0xDE, 0xDC, 0x6E, 0x63,
        0x9D, 0x9B, 0x63, 0xE1, 0xB1, 0x94, 0x40, 0x6E, 0x3A, 0x52, 0x5A,
        0x28, 0x08, 0x4D, 0xFB, 0x22, 0x18, 0xEB, 0xBA, 0x98, 0x49, 0x77,
        0xBF, 0xAA, 0x43, 0x75, 0xF5, 0xD3, 0x83, 0x71, 0x58, 0xA4, 0xAF,
        0x1B, 0x53, 0x99, 0x8A, 0x27, 0x5B, 0xC2, 0x7F, 0x7A, 0xCD, 0x8D,
        0x33, 0x59, 0xEB, 0xA6, 0xFA, 0x7C, 0x00, 0x19, 0xC4, 0xAA, 0x24,
        0xF8, 0x84, 0xCD, 0xF7, 0x20, 0x4B, 0xAB, 0xF1, 0xD5, 0x01, 0x6F,
        0x7C, 0x91, 0x08, 0x7D, 0x8D, 0x89, 0x7C, 0x71, 0x65, 0x99, 0x9B,
        0x6F, 0x3A, 0x1C, 0x49, 0xE3, 0xAF, 0x1F, 0xC6, 0xA5, 0x79, 0xFE,
        0xAE, 0xA1, 0xCA, 0x59, 0x3C, 0xEE, 0xC1, 0x02, 0xBD, 0x2B, 0x8E,
        0xC5, 0x7D, 0x38, 0x80, 0x8F, 0x72, 0xF3, 0x86, 0x5D, 0xF4, 0x20,
        0x0A, 0x5B, 0xA0, 0xE3, 0x85, 0xB5, 0x67, 0x43, 0x96, 0xBB, 0x75,
        0x86, 0x8D, 0x7E, 0x7E, 0xE6, 0xAA, 0x18, 0x57, 0xC4, 0xAA, 0x87,
        0xDC, 0x74, 0x05, 0xAA, 0xBD, 0x5E, 0x4F, 0xA9, 0xB5, 0x5E, 0xC5,
        0xE8, 0x11, 0x6D, 0x68, 0x89, 0x17, 0x7C, 0x10, 0x05, 0xA2, 0xBA,
        0x43, 0x01, 0xD6, 0xFD, 0x26, 0x19, 0x57, 0xFA, 0x4D, 0x01, 0xB0,
        0xED, 0x3A, 0x55, 0xEB, 0x65, 0x8E, 0xD1, 0x58, 0x27, 0xAD, 0xA1,
        0x5E, 0x57, 0x3F, 0xA0, 0xEF, 0x59, 0x3E, 0xA4, 0xEB, 0x12, 0x15,
        0x60, 0xBE, 0x95, 0x61, 0x0B, 0x98, 0xF5, 0xF4, 0x12, 0x1C, 0xD8,
        0x62, 0x3F, 0xFD, 0xCF, 0x01, 0x3A, 0xE7, 0xC2, 0x19, 0x38, 0x6C,
        0xC3, 0x90, 0x3E,
    ])

    def decrypt(self, entry, offset, buf, pos, count):
        for i in range(count):
            buf[pos + i] ^= self.KEY_TABLE[(offset + i) & 0xFF]

    encrypt = decrypt


class NatsupochiCrypt(Crypt):
    def decrypt(self, entry, offset, buf, pos, count):
        key = (self.entry_hash(entry) >> 3) & 0xFF
        for i in range(count):
            buf[pos + i] ^= key

    encrypt = decrypt


class PoringSoftCrypt(Crypt):
    def decrypt(self, entry, offset, buf, pos, count):
        key = (~(self.entry_hash(entry) + 1)) & 0xFF
        for i in range(count):
            buf[pos + i] ^= key

    encrypt = decrypt


class AppliqueCrypt(Crypt):
    def decrypt(self, entry, offset, buf, pos, count):
        if offset < 5:
            skip = min(5 - offset, count)
            offset += skip
            pos += skip
            count -= skip
        key = (self.entry_hash(entry) >> 12) & 0xFF
        for i in range(count):
            buf[pos + i] ^= key

    encrypt = decrypt


class TokidokiCrypt(Crypt):
    hash_after_crypt = True

    @staticmethod
    def _parameters(entry):
        name = getattr(entry, 'name', '') or ''
        ext = os.path.splitext(name)[1]
        if ext:
            ext = ext.lower()
            ext_bin = bytearray(16)
            encoded = _cp932(ext)[:4]
            ext_bin[:len(encoded)] = encoded
            key = (~_u32(ext_bin, 0)) & U32
            if ext in '.asd.tjs.ks':
                return key, TokidokiCrypt.entry_size(entry)
            return key, min(TokidokiCrypt.entry_size(entry), 0x100)
        return U32, min(TokidokiCrypt.entry_size(entry), 0x100)

    def decrypt(self, entry, offset, buf, pos, count):
        key, limit = self._parameters(entry)
        for i in range(count):
            if offset >= limit:
                break
            buf[pos + i] ^= _byte(key >> ((offset & 3) << 3))
            offset += 1

    encrypt = decrypt


class SourireCrypt(Crypt):
    def decrypt(self, entry, offset, buf, pos, count):
        key = (self.entry_hash(entry) ^ 0xCD) & 0xFF
        for i in range(count):
            buf[pos + i] ^= key

    encrypt = decrypt


class HibikiCrypt(Crypt):
    def decrypt(self, entry, offset, buf, pos, count):
        hash_value = self.entry_hash(entry)
        key1 = (hash_value >> 5) & 0xFF
        key2 = (hash_value >> 8) & 0xFF
        for i in range(count):
            buf[pos + i] ^= key1 if ((offset & 4) or offset <= 0x64) else key2
            offset += 1

    encrypt = decrypt


class AkabeiCrypt(Crypt):
    def __init__(self, m_seed=0):
        self.m_seed = m_seed & U32

    def get_key(self, hash_value):
        hash_value = ((hash_value ^ self.m_seed) & 0x7FFFFFFF)
        hash_value = ((hash_value << 31) | hash_value) & U32
        key = []
        for _ in range(0x20):
            key.append(hash_value & 0xFF)
            hash_value = ((((hash_value & 0xFFFFFFFE) << 23)
                           | (hash_value >> 8)) & U32)
        return key

    def decrypt(self, entry, offset, buf, pos, count):
        key = self.get_key(self.entry_hash(entry))
        key_pos = offset
        for i in range(count):
            buf[pos + i] ^= key[key_pos & 0x1F]
            key_pos += 1

    encrypt = decrypt


class HaikuoCrypt(Crypt):
    def decrypt(self, entry, offset, buf, pos, count):
        hash_value = self.entry_hash(entry)
        key = (hash_value ^ (hash_value >> 8)) & 0xFF
        for i in range(count):
            buf[pos + i] ^= key

    encrypt = decrypt


class StripeCrypt(Crypt):
    def __init__(self, m_key=0):
        self.m_key = m_key & 0xFF

    def decrypt(self, entry, offset, buf, pos, count):
        for i in range(count):
            buf[pos + i] = ((buf[pos + i] ^ self.m_key) + 1) & 0xFF

    def encrypt(self, entry, offset, buf, pos, count):
        for i in range(count):
            buf[pos + i] = ((buf[pos + i] - 1) & 0xFF) ^ self.m_key


class ExaCrypt(Crypt):
    def decrypt(self, entry, offset, buf, pos, count):
        hash_value = self.entry_hash(entry)
        shift = offset % 5
        for i in range(count):
            buf[pos + i] ^= _byte(hash_value >> ((shift + i) % 5))

    encrypt = decrypt


class SmileCrypt(Crypt):
    def __init__(self, m_key_xor=0, m_first_xor=0, m_zero_xor=0):
        self.m_key_xor = m_key_xor & U32
        self.m_first_xor = m_first_xor & 0xFF
        self.m_zero_xor = m_zero_xor & 0xFF

    def decrypt(self, entry, offset, buf, pos, count):
        hash_value = self.entry_hash(entry) ^ self.m_key_xor
        key = _byte(hash_value ^ (hash_value >> 8) ^ (hash_value >> 16)
                    ^ (hash_value >> 24))
        if key == 0:
            key = self.m_zero_xor
        if offset == 0 and count > 0:
            if (hash_value & 0xFF) == 0:
                hash_value = self.m_first_xor
            buf[pos] ^= _byte(hash_value)
        for i in range(count):
            buf[pos + i] ^= key

    encrypt = decrypt


class YuzuCrypt(Crypt):
    def decrypt(self, entry, offset, buf, pos, count):
        hash_value = self.entry_hash(entry) ^ 0x1DDB6E7A
        key = _byte(hash_value ^ (hash_value >> 8) ^ (hash_value >> 16)
                    ^ (hash_value >> 24))
        if key == 0:
            key = 0xD0
        for i in range(count):
            buf[pos + i] ^= key

    encrypt = decrypt


class HighRunningCrypt(Crypt):
    def decrypt(self, entry, offset, buf, pos, count):
        key = self.entry_hash(entry) & 0xFF
        if key == 0:
            return
        for i in range(count):
            if (offset + i) % key != 0:
                buf[pos + i] ^= key

    encrypt = decrypt


class PuCaCrypt(Crypt):
    def __init__(self, HashTable=None, KeyTable=None):
        self.HashTable = HashTable
        self.KeyTable = KeyTable

    def decrypt(self, entry, offset, buf, pos, count):
        hash_value = self.entry_hash(entry)
        if self.HashTable:
            try:
                index = self.HashTable.index(hash_value)
            except ValueError:
                index = -1
            if index != -1:
                key = self.KeyTable[index] & 0xFF
                for i in range(count):
                    buf[pos + i] ^= key
                return

        hash_table = bytearray(32)
        value = hash_value
        for k in range(0, 32, 4):
            if value & 1:
                value |= 0x80000000
            else:
                value &= 0x7FFFFFFF
            struct.pack_into('<I', hash_table, k, value)
            value = (value >> 1) & 0x7FFFFFFF

        key_table = bytearray(0x400)
        for l in range(32):
            for m in range(32):
                key_table[32 * l + m] = ((~hash_table[l]) ^ hash_table[m]) & 0xFF
        for i in range(count):
            buf[pos + i] ^= key_table[(offset + i) & 0x3FF]

    encrypt = decrypt


class RhapsodyCrypt(Crypt):
    def __init__(self, FileListName=None):
        self.FileListName = FileListName

    def decrypt(self, entry, offset, buf, pos, count):
        key = bytearray(12)
        struct.pack_into('<I', key, 0, self.entry_hash(entry))
        struct.pack_into('<I', key, 4, 0x6E1DA9B2)
        struct.pack_into('<I', key, 8, 0x0040C800)
        k = offset % 12
        for i in range(count):
            buf[pos + i] ^= key[k]
            k += 1
            if k == 12:
                k = 0

    encrypt = decrypt

    @staticmethod
    def name_hash(name):
        value = 0
        for ch in name:
            c = ord(ch.lower())
            value = (0x1000193 * value ^ (c & 0xFF)) & U32
            value = (0x1000193 * value ^ ((c >> 8) & 0xFF)) & U32
        return value


class MadoCrypt(AkabeiCrypt):
    def decrypt(self, entry, offset, buf, pos, count):
        key = self.get_key(self.entry_hash(entry))
        key_pos = offset
        for i in range(count):
            buf[pos + i] ^= key[key_pos % 0x1F]
            key_pos += 1

    encrypt = decrypt


class SmxCrypt(Crypt):
    def __init__(self, Mask=5, KeySeq=None):
        self.Mask = Mask
        self.KeySeq = list(KeySeq or [])
        if len(self.KeySeq) <= self.Mask + 1:
            raise ValueError('not enough arguments for SmxCrypt')

    def _generate_key(self, hash_value):
        return [_byte(hash_value >> shift) for shift in self.KeySeq[1:]]

    def decrypt(self, entry, offset, buf, pos, count):
        hash_value = self.entry_hash(entry)
        start_key = _byte(hash_value >> self.KeySeq[0])
        key = self._generate_key(hash_value)
        for i in range(count):
            index = offset + i
            if index <= 100:
                buf[pos + i] ^= start_key
            else:
                buf[pos + i] ^= key[index & self.Mask]

    encrypt = decrypt


class FestivalCrypt(Crypt):
    def decrypt(self, entry, offset, buf, pos, count):
        key = (~(self.entry_hash(entry) >> 7)) & 0xFF
        for i in range(count):
            buf[pos + i] ^= key

    encrypt = decrypt


class PinPointCrypt(Crypt):
    @staticmethod
    def _count_bits(value):
        value = (value & 0x55) + ((value >> 1) & 0x55)
        value = (value & 0x33) + ((value >> 2) & 0x33)
        return ((value & 0xF) + ((value >> 4) & 0xF)) & 0xF

    def decrypt(self, entry, offset, buf, pos, count):
        for i in range(count):
            value = buf[pos + i]
            bits = self._count_bits(value)
            if bits:
                buf[pos + i] = rotl8(value, bits)

    def encrypt(self, entry, offset, buf, pos, count):
        for i in range(count):
            value = buf[pos + i]
            bits = self._count_bits(value)
            if bits:
                buf[pos + i] = rotr8(value, bits)


class HybridCrypt(Crypt):
    def decrypt(self, entry, offset, buf, pos, count):
        key = (self.entry_hash(entry) >> 5) & 0xFF
        for i in range(count):
            buf[pos + i] ^= key

    encrypt = decrypt


class NekoWorksCrypt(Crypt):
    def __init__(self, DefaultKey=None):
        self.DefaultKey = list(DefaultKey or [0] * 32)

    def _init_key(self, hash_value):
        hash_value &= 0x7FFFFFFF
        hash_value = ((hash_value << 31) | hash_value) & U32
        key = list(self.DefaultKey)
        for i in range(31):
            key[i] ^= hash_value & 0xFF
            hash_value = ((((hash_value & 0xFFFFFFFE) << 23)
                           | (hash_value >> 8)) & U32)
        return key

    def decrypt(self, entry, offset, buf, pos, count):
        key = self._init_key(self.entry_hash(entry))
        for i in range(count):
            buf[pos + i] ^= key[(offset + i) % 31]

    encrypt = decrypt


class NinkiSeiyuuCrypt(Crypt):
    def __init__(self, m_key1=0, m_key2=0, m_key3=0):
        self.m_key1 = m_key1 & M64
        self.m_key2 = m_key2 & M64
        self.m_key3 = m_key3 & M64
        self.m_tbl2 = self._table2(3080)
        self.m_tbl3 = self._table3(3080, self.m_key1, self.m_key2, self.m_key3)

    @staticmethod
    def _table1(seed):
        key = bytearray(32)
        value = seed & 0x7FFFFFFF
        for i in range(31):
            key[i] = value & 0xFF
            value = ((value >> 8) | ((value & 0xFF) << 23)) & U32
        return key

    @staticmethod
    def _table2(seed):
        key = bytearray(64)
        v51 = seed & 0xFFF
        v52 = ((v51 | (v51 << 13)) & M64) | ((v51 >> 19) << 32)
        v53 = (v51 | ((v52 & U32) << 13)) & U32
        v54 = ((((v52 & U32) << 7) & 0x1FFFFFFF) | (v52 >> 19)) & U32
        for i in range(61):
            v56 = v53 & 0xFF
            key[i] = v56
            v53 = (((v54 << 32) | v53) >> 8) & U32
            v54 = ((v54 >> 8) | (v56 << 21)) & U32
        return key

    @staticmethod
    def _table3(seed, key1, key2, key3):
        key = bytearray(64)
        v88 = seed & 0xFFF
        v89 = ((v88 | (v88 << 13)) & M64) | ((v88 >> 19) << 32)
        v90 = ((key1 + key2) ^ (v88 | (((v89 & U32) << 13) & U32))) & U32
        high = ((key3 & 0xFFFFFFFF00000000) | (key2 & 0xFFFFFFFF)) & M64
        left = ((key1 + high) >> 32) & 0x1FFFFFFF
        right = ((((v89 & U32) << 7) & 0x1FFFFFFF) | (v89 >> 19)) & U32
        v91 = (left ^ right) & U32
        for i in range(61):
            v93 = v90 & 0xFF
            key[i] = v93
            v90 = (((v91 << 32) | v90) >> 8) & U32
            v91 = ((v91 >> 8) | (v93 << 21)) & U32
        return key

    def decrypt(self, entry, offset, buf, pos, count):
        tbl1 = self._table1(self.entry_hash(entry))
        for i in range(count):
            index = offset + i
            buf[pos + i] ^= tbl1[index % 0x1F]
            buf[pos + i] = (buf[pos + i]
                            + (self.m_tbl2[index % 0x3D]
                               ^ self.m_tbl3[index % 0x3D])) & 0xFF

    def encrypt(self, entry, offset, buf, pos, count):
        # Decrypt only: XOR-then-add is not an involution.
        raise NotImplementedError('NinkiSeiyuuCrypt encryption is not implemented')


class SyangrilaSmartCrypt(Crypt):
    @staticmethod
    def _key(hash_value):
        return [((hash_value >> 5) & 0xFF), ((hash_value >> 5) & 0xFF),
                ((hash_value >> 7) & 0xFF), ((hash_value >> 1) & 0xFF),
                ((hash_value >> 4) & 0xFF)]

    def decrypt(self, entry, offset, buf, pos, count):
        key = self._key(self.entry_hash(entry))
        for i in range(count):
            index = offset + i
            buf[pos + i] ^= key[4] if index <= 0x64 else key[index & 3]

    encrypt = decrypt


class GensouCrypt(Crypt):
    def __init__(self, Key1=None, Key2=None):
        self.Key1 = list(Key1 or [])
        self.Key2 = list(Key2 or [])
        self.startup_tjs_not_encrypted = True

    def decrypt(self, entry, offset, buf, pos, count):
        hash_value = self.entry_hash(entry)
        off16 = offset & 0xFFFF
        key2 = self.Key2
        key1 = self.Key1
        for i in range(count):
            word = key2[off16 >> 3]
            value = (key1[(word >> ((off16 & 7) << 2)) & 0xF]
                     ^ ((word ^ hash_value) >> ((off16 & 3) << 3)))
            buf[pos + i] ^= value & 0xFF
            off16 = (off16 + 1) & 0xFFFF

    encrypt = decrypt


class Kano2Crypt(Crypt):
    def decrypt(self, entry, offset, buf, pos, count):
        hash_value = self.entry_hash(entry) & 0xFF
        i = 0
        while i < count:
            if (i + pos) % 8 == 0:
                buf[i + pos] ^= hash_value
                i += 7
            i += 1

    encrypt = decrypt


class MiburoCrypt(Crypt):
    KEY_SIZE = 29

    def _init_key(self, hash_value):
        size = self.KEY_SIZE
        hash_value &= 0x1FFFFFFF
        hash_value |= ((hash_value & 1) << size)
        key = bytearray(size)
        for i in range(size):
            key[i] = hash_value & 0xFF
            hash_value = (((hash_value >> 8)
                           | ((hash_value << 0x15) & 0xFF000000)) & U32)
        return key

    def decrypt(self, entry, offset, buf, pos, count):
        key = self._init_key(self.entry_hash(entry))
        for i in range(count):
            buf[pos + i] ^= key[(offset + i) % self.KEY_SIZE]

    encrypt = decrypt


class HoneyCombCrypt(Crypt):
    def decrypt(self, entry, offset, buf, pos, count):
        seed = (self.entry_hash(entry) ^ 0xABCD9876) & U32
        key = _byte((seed >> 24) ^ (seed >> 16) ^ (seed >> 8) ^ seed)
        if key == 0:
            key = 0xA5
        for i in range(count):
            buf[pos + i] ^= key

    encrypt = decrypt


# ---------------------------------------------------------------------------
# CzCrypt.cs -- cZLIB (AES-CBC) entry filter
# ---------------------------------------------------------------------------
class CzCrypt(Crypt):
    CZ_MAGIC = 0xA590D7FD
    CZ_IV_SEED = 0xBFBFBFBF
    CZ_HEADER_KEY = (0x9D, 0x1D, 0x9A, 0xF2)
    CZ_DEFAULT_KEY = bytes([
        0x91, 0x10, 0xFC, 0x75, 0x45, 0x8F, 0xB5, 0xE6,
        0xFE, 0xAC, 0xBA, 0x44, 0x76, 0x58, 0xC2, 0x1A,
    ])

    def entry_read_filter(self, entry, data):
        if self.entry_size(entry) <= 15 or getattr(entry, 'type', None) == 'audio':
            return data
        if len(data) < 15 or _u32(data, 0) != self.CZ_MAGIC:
            return Crypt.entry_read_filter(self, entry, data)
        type_chars = (data[4] ^ 0x11, data[5] ^ 0x7F, data[6] ^ 0x9A)
        key = type_chars[0]
        unpacked_size = self._decrypt_int(data, 7, key)
        packed_size = self._decrypt_int(data, 11, key)
        if not (0 <= packed_size < self.entry_size(entry)
                and ((packed_size - 5) & 0xF) == 0):
            return data
        if len(data) < 15 + packed_size:
            return data
        payload = data[15:15 + packed_size]
        plain = self._decrypt_data(payload)
        if type_chars[0] == ord('C'):
            plain = zlib.decompress(plain)
        return plain

    @classmethod
    def _decrypt_int(cls, data, offset, key):
        value = ((data[offset] ^ key ^ cls.CZ_HEADER_KEY[0])
                 | ((data[offset + 1] ^ key ^ cls.CZ_HEADER_KEY[1]) << 8)
                 | ((data[offset + 2] ^ key ^ cls.CZ_HEADER_KEY[2]) << 16)
                 | ((data[offset + 3] ^ key ^ cls.CZ_HEADER_KEY[3]) << 24))
        return value

    @classmethod
    def _decrypt_data(cls, data):
        padded_size = len(data) - 5
        if padded_size <= 0:
            return b''
        original_size = padded_size - (data[padded_size + 1] ^ data[padded_size])
        iv_seed = _u32(data, padded_size + 1) ^ cls.CZ_IV_SEED
        iv = cls._create_iv(iv_seed)
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        decryptor = Cipher(algorithms.AES(cls.CZ_DEFAULT_KEY),
                           modes.CBC(iv)).decryptor()
        plain = decryptor.update(bytes(data[:padded_size])) + decryptor.finalize()
        if original_size < 0:
            return b''
        return plain[:original_size]

    @staticmethod
    def _create_iv(seed):
        state = [123456789, 972436830, 524018621, seed & U32]
        iv = bytearray(16)
        for i in range(16):
            a = state[3]
            b = (state[0] ^ ((state[0] << 11) & U32)) & U32
            state[0] = state[1]
            state[1] = state[2]
            state[2] = a
            state[3] = (b ^ a ^ ((b ^ (a >> 11)) >> 8)) & U32
            iv[i] = state[3] & 0xFF
        return bytes(iv)


class KissCrypt(CzCrypt):
    def decrypt(self, entry, offset, buf, pos, count):
        hash_value = self.entry_hash(entry)
        key = (hash_value ^ (hash_value >> 19) ^ 0x4A9EEFF0) & U32
        i = 0
        while ((offset + i) & 0xF) != 0:
            i += 1
        while i < count:
            buf[pos + i] ^= _byte(key ^ (offset + i))
            i += 0x10

    encrypt = decrypt


# ---------------------------------------------------------------------------
# ChainReactionCrypt.cs
# ---------------------------------------------------------------------------
class ChainReactionCrypt(Crypt):
    def __init__(self, m_list_bin='plugin/list.bin'):
        self.m_list_bin = m_list_bin
        self.encryption_threshold_map = None

    # -- list.bin loading -------------------------------------------------
    def init(self, archive=None, reader=None, base_dir=None):
        data = None
        if reader is not None:
            data = reader(self.m_list_bin)
        elif archive is not None and hasattr(archive, 'read_named'):
            data = archive.read_named(self.m_list_bin)
        if not data or len(data) <= 0x30:
            return
        self.init_list(data)

    def init_list(self, data):
        if not data.startswith(b'"\r\n'):
            for _ in range(3):
                data = self._decode_list(data)
                if data is None:
                    return
        self.encryption_threshold_map = {}
        self._parse_list(data)

    def _parse_list(self, data):
        text = data.decode('latin-1')
        for line in text.splitlines():
            if not line or line[0] != '0':
                continue
            parts = line.split(',')
            if len(parts) > 1:
                self.encryption_threshold_map[self._uint(parts[0])] = \
                    self._uint(parts[1])

    @staticmethod
    def _uint(text):
        text = text.strip()
        if text.lower().startswith('0x'):
            return int(text, 16) & U32
        return int(text, 10) & U32

    @classmethod
    def _decode_list(cls, data):
        if len(data) < 0x30:
            return None
        header = cls._decode_dpd(data, 0, 0x30)
        packed_size = struct.unpack_from('<i', header, 0x0C)[0]
        unpacked_size = struct.unpack_from('<i', header, 0x10)[0]
        if packed_size <= 0 or packed_size > len(data) - 0x30:
            return None
        magic = header[:4]
        if magic == b'DPDC':
            return cls._decode_dpd(data, 0x30, packed_size)
        if magic == b'SZLC':
            return cls._decode_lzss(data[0x30:0x30 + packed_size],
                                    packed_size, unpacked_size)
        if magic == b'ELRC':
            unpacked = bytearray(unpacked_size)
            min_repeat = struct.unpack_from('<i', header, 0x1C)[0]
            cls._decode_rle(data, 0x30, packed_size, unpacked, min_repeat)
            return bytes(unpacked)
        return None

    @staticmethod
    def _decode_dpd(src, offset, length):
        dst = bytearray(length)
        if length < 8:
            return bytes(dst)
        tail = length & 3
        if tail:
            dst[length - tail:length] = src[offset + length - tail:
                                             offset + length]
        words = length // 4
        first = 0
        for i in range(words - 1):
            value = _u32(src, offset + 4 * i) ^ _u32(src, offset + 4 * i + 4)
            struct.pack_into('<I', dst, 4 * i, value)
            if i == 0:
                first = value
        last = first ^ _u32(src, offset + 4 * (words - 1))
        struct.pack_into('<I', dst, 4 * (words - 1), last)
        return bytes(dst)

    @staticmethod
    def _decode_rle(src, offset, length, dst, min_repeat):
        pos = offset
        end = offset + length
        out = 0
        while pos < end:
            value = src[pos]
            pos += 1
            repeat = 1
            while repeat < min_repeat and pos < end and src[pos] == value:
                repeat += 1
                pos += 1
            if repeat == min_repeat:
                ctl = src[pos]
                pos += 1
                if ctl > 0x7F:
                    repeat += src[pos] + ((ctl & 0x7F) << 8) + 0x80
                    pos += 1
                else:
                    repeat += ctl
            for _ in range(repeat):
                if out < len(dst):
                    dst[out] = value
                out += 1

    @staticmethod
    def _decode_lzss(src, packed_size, unpacked_size):
        out = bytearray(unpacked_size)
        frame = bytearray(0x1000)
        frame_pos = 0xFEE
        mask = 0xFFF
        dst = 0
        pos = 0
        remaining = packed_size
        while remaining > 0:
            ctl = src[pos]
            pos += 1
            remaining -= 1
            bit = 1
            while remaining > 0 and bit != 0x100:
                if dst >= unpacked_size:
                    return bytes(out)
                if ctl & bit:
                    if pos >= len(src):
                        return bytes(out)
                    value = src[pos]
                    pos += 1
                    remaining -= 1
                    frame[frame_pos] = value
                    frame_pos = (frame_pos + 1) & mask
                    out[dst] = value
                    dst += 1
                else:
                    if remaining < 2 or pos + 1 >= len(src):
                        return bytes(out)
                    lo = src[pos]
                    hi = src[pos + 1]
                    pos += 2
                    remaining -= 2
                    offset = ((hi & 0xF0) << 4) | lo
                    for _ in range(3 + (hi & 0xF)):
                        if dst >= unpacked_size:
                            break
                        value = frame[offset]
                        offset = (offset + 1) & mask
                        frame[frame_pos] = value
                        frame_pos = (frame_pos + 1) & mask
                        out[dst] = value
                        dst += 1
                bit <<= 1
        return bytes(out)

    # -- body filter ------------------------------------------------------
    def get_encryption_limit(self, entry):
        if self.encryption_threshold_map is not None:
            value = self.encryption_threshold_map.get(self.entry_hash(entry))
            if value is not None:
                return value
        return 0x200

    def decrypt(self, entry, offset, buf, pos, count):
        limit = self.get_encryption_limit(entry)
        if offset >= limit:
            return
        count = min(limit - offset, count)
        key = self.entry_hash(entry)
        for i in range(count):
            index = offset + i
            buf[pos + i] ^= _byte(index ^ _byte(key >> ((index & 3) << 3)))

    def encrypt(self, entry, offset, buf, pos, count):
        raise NotImplementedError('ChainReactionCrypt encryption requires '
                                  'list.bin regeneration')


class HachukanoCrypt(ChainReactionCrypt):
    def __init__(self):
        ChainReactionCrypt.__init__(self, 'plugins/list.txt')
        self.startup_tjs_not_encrypted = True

    def get_encryption_limit(self, entry):
        limit = ChainReactionCrypt.get_encryption_limit(self, entry)
        if limit == 0:
            return 0
        if limit == 1:
            return 0x100
        if limit == 2:
            return 0x200
        if limit == 3:
            return self.entry_size(entry)
        return limit


class ChocolatCrypt(ChainReactionCrypt):
    def __init__(self):
        ChainReactionCrypt.__init__(self, 'plugins/list.txt')
        self.startup_tjs_not_encrypted = True

    def get_encryption_limit(self, entry):
        limit = ChainReactionCrypt.get_encryption_limit(self, entry)
        if limit == 0:
            return 0
        if limit == 2:
            return self.entry_size(entry)
        return 0x100


class XanaduCrypt(ChainReactionCrypt):
    def __init__(self):
        ChainReactionCrypt.__init__(self, 'plugins/list.txt')
        self.startup_tjs_not_encrypted = True

    def init(self, archive=None, reader=None, base_dir=None):
        data = None
        if reader is not None:
            data = reader('list2.txt') or reader('plugins/list.txt')
        elif archive is not None and hasattr(archive, 'read_named'):
            data = archive.read_named('list2.txt') or archive.read_named(
                'plugins/list.txt')
        if data:
            self.init_list(data)

    def get_encryption_limit(self, entry):
        limit = ChainReactionCrypt.get_encryption_limit(self, entry)
        if limit == 0:
            return 0
        if limit == 2:
            return self.entry_size(entry)
        return 0x100

    def decrypt(self, entry, offset, buf, pos, count):
        limit = self.get_encryption_limit(entry)
        if offset >= limit:
            return
        count = min(limit - offset, count)
        key = (self.entry_hash(entry) ^ (~0x03020100 & U32)) & U32
        extra = ((offset & 0xFF) >> 2) << 2
        for i in range(count):
            index = offset + i
            if (index & 0xFF) == 0:
                extra = 0
            elif (index & 3) == 0:
                extra = (extra + 4) & 0xFF
            buf[pos + i] ^= _byte((key >> ((index & 3) << 3)) ^ extra)

    def encrypt(self, entry, offset, buf, pos, count):
        self.decrypt(entry, offset, buf, pos, count)


class SisMikoCrypt(XanaduCrypt):
    def decrypt(self, entry, offset, buf, pos, count):
        limit = self.get_encryption_limit(entry)
        if offset >= limit:
            return
        count = min(limit - offset, count)
        key = (~rotr32(self.entry_hash(entry), 16)) & U32
        for i in range(count):
            buf[pos + i] ^= _byte(key >> (((offset + i) & 3) << 3))

    def encrypt(self, entry, offset, buf, pos, count):
        self.decrypt(entry, offset, buf, pos, count)


# ---------------------------------------------------------------------------
# CroixCrypt.cs
# ---------------------------------------------------------------------------
class CroixCrypt(Crypt):
    SIGDATA_CHECKSUM = 0x6C04B9AB66EF2EF0
    V160 = 0xA5665A5F061EC576
    CRC = 0xE51804DAE70D133E

    _DWORD_TABLE = [
        0x00000000, 0x09073096, 0x120E612C, 0x1B0951BA, 0xFF6DC419, 0xF66AF48F,
        0xED63A535, 0xE46495A3, 0xFEDB8832, 0xF7DCB8A4, 0xECD5E91E, 0xE5D2D988,
        0x01B64C2B, 0x08B17CBD, 0x13B82D07, 0x1ABF1D91, 0xFDB71064, 0xF4B020F2,
        0xEFB97148, 0xE6BE41DE, 0x02DAD47D, 0x0BDDE4EB, 0x10D4B551, 0x19D385C7,
        0x036C9856, 0x0A6BA8C0, 0x1162F97A, 0x1865C9EC, 0xFC015C4F, 0xF5066CD9,
        0xEE0F3D63, 0xE7080DF5, 0xFB6E20C8, 0xF269105E, 0xE96041E4, 0xE0677172,
        0x0403E4D1, 0x0D04D447, 0x160D85FD, 0x1F0AB56B, 0x05B5A8FA, 0x0CB2986C,
        0x17BBC9D6, 0x1EBCF940, 0xFAD86CE3, 0xF3DF5C75, 0xE8D60DCF, 0xE1D13D59,
        0x06D930AC, 0x0FDE003A, 0x14D75180, 0x1DD06116, 0xF9B4F4B5, 0xF0B3C423,
        0xEBBA9599, 0xE2BDA50F, 0xF802B89E, 0xF1058808, 0xEA0CD9B2, 0xE30BE924,
        0x076F7C87, 0x0E684C11, 0x15611DAB, 0x1C662D3D, 0xF6DC4190, 0xFFDB7106,
        0xE4D220BC, 0xEDD5102A, 0x09B18589, 0x00B6B51F, 0x1BBFE4A5, 0x12B8D433,
        0x0807C9A2, 0x0100F934, 0x1A09A88E, 0x130E9818, 0xF76A0DBB, 0xFE6D3D2D,
        0xE5646C97, 0xEC635C01, 0x0B6B51F4, 0x026C6162, 0x196530D8, 0x1062004E,
        0xF40695ED, 0xFD01A57B, 0xE608F4C1, 0xEF0FC457, 0xF5B0D9C6, 0xFCB7E950,
        0xE7BEB8EA, 0xEEB9887C, 0x0ADD1DDF, 0x03DA2D49, 0x18D37CF3, 0x11D44C65,
        0x0DB26158, 0x04B551CE, 0x1FBC0074, 0x16BB30E2, 0xF2DFA541, 0xFBD895D7,
        0xE0D1C46D, 0xE9D6F4FB, 0xF369E96A, 0xFA6ED9FC, 0xE1678846, 0xE860B8D0,
        0x0C042D73, 0x05031DE5, 0x1E0A4C5F, 0x170D7CC9, 0xF005713C, 0xF90241AA,
        0xE20B1010, 0xEB0C2086, 0x0F68B525, 0x066F85B3, 0x1D66D409, 0x1461E49F,
        0x0EDEF90E, 0x07D9C998, 0x1CD09822, 0x15D7A8B4, 0xF1B33D17, 0xF8B40D81,
        0xE3BD5C3B, 0xEABA6CAD, 0xEDB88320, 0xE4BFB3B6, 0xFFB6E20C, 0xF6B1D29A,
        0x12D54739, 0x1BD277AF, 0x00DB2615, 0x09DC1683, 0x13630B12, 0x1A643B84,
        0x016D6A3E, 0x086A5AA8, 0xEC0ECF0B, 0xE509FF9D, 0xFE00AE27, 0xF7079EB1,
        0x100F9344, 0x1908A3D2, 0x0201F268, 0x0B06C2FE, 0xEF62575D, 0xE66567CB,
        0xFD6C3671, 0xF46B06E7, 0xEED41B76, 0xE7D32BE0, 0xFCDA7A5A, 0xF5DD4ACC,
        0x11B9DF6F, 0x18BEEFF9, 0x03B7BE43, 0x0AB08ED5, 0x16D6A3E8, 0x1FD1937E,
        0x04D8C2C4, 0x0DDFF252, 0xE9BB67F1, 0xE0BC5767, 0xFBB506DD, 0xF2B2364B,
        0xE80D2BDA, 0xE10A1B4C, 0xFA034AF6, 0xF3047A60, 0x1760EFC3, 0x1E67DF55,
        0x056E8EEF, 0x0C69BE79, 0xEB61B38C, 0xE266831A, 0xF96FD2A0, 0xF068E236,
        0x140C7795, 0x1D0B4703, 0x060216B9, 0x0F05262F, 0x15BA3BBE, 0x1CBD0B28,
        0x07B45A92, 0x0EB36A04, 0xEAD7FFA7, 0xE3D0CF31, 0xF8D99E8B, 0xF1DEAE1D,
        0x1B64C2B0, 0x1263F226, 0x096AA39C, 0x006D930A, 0xE40906A9, 0xED0E363F,
        0xF6076785, 0xFF005713, 0xE5BF4A82, 0xECB87A14, 0xF7B12BAE, 0xFEB61B38,
        0x1AD28E9B, 0x13D5BE0D, 0x08DCEFB7, 0x01DBDF21, 0xE6D3D2D4, 0xEFD4E242,
        0xF4DDB3F8, 0xFDDA836E, 0x19BE16CD, 0x10B9265B, 0x0BB077E1, 0x02B74777,
        0x18085AE6, 0x110F6A70, 0x0A063BCA, 0x03010B5C, 0xE7659EFF, 0xEE62AE69,
        0xF56BFFD3, 0xFC6CCF45, 0xE00AE278, 0xE90DD2EE, 0xF2048354, 0xFB03B3C2,
        0x1F672661, 0x166016F7, 0x0D69474D, 0x046E77DB, 0x1ED16A4A, 0x17D65ADC,
        0x0CDF0B66, 0x05D83BF0, 0xE1BCAE53, 0xE8BB9EC5, 0xF3B2CF7F, 0xFAB5FFE9,
        0x1DBDF21C, 0x14BAC28A, 0x0FB39330, 0x06B4A3A6, 0xE2D03605, 0xEBD70693,
        0xF0DE5729, 0xF9D967BF, 0xE3667A2E, 0xEA614AB8, 0xF1681B02, 0xF86F2B94,
        0x1C0BBE37, 0x150C8EA1, 0x0E05DF1B, 0x0702EF8D,
    ]

    @classmethod
    def _table(cls):
        value = (cls.V160 + cls.CRC) & M64
        v58 = value & U32
        v59 = (value >> 32) & 0x1FFFFFFF
        key = bytearray(0x3D)
        for i in range(0x3D):
            v61 = v58 & 0xFF
            key[i] = v61
            v58 = ((v59 << 24) | (v58 >> 8)) & U32
            v59 = ((v59 >> 8) | ((v61 << 21) & U32)) & U32
        return bytes(key)

    TABLE = None

    @staticmethod
    def _mix(value):
        v21 = value
        left = (((v21 & 0xF) | (((v21 & 0xFFFFFFF0) << 14) & U32)) << 3) & U32
        p1 = (v21 & 0xE0000) | ((v21 >> 1) & 0x7F000000)
        p2 = (v21 & 0x1F00000) | (p1 >> 13)
        p3 = (v21 & 0x18000) | (p2 >> 3)
        return (left | (p3 >> 1)) & U32

    @classmethod
    def _scramble(cls, v16):
        a = ((v16 & 0x78) | ((v16 >> 14) & 0x3FF80)) >> 3
        b1 = (v16 & 7) | ((2 * (v16 & 0xFFFFFF80)) & U32)
        b2 = (v16 & 0x1F0000) | ((b1 << 13) & U32)
        b3 = (v16 & 0xC000) | ((b2 << 3) & U32)
        value = (~(a | ((b3 << 1) & U32))) & U32
        table = cls.TABLE if cls.TABLE is not None else cls._table()
        for i in range(0x3D):
            value = cls._DWORD_TABLE[(value ^ table[i]) & 0xFF] ^ (value >> 8)
        return cls._mix((~value) & U32)

    def decrypt(self, entry, offset, buf, pos, count):
        table = self.TABLE if self.TABLE is not None else self._table()
        v16 = 0x6EDC44A8
        v17 = 0x139E
        v19 = 0x4B93
        for i in range(0x3D):
            v17 = (v17 + table[i]) & U32
            v19 = (v19 + v17) & U32
        v17 %= 0xFFEF
        v19 %= 0xFFEF
        v21 = (v17 ^ (v19 << 16)) & U32
        v22 = self._mix(v21)
        v25 = self._scramble(v16)
        v29 = 0x5793CE00
        v71 = (v22 ^ ((v29 << 21) & U32)) & U32
        v75 = (v25 ^ (v29 >> 11)) & U32
        v33 = (self.SIGDATA_CHECKSUM ^ 0x5793CE00 ^ v71
               ^ ((v75 << 32) & M64)) & M64
        v55 = (v33 >> 17) & U32
        hash_value = self.entry_hash(entry)

        key_hash = (hash_value ^ v55 ^ 0x15C3F972) & U32
        for i in range(count):
            shift = ((offset + i) & 3) << 3
            value = (buf[pos + i] ^ _byte(key_hash >> shift)) & 0xFF
            value = (value - _byte(0xE3AD9ACB >> shift)) & 0xFF
            value ^= _byte(offset >> (((offset + i) & 7) << 3))
            value = (value + _byte(0xFECAB9F2 >> shift)) & 0xFF
            buf[pos + i] = value

        key_hash = (hash_value ^ 0x27D3BCA1) & U32
        for i in range(count):
            shift = ((offset + i) & 3) << 3
            value = (buf[pos + i] ^ _byte(key_hash >> shift)) & 0xFF
            value = (value - _byte(0xE3779ACB >> shift)) & 0xFF
            value ^= _byte(offset >> (((offset + i) & 7) << 3))
            value = (value + _byte(0xFDCAB972 >> shift)) & 0xFF
            buf[pos + i] = value

        value = hash_value & 0x7FFFFFFF
        v83 = bytearray(0x1F)
        for i in range(0x1F):
            v83[i] = value & 0xFF
            value = (v55 ^ (((value >> 8) | ((value & 0xFF) << 23))
                            & U32)) & U32

        v58 = v59 = 0
        v84 = bytearray(0x3D)
        for i in range(0x3D):
            v61 = v58 & 0xFF
            v84[i] = v61
            v58 = (v55 ^ (((v59 << 24) | (v58 >> 8)) & U32)) & U32
            v59 = ((v59 >> 8) | ((v61 << 21) & U32)) & U32

        for i in range(count):
            index = offset + i
            buf[pos + i] ^= v83[index % 0x1F]
            buf[pos + i] = (buf[pos + i]
                            + (v55 ^ v84[index % 0x3D]
                               ^ table[index % 0x3D])) & 0xFF

    encrypt = decrypt


CroixCrypt.TABLE = CroixCrypt._table()


# ---------------------------------------------------------------------------
# MoreCrypt.cs -- PureMore filename scrambling
# ---------------------------------------------------------------------------
class PureMoreCrypt(NoCrypt):
    DEFAULT_CHAR_MAP = (
        '0123456789abcdefghijklmnopqrstuvwxyz'
        '０１２３４５６７８９ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ'
        'ぁあぃいぅうぇえぉおかがきぎくぐけげこごさざしじすずせぜそぞただ'
        'ちぢっつづてでとどなにぬねのはばぱひびぴふぶぷへべぺほぼぽまみむ'
        'めもゃやゅゆょよらりるれろゎわゐゑをんァアィイゥウェエォオカガキ'
        'ギクグケゲコゴサザシジスズセゼソゾタダチヂッツヅテデトドナニヌネ'
        'ノハバパヒビピフブプヘベペホボポマミムメモャヤュユョヨラリルレロ'
        'ヮワヰヱヲンヴヵヶ零一二三四五六七八九十百千万億'
    )

    def __init__(self, FileListName=None, CharMap=None, LayerNameSuffix=None):
        import hashlib
        self.FileListName = FileListName
        self.CharMap = CharMap or self.DEFAULT_CHAR_MAP
        self.LayerNameSuffix = LayerNameSuffix or ''
        self._sha256 = hashlib.sha256
        self._known = None

    def read_names(self, lines):
        self._known = {}
        for line in lines:
            parts = line.split(',', 1)
            name = parts[0]
            ext = name.rfind('.')
            if ext != -1:
                name = name[:ext]
            if len(parts) == 2:
                name += self.LayerNameSuffix
            digest = self._sha256(name.encode('utf-16le')).digest()
            mapped = ''.join(self.CharMap[b] for b in digest) + '.tlg'
            self._known[mapped] = parts[0]

    def map_name(self, name):
        if not self._known or len(name) != 36 or not name.endswith('.tlg'):
            return name
        return self._known.get(name, name)


# ---------------------------------------------------------------------------
# HxCrypt.cs
# ---------------------------------------------------------------------------
class HxIndexKey(object):
    def __init__(self, Key1=None, Key2=None):
        self.Key1 = Key1
        self.Key2 = Key2


class HxEntry(object):
    __slots__ = ('path', 'name', 'id', 'key', 'filter')

    def __init__(self):
        self.path = None
        self.name = None
        self.id = 0
        self.key = 0
        self.filter = None


class HxFilterKey(object):
    __slots__ = ('key', 'split_position', 'header_key', 'has_header_key', 'flag')

    def __init__(self):
        self.key = [0, 0]
        self.split_position = 0
        self.header_key = bytearray(16)
        self.has_header_key = False
        self.flag = False


class HxHeaderKey(object):
    __slots__ = ('position', 'key', 'key_ptr', 'length')

    def __init__(self):
        self.position = 0
        self.key = None
        self.key_ptr = 0
        self.length = 0


class HxFilterSpan(object):
    __slots__ = ('position', 'buffer', 'buffer_ptr', 'length')

    def __init__(self, position=0, buffer=None, buffer_ptr=0, length=0):
        self.position = position
        self.buffer = buffer
        self.buffer_ptr = buffer_ptr
        self.length = length

    def decrypt_header(self, key):
        if self.buffer is None or self.length == 0 or key.key is None \
                or key.length == 0:
            return
        start = self.position
        end = start + self.length
        if start <= key.position:
            start = key.position
        if end >= key.position + key.length:
            end = key.position + key.length
        if start >= end:
            return
        ptr = self.buffer_ptr + start - self.position
        key_ptr = start - key.position
        for i in range(end - start):
            self.buffer[ptr + i] ^= key.key[key_ptr + i]

    def split(self, split_position):
        """Return (flags, [span0, span1]) mirroring the C# return value."""
        if self.buffer is None or self.length == 0:
            return 0, [None, None]
        if split_position > self.position:
            if split_position < self.position + self.length:
                first = HxFilterSpan(self.position, self.buffer,
                                     self.buffer_ptr,
                                     split_position - self.position)
                second = HxFilterSpan(split_position, self.buffer,
                                      self.buffer_ptr + first.length,
                                      self.length - first.length)
                return 3, [first, second]
            return 1, [self, None]
        return 2, [None, self]

    def first_decrypt(self, key):
        if self.buffer is None or self.length == 0:
            return
        buf = struct.pack('<I', key & U32)
        for i in range(self.length):
            self.buffer[self.buffer_ptr + i] ^= buf[(self.position + i) & 3]


class HxFilterSpanDecryptor(object):
    def __init__(self, key, flag):
        self.decrypt_key = (key >> 8) & 0xFF
        self.decrypt_key |= (key >> 8) & 0xFF00
        self.span_position = [(key >> 48) & 0xFFFF, (key >> 32) & 0xFFFF]
        self.first_decrypt_key = key & 0xFF

        if self.span_position[0] == self.span_position[1]:
            self.span_position[1] += 1

        if flag:
            self.decrypt_key = 0
        if not flag and self.first_decrypt_key == 0:
            self.first_decrypt_key = 0xA5
        self.first_decrypt_key = (self.first_decrypt_key * 0x1010101) & U32

    def decrypt(self, span):
        span.first_decrypt(self.first_decrypt_key)
        key1 = self.decrypt_key & 0xFF
        key2 = (self.decrypt_key >> 8) & 0xFF
        if key1:
            pos = self.span_position[0]
            if span.position <= pos < span.position + span.length:
                span.buffer[span.buffer_ptr + pos - span.position] ^= key1
        if key2:
            pos = self.span_position[1]
            if span.position <= pos < span.position + span.length:
                span.buffer[span.buffer_ptr + pos - span.position] ^= key2


class HxFilter(object):
    def __init__(self, key):
        self.span = [HxFilterSpanDecryptor(key.key[0], key.flag),
                     HxFilterSpanDecryptor(key.key[1], key.flag)]
        self.split_position = key.split_position
        self.header_key = HxHeaderKey()
        if key.has_header_key:
            self.header_key.key = key.header_key
            self.header_key.length = 16

    def decrypt(self, position, buffer, buffer_ptr, length):
        span = HxFilterSpan(position, buffer, buffer_ptr, length)
        if span.position < self.header_key.position + self.header_key.length:
            span.decrypt_header(self.header_key)
        flags, sub = span.split(self.split_position)
        if flags & 1:
            self.span[0].decrypt(sub[0])
        if flags & 2:
            self.span[1].decrypt(sub[1])


class HxSplittableRandom(object):
    def __init__(self, seed):
        self.seed = seed & M64

    def next(self):
        self.seed = (self.seed + 0x9E3779B97F4A7C15) & M64
        z = self.seed
        z ^= z >> 30
        z = (z * 0xBF58476D1CE4E5B9) & M64
        z ^= z >> 27
        z = (z * 0x94D049BB133111EB) & M64
        z ^= z >> 31
        return z


def _split64(value):
    return value & U32, (value >> 32) & U32


def _join64(lo, hi):
    return ((hi & U32) << 32) | (lo & U32)


class HxProgram(CxProgram):
    def __init__(self, seed, control_block, random_method):
        CxProgram.__init__(self, seed, control_block)
        self.random_method = random_method
        s = (seed & U32) | (((~seed) & U32) << 32)
        rng = HxSplittableRandom(s)
        self.seed0 = rng.next()
        self.seed1 = rng.next()

    def _get_old_random(self):
        a = self.seed0
        b = self.seed1
        a_lo, a_hi = _split64(a)
        b_lo, b_hi = _split64(b)
        c_lo = a_hi ^ b_hi
        c_hi = a_lo ^ b_lo
        c = _join64(c_lo, c_hi)
        e = _join64(c_hi, c_lo)

        t = ((c_hi << 21) & M64)
        t ^= a >> 15
        t ^= c_hi
        lo = t & U32

        t = (a_hi >> 15) & M64
        t |= (a_lo << 17) & M64
        t ^= e >> 11
        t ^= c_lo
        hi = t & U32

        self.seed0 = _join64(lo, hi)
        self.seed1 = ((e >> 4) & M64) | (((c >> 4) & U32) << 32)

        d = (a + b) & M64
        t = (d << 17) & M64
        t |= (d >> 32) >> 15
        t = (t + a) & M64
        return t

    def _get_new_random(self):
        a = self.seed0
        b = self.seed1
        a_lo, a_hi = _split64(a)
        b_lo, b_hi = _split64(b)
        c_lo = a_lo ^ b_lo
        c_hi = a_hi ^ b_hi
        c = _join64(c_lo, c_hi)

        t = (a_lo << 24) & M64
        t |= a_hi >> 8
        t ^= (c_lo << 16) & M64
        t ^= c_lo
        lo = t & U32

        t = c >> 16
        t ^= a >> 8
        t ^= c_hi
        hi = t & U32

        self.seed0 = _join64(lo, hi)

        t = c_hi >> 27
        t |= (c_lo << 5) & M64
        m1_hi = t & U32
        m1_lo = (c >> 27) & U32
        self.seed1 = _join64(m1_lo, m1_hi)

        d = (5 * a) & M64
        t = d >> 32 >> 25
        t |= (d << 7) & M64
        t = (t * 9) & M64
        return t

    def get_random(self):
        if self.random_method == 0:
            return self._get_old_random() & U32
        return self._get_new_random() & U32


class HxProgramLite(CxProgram):
    def __init__(self, seed, control_block, random_method):
        CxProgram.__init__(self, seed, control_block)
        self.random_type = random_method
        block = [0] * 0x270
        block[0] = seed & U32
        for i in range(1, 0x270):
            previous = block[i - 1]
            block[i] = (((previous ^ (previous >> 0x1E)) * 0x6C078965) + i) & U32
        self.random_block = block
        self.block_position = 0x270

    def get_random(self):
        if self.random_type == 0:
            return CxProgram.get_random(self)
        return self._get_random_new()

    def _get_random_new(self):
        if self.block_position == len(self.random_block):
            self._transform_block()
        s0 = self.random_block[self.block_position]
        s1 = (s0 >> 11) ^ s0
        s2 = (((s1 & 0xFF3A58AD) << 7) ^ s1) & U32
        s3 = (((s2 & 0xFFFFDF8C) << 15) ^ s2) & U32
        s4 = (s3 >> 18) ^ s3
        self.block_position += 1
        return s4 & U32

    def _transform_block(self):
        self.block_position = 0
        block = self.random_block
        for i in range(0xE3):
            s0 = 0x9908B0DF if (block[i + 1] & 1) else 0
            s1 = ((((block[i] ^ block[i + 1]) & 0x7FFFFFFE)
                   ^ block[i]) >> 1)
            block[i] = (s0 ^ s1 ^ block[i + 0x18D]) & U32
        for i in range(0x18C):
            s0 = 0x9908B0DF if (block[i + 1 + 0xE3] & 1) else 0
            s1 = ((((block[i + 0xE3] ^ block[i + 1 + 0xE3])
                    & 0x7FFFFFFE) ^ block[i + 0xE3]) >> 1)
            block[i + 0xE3] = (s0 ^ s1 ^ block[i]) & U32
        s0 = 0x9908B0DF if (block[0] & 1) else 0
        s1 = ((((block[0x26F] ^ block[0]) & 0x7FFFFFFE)
               ^ block[0x26F]) >> 1)
        block[0x26F] = (s0 ^ s1 ^ block[0x18C]) & U32


class HxChachaDecryptor(object):
    def __init__(self, key, nonce, seed):
        self.state = bytearray(64)
        self.state[0:16] = b'expand 32-byte k'
        self.state[16:32] = key[0:16]
        self.state[32:48] = key[16:32]
        struct.pack_into('<I', self.state, 48, seed[0] & U32)
        struct.pack_into('<I', self.state, 52, seed[1] & U32)
        self.state[56:64] = nonce[0:8]

    @staticmethod
    def _transform(src):
        z = list(struct.unpack_from('<16I', src, 0))

        def quarter(a, b, c, d):
            z[a] = (z[a] + z[b]) & U32
            z[d] = rotl32(z[d] ^ z[a], 16)
            z[c] = (z[c] + z[d]) & U32
            z[b] = rotl32(z[b] ^ z[c], 12)
            z[a] = (z[a] + z[b]) & U32
            z[d] = rotl32(z[d] ^ z[a], 8)
            z[c] = (z[c] + z[d]) & U32
            z[b] = rotl32(z[b] ^ z[c], 7)

        for _ in range(10):
            quarter(0, 4, 8, 12)
            quarter(1, 5, 9, 13)
            quarter(2, 6, 10, 14)
            quarter(3, 7, 11, 15)
            quarter(0, 5, 10, 15)
            quarter(1, 6, 11, 12)
            quarter(2, 7, 8, 13)
            quarter(3, 4, 9, 14)
        return struct.pack('<16I', *z)

    def decrypt(self, data, input_pos, output, output_pos, length):
        num_block = length // 64
        for _ in range(num_block):
            transformed = self._transform(self.state)
            for j in range(0, 64, 4):
                value = (_u32(data, input_pos + j)
                         + _u32(transformed, j)) & U32
                value ^= _u32(self.state, j)
                struct.pack_into('<I', output, output_pos + j, value)
            input_pos += 64
            output_pos += 64
            k = 0
            while True:
                self.state[48 + k] = (self.state[48 + k] + 1) & 0xFF
                if self.state[48 + k] != 0:
                    break
                k += 1
                if k == 8:
                    break
        remaining = length & 63
        if remaining:
            transformed = self._transform(self.state)
            temp = bytearray(64)
            for j in range(0, 64, 4):
                value = (_u32(self.state, j) + _u32(transformed, j)) & U32
                struct.pack_into('<I', temp, j, value)
            for i in range(remaining):
                output[output_pos + i] = data[input_pos + i] ^ temp[i]


class HxIndexDeserializer(object):
    @classmethod
    def deserialize(cls, data):
        reader = _HxReader(data)
        return reader.read_object()


class _HxReader(object):
    def __init__(self, data):
        self.data = data
        self.pos = 0

    def read(self, count):
        value = self.data[self.pos:self.pos + count]
        self.pos += count
        return value

    def read_byte(self):
        value = self.data[self.pos]
        self.pos += 1
        return value

    def read_int32(self):
        value = struct.unpack_from('<i', self.data, self.pos)[0]
        self.pos += 4
        return value

    def read_big_int32(self):
        value = struct.unpack_from('>i', self.data, self.pos)[0]
        self.pos += 4
        return value

    def read_big_int64(self):
        value = struct.unpack_from('>q', self.data, self.pos)[0]
        self.pos += 8
        return value

    def read_string(self):
        length = self.read_big_int32()
        raw = self.read(2 * length)
        return raw.decode('utf-16le', 'replace')

    def read_object(self):
        type_id = self.read_byte()
        if type_id in (0x00, 0x01):
            return None
        if type_id == 0x02:
            return self.read_string()
        if type_id == 0x03:
            count = self.read_big_int32()
            return self.read(count)
        if type_id in (0x04, 0x05):
            return self.read_big_int64()
        if type_id == 0x81:
            count = self.read_big_int32()
            return [self.read_object() for _ in range(count)]
        if type_id == 0xC1:
            count = self.read_big_int32()
            result = {}
            for _ in range(count):
                name = self.read_string()
                result[name] = self.read_object()
            return result
        raise ValueError('unknown Hx index object type 0x%02X' % type_id)


class HxCrypt(CxEncryption):
    """Hx KiriKiri scheme with an encrypted out-of-line index."""

    def __init__(self, m_mask=0, m_offset=0, PrologOrder=None,
                 OddBranchOrder=None, EvenBranchOrder=None,
                 ControlBlock=None, TpmFileName=None,
                 IndexKey1=None, IndexKey2=None, FilterKey=0,
                 RandomType=0, NamesFile=None, IndexKeyDict=None):
        CxEncryption.__init__(self, m_mask, m_offset, PrologOrder,
                              OddBranchOrder, EvenBranchOrder,
                              ControlBlock, TpmFileName)
        self.IndexKey1 = IndexKey1
        self.IndexKey2 = IndexKey2
        self.FilterKey = FilterKey & M64
        self.RandomType = RandomType
        self.NamesFile = NamesFile
        self.IndexKeyDict = IndexKeyDict or None
        self._names = {}

    def new_program(self, seed):
        return HxProgram(seed, self.ControlBlock, self.RandomType)

    def load_names(self, lines):
        self._names = {}
        for line in lines:
            parts = line.rstrip('\r\n').split(':')
            if len(parts) != 2:
                continue
            key, name = parts
            self._names.setdefault(len(key), {})[key.upper()] = name

    @staticmethod
    def _binary_to_string(data):
        return ''.join('%02X' % value for value in data)

    @staticmethod
    def get_unicode_name(hash_value):
        chars = []
        while True:
            chars.append(chr((hash_value & 0x3FFF) + 0x5000))
            hash_value >>= 14
            if hash_value == 0:
                break
        return ''.join(chars)

    def read_index(self, arc_name, data):
        if len(data) <= 20:
            return None
        index_key1 = self.IndexKey1
        index_key2 = self.IndexKey2
        if self.IndexKeyDict and arc_name in self.IndexKeyDict:
            key = self.IndexKeyDict[arc_name]
            index_key1 = getattr(key, 'Key1', None) or key.get('Key1')
            index_key2 = getattr(key, 'Key2', None) or key.get('Key2')
        if not index_key1 or len(index_key1) != 32:
            return None
        if not index_key2 or len(index_key2) != 16:
            return None
        seed = [1, 0]
        crypt = HxChachaDecryptor(bytes(index_key1), bytes(index_key2), seed)
        buf = bytearray(len(data) - 16)
        crypt.decrypt(data, 16, buf, 0, len(buf))
        index_stream = zlib.decompress(bytes(buf[4:]))
        root_obj = HxIndexDeserializer.deserialize(index_stream)
        if not isinstance(root_obj, list):
            return None

        path_map = self._names.get(16, {})
        name_map = self._names.get(64, {})
        entry_info_map = {}
        for i in range(0, len(root_obj) - 1, 2):
            path_hash = root_obj[i]
            dir_obj = root_obj[i + 1]
            if not isinstance(path_hash, (bytes, bytearray)):
                continue
            if not isinstance(dir_obj, list):
                continue
            path_str = self._binary_to_string(path_hash)
            info_path = path_map.get(path_str)
            for j in range(0, len(dir_obj) - 1, 2):
                entry_hash = dir_obj[j]
                entry_obj = dir_obj[j + 1]
                if not isinstance(entry_hash, (bytes, bytearray)):
                    continue
                if not isinstance(entry_obj, list) or len(entry_obj) < 2:
                    continue
                entry_id = entry_obj[0]
                entry_key = entry_obj[1]
                if entry_id is None or entry_key is None:
                    continue
                info = HxEntry()
                info.path = info_path
                info.name = name_map.get(self._binary_to_string(entry_hash))
                info.key = entry_key & M64
                info.id = entry_id & M64
                uname = self.get_unicode_name(entry_id & U32)
                entry_info_map[uname] = info
        return entry_info_map

    def create_filter_key(self, entry_key, header_key_seed):
        result = HxFilterKey()
        key0 = entry_key & U32
        key1 = (entry_key >> 32) & U32

        k0 = self.execute_xcode(key0)
        result.key[0] = _join64(k0[0], k0[1])
        k1 = self.execute_xcode(key1)
        result.key[1] = _join64(k1[0], k1[1])

        result.split_position = (
            self.m_offset + ((entry_key >> 16) & self.m_mask)) & U32

        k3 = self.execute_xcode(header_key_seed & U32)
        value = (~_join64(k3[0], k3[1])) & M64
        for i, shift in enumerate(range(56, -1, -8)):
            result.header_key[i] = (value >> shift) & 0xFF

        k3 = self.execute_xcode(value & U32)
        value = (~_join64(k3[0], k3[1])) & M64
        for i, shift in enumerate(range(56, -1, -8)):
            result.header_key[i + 8] = (value >> shift) & 0xFF

        result.has_header_key = True
        result.flag = False
        return result

    def create_filter(self, entry):
        info = getattr(entry, 'extra', None)
        if info is None or info.filter is not None:
            return
        entry_key = info.key
        if not (info.id & 0x100000000):
            entry_key ^= self.FilterKey
        header_key = (~entry_key) & M64
        key = self.create_filter_key(entry_key, header_key)
        info.filter = HxFilter(key)

    def init(self, archive=None, reader=None, base_dir=None):
        return

    def entry_read_filter(self, entry, data):
        self.create_filter(entry)
        return Crypt.entry_read_filter(self, entry, data)

    def decrypt(self, entry, offset, buf, pos, count):
        self.create_filter(entry)
        info = getattr(entry, 'extra', None)
        if info is None or info.filter is None:
            return
        info.filter.decrypt(offset, buf, pos, count)

    def encrypt(self, entry, offset, buf, pos, count):
        raise NotImplementedError('HxCrypt encryption is not implemented')


class HxCryptLite(CxEncryption):
    """Wamsoft HxCrypt without hashes (CxdecV1.9)."""

    def __init__(self, m_mask=0, m_offset=0, PrologOrder=None,
                 OddBranchOrder=None, EvenBranchOrder=None,
                 ControlBlock=None, TpmFileName=None,
                 mHeaderKey=None, mHeaderSplitPosition=0,
                 mFileCryptFlag=False, mRandomType=0):
        CxEncryption.__init__(self, m_mask, m_offset, PrologOrder,
                              OddBranchOrder, EvenBranchOrder,
                              ControlBlock, TpmFileName)
        self.mHeaderKey = mHeaderKey
        self.mHeaderSplitPosition = mHeaderSplitPosition
        self.mFileCryptFlag = mFileCryptFlag
        self.mRandomType = mRandomType

    def new_program(self, seed):
        return HxProgramLite(seed, self.ControlBlock, self.mRandomType)

    def decrypt(self, entry, offset, buf, pos, count):
        hash_value = self.entry_hash(entry)
        if self.mHeaderKey is not None:
            header = HxHeaderDecryptor(hash_value, self.mHeaderKey,
                                       self.mHeaderSplitPosition)
            header.decrypt(buf, offset, pos, count)

        ret1 = self.execute_xcode(hash_value)
        ret2 = self.execute_xcode(hash_value ^ (hash_value >> 16))
        key1 = _join64(ret1[0], ret1[1])
        key2 = _join64(ret2[0], ret2[1])
        split_position = self.m_offset + (hash_value & self.m_mask)

        dec1 = HxFileDecryptor(key1, self.mFileCryptFlag)
        dec2 = HxFileDecryptor(key2, self.mFileCryptFlag)
        if split_position > offset:
            if split_position < offset + count:
                length1 = split_position - offset
                length2 = offset + count - split_position
                dec1.decrypt(buf, offset, pos, length1)
                dec2.decrypt(buf, offset + length1, pos + length1, length2)
            else:
                dec1.decrypt(buf, offset, pos, count)
        else:
            dec2.decrypt(buf, offset, pos, count)

    def encrypt(self, entry, offset, buf, pos, count):
        raise NotImplementedError('HxCryptLite encryption is not implemented')


class HxFileDecryptor(object):
    def __init__(self, key, file_key_flag):
        raw = struct.pack('<Q', key & M64)
        self.global_key = raw[0]
        self.key1 = raw[1]
        self.key2 = raw[2]
        self.split_position1 = struct.unpack_from('<H', raw, 6)[0]
        self.split_position2 = struct.unpack_from('<H', raw, 4)[0]
        if self.split_position1 == self.split_position2:
            self.split_position2 += 1
        if self.global_key == 0:
            self.global_key = 1
        self.global_key = (self.global_key * 0x01010101) & U32
        if file_key_flag:
            self.key1 = 0
            self.key2 = 0

    def decrypt(self, data, offset, position, count):
        if count == 0:
            return
        key = struct.pack('<I', self.global_key)
        key_index = offset & 3
        for i in range(count):
            data[position + i] ^= key[key_index]
            key_index = (key_index + 1) & 3
        if self.split_position1 >= offset and \
                self.split_position1 < offset + count:
            data[position + self.split_position1 - offset] ^= self.key1
        if self.split_position2 >= offset and \
                self.split_position2 < offset + count:
            data[position + self.split_position2 - offset] ^= self.key2


class HxHeaderDecryptor(object):
    def __init__(self, hash_value, key, split_pos):
        self.key = None
        self.split_position = 0
        if key is not None and len(key) >= 8:
            self.split_position = split_pos
            k0, k1 = struct.unpack_from('<II', bytes(key), 0)
            s0 = hash_value ^ k1
            s1 = (hash_value ^ ((hash_value << 13) & U32)) & U32
            s2 = (s1 ^ (s1 >> 17)) & U32
            s3 = (s2 ^ ((s2 << 5) & U32) ^ k0) & U32
            self.key = struct.pack('<Q', ((s3 & U32) << 32) | (s0 & U32))

    def decrypt(self, data, offset, position, count):
        if self.key is None:
            return
        key_len = len(self.key)
        start = offset
        if start <= self.split_position:
            start = self.split_position
        end = offset + count
        if end >= self.split_position + key_len:
            end = self.split_position + key_len
        if start >= end:
            return
        key_index = start - self.split_position
        data_index = start - offset + position
        for i in range(end - start):
            data[data_index + i] ^= self.key[key_index + i]


# ---------------------------------------------------------------------------
# YuzCrypt.cs
# ---------------------------------------------------------------------------
class SenrenCxCrypt(CxEncryption):
    names_section_id = 'sen:'

    def read_yuz_names(self, yuz):
        from io import BytesIO
        stream = BytesIO(zlib.decompress(yuz))
        names = {}
        dir_offset = 0
        while stream.tell() < len(zlib.decompress(yuz)):
            pass
        data = zlib.decompress(yuz)
        pos = 0
        while pos + 14 <= len(data):
            entry_size = struct.unpack_from('<q', data, pos + 4)[0]
            if entry_size < 0:
                break
            dir_offset += 12 + entry_size
            hash_value = struct.unpack_from('<I', data, pos + 12)[0]
            name_size = struct.unpack_from('<h', data, pos + 16)[0]
            if name_size > 0 and name_size * 2 <= entry_size - 6:
                name = data[pos + 18:pos + 18 + name_size * 2].decode(
                    'utf-16le', 'replace')
                names[hash_value] = name
            pos = dir_offset
        names['$'] = 'startup.tjs'
        return names


class CabbageCxCrypt(SenrenCxCrypt):
    names_section_id = 'cbg:'

    def __init__(self, m_mask=0, m_offset=0, PrologOrder=None,
                 OddBranchOrder=None, EvenBranchOrder=None,
                 ControlBlock=None, TpmFileName=None, m_random_seed=0):
        SenrenCxCrypt.__init__(self, m_mask, m_offset, PrologOrder,
                               OddBranchOrder, EvenBranchOrder,
                               ControlBlock, TpmFileName)
        self.m_random_seed = m_random_seed & U32

    def new_program(self, seed):
        return CxProgramNana(seed, self.m_random_seed, self.ControlBlock)


class NanaCxCrypt(CabbageCxCrypt):
    names_section_id = 'dls:'

    def __init__(self, m_mask=0, m_offset=0, PrologOrder=None,
                 OddBranchOrder=None, EvenBranchOrder=None,
                 ControlBlock=None, TpmFileName=None, m_random_seed=0,
                 YuzKey=None):
        CabbageCxCrypt.__init__(self, m_mask, m_offset, PrologOrder,
                                OddBranchOrder, EvenBranchOrder,
                                ControlBlock, TpmFileName, m_random_seed)
        self.YuzKey = list(YuzKey or [])

    def read_yuz_names(self, yuz):
        if not self.YuzKey:
            raise CxError('YuzKey required for NanaCxCrypt name list')
        data = bytearray(yuz)
        self.create_name_list_decryptor().decrypt(data, min(len(data), 0x100))
        return SenrenCxCrypt.read_yuz_names(self, bytes(data))

    def create_name_list_decryptor(self):
        return NanaDecryptor(self.YuzKey, self.YuzKey[4], self.YuzKey[5])


class RiddleCxCrypt(NanaCxCrypt):
    names_section_id = 'yuz:'

    def __init__(self, m_mask=0, m_offset=0, PrologOrder=None,
                 OddBranchOrder=None, EvenBranchOrder=None,
                 ControlBlock=None, TpmFileName=None, m_random_seed=0,
                 YuzKey=None, m_key1=0xAAAAAAAA, m_key2=0x55555555):
        NanaCxCrypt.__init__(self, m_mask, m_offset, PrologOrder,
                             OddBranchOrder, EvenBranchOrder, ControlBlock,
                             TpmFileName, m_random_seed, YuzKey)
        self.m_key1 = m_key1 & U32
        self.m_key2 = m_key2 & U32

    def decrypt(self, entry, offset, buf, pos, count):
        self._process_first_bytes(entry, offset, buf, pos, count)
        CxEncryption.decrypt(self, entry, offset, buf, pos, count)

    def encrypt(self, entry, offset, buf, pos, count):
        CxEncryption.encrypt(self, entry, offset, buf, pos, count)
        self._process_first_bytes(entry, offset, buf, pos, count)

    def _process_first_bytes(self, entry, offset, buf, pos, count):
        if offset < 8 and count > 0:
            key = self.get_key_from_hash(self.entry_hash(entry))
            key >>= offset << 3
            first_chunk = min(count, 8 - offset)
            for i in range(first_chunk):
                buf[pos + i] ^= key & 0xFF
                key >>= 8

    def get_key_from_hash(self, hash_value):
        lo = (hash_value ^ self.m_key2) & U32
        hi = ((hash_value << 13) ^ hash_value) & U32
        hi ^= hi >> 17
        hi = (hi ^ ((hi << 5) & U32) ^ self.m_key1) & U32
        return (hi << 32) | lo

    def create_name_list_decryptor(self):
        return YuzDecryptor(self.ControlBlock, self.YuzKey,
                            self.YuzKey[4], self.YuzKey[5])


class YuzDecryptor(object):
    """Name-list decryptor used by ``RiddleCxCrypt``."""

    def __init__(self, key1, key2, seed1, seed2):
        self.state = bytearray(64)
        self.state[0:16] = bytes(key2[0:4]) if False else b''
        raw_key2 = struct.pack('<%dI' % min(4, len(key2)), *key2[:4])
        self.state[0:16] = raw_key2.ljust(16, b'\0')[:16]
        raw_key1 = struct.pack('<%dI' % min(8, len(key1)), *key1[:8])
        self.state[16:48] = raw_key1.ljust(32, b'\0')[:32]
        struct.pack_into('<I', self.state, 48, 0xFFFFFFFF)
        struct.pack_into('<I', self.state, 52, 0xFFFFFFFF)
        struct.pack_into('<I', self.state, 56, (~seed1) & U32)
        struct.pack_into('<I', self.state, 60, (~seed2) & U32)

    def decrypt(self, data, length):
        offset = 0
        pos = 0
        while length > 0:
            state1 = bytearray(self.state)
            struct.pack_into('<I', state1, 48, (~offset) & U32)
            state2 = self._transform(state1, 8)
            count = min(0x40, length)
            for j in range(count):
                data[pos + j] ^= state2[j]
            pos += count
            length -= count
            offset += 1

    @staticmethod
    def _transform(state1, rounds):
        tmp = [0] * 16
        for i in range(16):
            tmp[i] = (~_u32(state1, i * 4)) & U32

        for _ in range((rounds - 1 >> 1) + 1 if rounds > 0 else 0):
            t1 = (tmp[4] + tmp[0]) & U32
            t2 = rotl32(t1 ^ tmp[12], 16)
            t3 = (t2 + tmp[8]) & U32
            t4 = rotl32(tmp[4] ^ t3, 12)
            t5 = (t4 + t1) & U32
            t6 = rotl32(t5 ^ t2, 8)
            tmp[12] = t6
            t6 = (t6 + t3) & U32
            tmp[4] = rotl32(t4 ^ t6, 7)
            t4 = rotl32((tmp[5] + tmp[1]) & U32 ^ tmp[13], 16)
            t3 = rotl32(tmp[5] ^ ((t4 + tmp[9]) & U32), 12)
            t2 = (t3 + tmp[5] + tmp[1]) & U32
            tmp[13] = rotl32(t2 ^ t4, 8)
            tmp[9] = (tmp[9] + tmp[13] + t4) & U32
            tmp[5] = rotl32(t3 ^ tmp[9], 7)
            t4 = rotl32((tmp[6] + tmp[2]) & U32 ^ tmp[14], 16)
            tmp[10] = (tmp[10] + t4) & U32
            t1 = rotl32(tmp[6] ^ tmp[10], 12)
            t3 = (t1 + tmp[6] + tmp[2]) & U32
            tmp[14] = rotl32(t3 ^ t4, 8)
            tmp[6] = rotl32(t1 ^ ((tmp[14] + tmp[10]) & U32), 7)
            tmp[10] = (tmp[10] + tmp[14]) & U32
            t4 = ((tmp[7] + tmp[3]) & U32) ^ tmp[15]
            tmp[3] = (tmp[3] + tmp[7]) & U32
            t4 = rotl32(t4, 16)
            tmp[11] = (tmp[11] + t4) & U32
            t1 = rotl32(tmp[7] ^ tmp[11], 12)
            t4 ^= (t1 + tmp[3]) & U32
            tmp[3] = (tmp[3] + t1) & U32
            t4 = rotl32(t4, 8)
            tmp[11] = (tmp[11] + t4) & U32
            t1 = rotl32(t1 ^ tmp[11], 7)
            t5 = (t5 + tmp[5]) & U32
            t2 = (t2 + tmp[6]) & U32
            t4 = rotl32(t5 ^ t4, 16)
            tmp[10] = (tmp[10] + t4) & U32
            tmp[5] = rotl32(tmp[5] ^ tmp[10], 12)
            tmp[0] = (tmp[5] + t5) & U32
            t4 = rotl32(tmp[0] ^ t4, 8)
            tmp[15] = t4
            tmp[10] = (tmp[10] + t4) & U32
            tmp[5] = rotl32(tmp[5] ^ tmp[10], 7)
            tmp[12] = rotl32(tmp[12] ^ t2, 16)
            tmp[11] = (tmp[11] + tmp[12]) & U32
            t4 = rotl32(tmp[11] ^ tmp[6], 12)
            tmp[1] = (t4 + t2) & U32
            tmp[12] = rotl32(tmp[12] ^ tmp[1], 8)
            tmp[11] = (tmp[11] + tmp[12]) & U32
            tmp[6] = rotl32(t4 ^ tmp[11], 7)
            t3 = (t3 + t1) & U32
            t4 = rotl32(tmp[13] ^ t3, 16)
            t2 = (t4 + t6) & U32
            t1 = rotl32(t2 ^ t1, 12)
            tmp[2] = (t1 + t3) & U32
            tmp[13] = rotl32(t4 ^ tmp[2], 8)
            tmp[8] = (tmp[13] + t2) & U32
            tmp[7] = rotl32(tmp[8] ^ t1, 7)
            t6 = rotl32(tmp[14] ^ ((tmp[4] + tmp[3]) & U32), 16)
            t1 = rotl32(tmp[4] ^ ((t6 + tmp[9]) & U32), 12)
            tmp[3] = (tmp[3] + t1 + tmp[4]) & U32
            t3 = rotl32(t6 ^ tmp[3], 8)
            tmp[9] = (tmp[9] + t3 + t6) & U32
            tmp[4] = rotl32(t1 ^ tmp[9], 7)
            tmp[14] = t3

        target = bytearray(64)
        for i in range(16):
            value = (tmp[i] + ((~_u32(state1, i * 4)) & U32)) & U32
            struct.pack_into('<I', target, i * 4, value)
        return target


class NanaDecryptor(object):
    def __init__(self, key, seed1, seed2):
        self.state = [0] * 27
        self.seed = ((seed2 & U32) << 32) | (seed1 & U32)
        s = [key[1] & U32, key[2] & U32, key[3] & U32]
        k = key[0] & U32
        self.state[0] = k
        dst = 1
        for i in range(26):
            src = i % 3
            m = rotr32(s[src], 8)
            n = (i ^ ((k + m) & U32)) & U32
            k = (n ^ rotl32(k, 3)) & U32
            self.state[dst] = k
            dst += 1
            s[src] = n

    def decrypt(self, data, length):
        pos = 0
        offset = 0
        while length > 0:
            offset = (offset + 1) & M64
            key = self._transform_key(offset ^ self.seed)
            count = min(8, length)
            for j in range(count):
                data[pos + j] ^= key & 0xFF
                key >>= 8
            pos += count
            length -= count

    def _transform_key(self, key):
        lo = key & U32
        hi = (key >> 32) & U32
        for i in range(27):
            hi = rotr32(hi, 8)
            hi = (hi + lo) & U32
            hi ^= self.state[i]
            lo = rotl32(lo, 3)
            lo ^= hi
        return ((hi & U32) << 32) | lo


# ---------------------------------------------------------------------------
# scheme database
# ---------------------------------------------------------------------------
CLASS_BY_NAME = {
    'NoCrypt': NoCrypt,
    'FateCrypt': FateCrypt,
    'MizukakeCrypt': MizukakeCrypt,
    'HashCrypt': HashCrypt,
    'XorCrypt': XorCrypt,
    'FlyingShineCrypt': FlyingShineCrypt,
    'SeitenCrypt': SeitenCrypt,
    'OkibaCrypt': OkibaCrypt,
    'DieselmineCrypt': DieselmineCrypt,
    'DameganeCrypt': DameganeCrypt,
    'NephriteCrypt': NephriteCrypt,
    'AlteredPinkCrypt': AlteredPinkCrypt,
    'NatsupochiCrypt': NatsupochiCrypt,
    'PoringSoftCrypt': PoringSoftCrypt,
    'AppliqueCrypt': AppliqueCrypt,
    'TokidokiCrypt': TokidokiCrypt,
    'SourireCrypt': SourireCrypt,
    'HibikiCrypt': HibikiCrypt,
    'AkabeiCrypt': AkabeiCrypt,
    'HaikuoCrypt': HaikuoCrypt,
    'StripeCrypt': StripeCrypt,
    'ExaCrypt': ExaCrypt,
    'SmileCrypt': SmileCrypt,
    'YuzuCrypt': YuzuCrypt,
    'HighRunningCrypt': HighRunningCrypt,
    'KissCrypt': KissCrypt,
    'PuCaCrypt': PuCaCrypt,
    'RhapsodyCrypt': RhapsodyCrypt,
    'MadoCrypt': MadoCrypt,
    'SmxCrypt': SmxCrypt,
    'FestivalCrypt': FestivalCrypt,
    'PinPointCrypt': PinPointCrypt,
    'HybridCrypt': HybridCrypt,
    'NekoWorksCrypt': NekoWorksCrypt,
    'NinkiSeiyuuCrypt': NinkiSeiyuuCrypt,
    'SyangrilaSmartCrypt': SyangrilaSmartCrypt,
    'GensouCrypt': GensouCrypt,
    'Kano2Crypt': Kano2Crypt,
    'MiburoCrypt': MiburoCrypt,
    'HoneyCombCrypt': HoneyCombCrypt,
    'CzCrypt': CzCrypt,
    'ChainReactionCrypt': ChainReactionCrypt,
    'HachukanoCrypt': HachukanoCrypt,
    'ChocolatCrypt': ChocolatCrypt,
    'XanaduCrypt': XanaduCrypt,
    'SisMikoCrypt': SisMikoCrypt,
    'CroixCrypt': CroixCrypt,
    'PureMoreCrypt': PureMoreCrypt,
    'CxEncryption': CxEncryption,
    'SenrenCxCrypt': SenrenCxCrypt,
    'CabbageCxCrypt': CabbageCxCrypt,
    'NanaCxCrypt': NanaCxCrypt,
    'RiddleCxCrypt': RiddleCxCrypt,
    'HxCrypt': HxCrypt,
    'HxCryptLite': HxCryptLite,
}


def _field_name(key):
    if key.startswith('<'):
        end = key.find('>')
        if end != -1:
            return key[1:end]
    if '+' in key:
        key = key.rsplit('+', 1)[1]
    return key


def make_crypt(spec):
    """Instantiate a filter from one ``KnownSchemes`` record.

    ``spec`` is either ``{'class': ..., 'fields': {...}}`` (our dumped form) or
    the raw dictionary with a ``__class__`` key.
    """
    if isinstance(spec, str):
        raise TypeError('pass a scheme record, not a title')
    if 'fields' in spec:
        class_name = spec['class']
        fields = spec['fields']
    else:
        class_name = spec.get('__class__', '').rsplit('.', 1)[-1]
        fields = dict((_field_name(k), v) for k, v in spec.items()
                      if k != '__class__')
    class_name = class_name.rsplit('.', 1)[-1]
    cls = CLASS_BY_NAME.get(class_name)
    if cls is None:
        raise KeyError('unsupported XP3 crypt class %r' % class_name)
    signature = inspect.signature(cls.__init__)
    kwargs = {}
    for name, parameter in signature.parameters.items():
        if name == 'self' or parameter.kind in (parameter.VAR_POSITIONAL,
                                                parameter.VAR_KEYWORD):
            continue
        if name in fields:
            kwargs[name] = fields[name]
    return cls(**kwargs)


def load_schemes(path=None):
    if path is None:
        here = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(here, '..', 'schemes', 'xp3_schemes.json')
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    return data


def load_scheme(title, path=None):
    schemes = load_schemes(path)
    if title not in schemes:
        raise KeyError('unknown XP3 scheme %r' % title)
    return make_crypt(schemes[title])


def resolve_scheme(name, path=None):
    """Load a scheme by exact title, case-insensitive title, or unique fragment."""
    schemes = load_schemes(path)
    if name in schemes:
        return make_crypt(schemes[name])
    lowered = (name or '').strip().lower()
    exact = [title for title in schemes if title.lower() == lowered]
    if len(exact) == 1:
        return make_crypt(schemes[exact[0]])
    partial = [title for title in schemes if lowered and lowered in title.lower()]
    if len(partial) == 1:
        return make_crypt(schemes[partial[0]])
    if not partial:
        raise KeyError('unknown XP3 scheme %r' % name)
    raise KeyError('ambiguous XP3 scheme %r: %s'
                   % (name, '; '.join(sorted(partial)[:8])))


def scheme_names(path=None):
    return sorted(load_schemes(path).keys())


# ---------------------------------------------------------------------------
# automatic scheme detection
# ---------------------------------------------------------------------------
TEXT_EXTENSIONS = ('.tjs', '.ks', '.txt', '.scn', '.csv', '.ini')


def _pick_samples(archive, limit=3):
    """Choose small text-like entries that are cheap to test."""
    picked = []
    for entry in archive.entries:
        name = (entry.name or '').lower()
        if name.startswith('$$$'):
            continue
        if not name.endswith(TEXT_EXTENSIONS):
            continue
        if (entry.original_size or 0) < 64:
            continue
        picked.append(entry)
    picked.sort(key=lambda e: e.original_size or 0)
    if len(picked) <= limit:
        return picked
    head = picked[:1]
    tail = picked[len(picked) // 2:len(picked) // 2 + limit - 1]
    return (head + tail)[:limit]


def _quality(data):
    """Heuristic 0..2 score: decrypted UTF-16 / TJS / PSB bodies score high."""
    if not data:
        return 0.0
    head = data[:2048]
    size = len(head)
    if not size:
        return 0.0
    zeros = head.count(0) / size
    score = zeros * 2.0
    if data[:4] in (b'TJS2', b'PSB\x00'):
        score += 1.0
    if data[:2] in (b'\xff\xfe', b'\xfe\xff'):
        score += 1.0
    if b'/*' in head or b'\r\x00\n\x00' in head:
        score += 0.5
    return min(score, 2.0)


def score_schemes(archive_path, candidates=None, limit=3):
    """Return ``[(score, title), ...]`` sorted best-first."""
    import xp3 as xp3mod

    archive = xp3mod.parse_archive(archive_path)
    samples = _pick_samples(archive, limit)
    if not samples:
        return []
    blobs = []
    for entry in samples:
        try:
            raw = xp3mod.extract_entry(archive_path, entry)[:2048]
        except Exception:
            continue
        blobs.append((entry, raw))
    if not blobs:
        return []

    base_dir = os.path.dirname(os.path.abspath(archive_path))
    schemes = load_schemes()
    titles = candidates if candidates is not None else list(schemes.keys())
    results = []
    for title in titles:
        spec = schemes.get(title)
        if spec is None:
            continue
        try:
            crypt = make_crypt(spec)
            crypt.init(archive=archive, reader=None, base_dir=base_dir)
        except Exception:
            continue
        total = 0.0
        for entry, raw in blobs:
            try:
                plain = crypt.decrypt_all(entry, raw)
            except Exception:
                total = 0.0
                break
            total += _quality(plain)
        results.append((total / len(blobs), title))
    results.sort(key=lambda item: (-item[0], item[1]))
    return results


def detect_scheme(archive_path, candidates=None, threshold=0.8, limit=3):
    """Return the best scheme title, or ``None`` when nothing scores well."""
    results = score_schemes(archive_path, candidates=candidates, limit=limit)
    if not results:
        return None
    score, title = results[0]
    if score < threshold:
        return None
    return title
