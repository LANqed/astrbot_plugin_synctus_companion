"""端到端加密：与 crates/core/src/crypto.rs 逐位对应。

    配对码 ──Argon2id(salt="synctus/room/v1", m=64MiB, t=3, p=1)──► 32 字节房间根密钥
                    │
            HKDF-SHA256(root, info=…)
                    ├─► room_id  16 字节，明文发给中继
                    ├─► auth_key 32 字节，证明房间成员身份
                    └─► msg_key  32 字节，XChaCha20-Poly1305 载荷密钥

任何一处参数改动都会让 Python 端与 Rust 端进入不同的房间，
因此常量在这里集中定义并有跨语言测试向量覆盖（tests/test_crypto_vectors.py）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from typing import Any

# 固定 salt：双方只凭配对码派生同一把密钥，没有地方存随机 salt。
# Argon2id 在这里的作用是抬高弱配对码的暴破成本，不是防彩虹表。
ROOM_SALT = b"synctus/room/v1"
INFO_ROOM_ID = b"synctus/v1/room-id"
INFO_AUTH = b"synctus/v1/auth"
INFO_MSG = b"synctus/v1/message"

ARGON_MEM_KIB = 64 * 1024
ARGON_PASSES = 3
ARGON_LANES = 1

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

NONCE_LEN = 24
TAG_LEN = 16

_ARGON_HINT = (
    "缺少 argon2-cffi，无法派生房间密钥。请在 AstrBot 环境执行："
    "pip install argon2-cffi"
)
_NACL_HINT = "缺少 PyNaCl，无法加解密载荷。请在 AstrBot 环境执行：pip install pynacl"


class MissingDependency(RuntimeError):
    """依赖缺失。单独成类，便于插件把它降级为一条可读的日志而不是崩溃。"""


def _argon2_hash_raw(password: bytes) -> bytes:
    try:
        from argon2.low_level import Type, hash_secret_raw
    except ImportError as exc:  # pragma: no cover - 环境相关
        raise MissingDependency(_ARGON_HINT) from exc
    return hash_secret_raw(
        secret=password,
        salt=ROOM_SALT,
        time_cost=ARGON_PASSES,
        memory_cost=ARGON_MEM_KIB,
        parallelism=ARGON_LANES,
        hash_len=32,
        type=Type.ID,
    )


def _aead() -> Any:
    try:
        from nacl import bindings
    except ImportError as exc:  # pragma: no cover - 环境相关
        raise MissingDependency(_NACL_HINT) from exc
    if not hasattr(bindings, "crypto_aead_xchacha20poly1305_ietf_encrypt"):
        raise MissingDependency(
            "当前 PyNaCl 不含 XChaCha20-Poly1305，请升级：pip install -U pynacl"
        )
    return bindings


def _hkdf_sha256(ikm: bytes, info: bytes, length: int) -> bytes:
    """RFC 5869，salt 省略（等价于 32 字节全 0），与 Rust 的 Hkdf::new(None, ..) 一致。"""
    prk = hmac.new(b"\x00" * hashlib.sha256().digest_size, ikm, hashlib.sha256).digest()
    out = b""
    block = b""
    counter = 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        out += block
        counter += 1
    return out[:length]


def normalise_code(code: str) -> str:
    """只保留字母数字并转大写，于是 `abcd-efgh` 与 `ABCD EFGH` 是同一个房间。"""
    return "".join(ch.upper() for ch in code if ch.isascii() and ch.isalnum())


def format_invite_code(code: str) -> str:
    normalised = normalise_code(code)
    return "-".join(
        normalised[i : i + 4] for i in range(0, len(normalised), 4)
    )


def generate_invite_code() -> str:
    """16 个字符、32 符号字母表，即 80 位熵。"""
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(16))
    return format_invite_code(raw)


def random_id(num_bytes: int) -> str:
    return os.urandom(num_bytes).hex()


class RoomKeys:
    """一个配对码派生出的全部密钥。"""

    __slots__ = ("_auth_key", "_msg_key", "_room_id")

    def __init__(self, invite_code: str) -> None:
        normalised = normalise_code(invite_code)
        if len(normalised) < 8:
            raise ValueError("配对码太短：至少需要 8 个有效字符")
        root = _argon2_hash_raw(normalised.encode("ascii"))
        self._room_id = _hkdf_sha256(root, INFO_ROOM_ID, 16)
        self._auth_key = _hkdf_sha256(root, INFO_AUTH, 32)
        self._msg_key = _hkdf_sha256(root, INFO_MSG, 32)

    def __repr__(self) -> str:  # 永不打印密钥材料
        return f"RoomKeys(room_id={self.room_id_hex()!r})"

    def room_id_hex(self) -> str:
        return self._room_id.hex()

    def auth_response(self, challenge: bytes) -> bytes:
        """回答中继的挑战：HMAC(auth_key, "synctus-auth" ‖ challenge)。"""
        mac = hmac.new(self._auth_key, b"synctus-auth", hashlib.sha256)
        mac.update(challenge)
        return mac.digest()

    def seal(self, plaintext: bytes, aad: bytes) -> bytes:
        """加密并返回 `nonce ‖ ciphertext`。aad 绑定发送方设备标识。"""
        bindings = _aead()
        nonce = os.urandom(NONCE_LEN)
        ct = bindings.crypto_aead_xchacha20poly1305_ietf_encrypt(
            plaintext, aad, nonce, self._msg_key
        )
        return nonce + ct

    def open(self, sealed: bytes, aad: bytes) -> bytes:
        bindings = _aead()
        if len(sealed) < NONCE_LEN + TAG_LEN:
            raise ValueError("密文长度不足")
        nonce, ct = sealed[:NONCE_LEN], sealed[NONCE_LEN:]
        try:
            return bindings.crypto_aead_xchacha20poly1305_ietf_decrypt(
                ct, aad, nonce, self._msg_key
            )
        except Exception as exc:
            raise ValueError("解密失败：配对码不一致或数据被篡改") from exc


def b64(data: bytes) -> str:
    return base64.standard_b64encode(data).decode("ascii")


def unb64(text: str) -> bytes:
    return base64.standard_b64decode(text.encode("ascii"))
