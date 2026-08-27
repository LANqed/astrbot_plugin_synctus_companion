"""跨语言一致性测试：Python 客户端必须与 crates/core 派生出同一个房间。

向量由 Rust 侧生成（RoomKeys::derive + auth_response），任何一方改动
Argon2/HKDF/HMAC 参数都会在这里失败，而不是在运行时表现为"连上了但收不到
对方消息"。

运行：python -m pytest astrbot_plugin_synctus_companion/tests -q
"""

from __future__ import annotations

import pytest

from astrbot_plugin_synctus_companion.synctus import crypto
from astrbot_plugin_synctus_companion.synctus.crypto import MissingDependency

# 由 crates/core 生成：RoomKeys::derive("ABCD-EFGH-JKLM-NPQR")
VECTOR_CODE = "ABCD-EFGH-JKLM-NPQR"
VECTOR_ROOM_ID = "158d67d0d95fa9d8f2f2ed0744f60039"
# auth_response(challenge = [7u8; 32])
VECTOR_AUTH_MAC_B64 = "WNKrvsT/X5CZYrg1XvlW98LAIojFcTFCTZrRzCXzCaQ="


def _keys(code: str = VECTOR_CODE):
    try:
        return crypto.RoomKeys(code)
    except MissingDependency as exc:
        pytest.skip(str(exc))


def test_room_id_matches_rust_vector():
    assert _keys().room_id_hex() == VECTOR_ROOM_ID


def test_auth_response_matches_rust_vector():
    mac = _keys().auth_response(bytes([7]) * 32)
    assert crypto.b64(mac) == VECTOR_AUTH_MAC_B64


def test_separators_and_case_do_not_change_the_room():
    a = _keys("abcd-efgh-jklm-npqr")
    b = _keys("ABCD EFGH JKLM NPQR")
    assert a.room_id_hex() == b.room_id_hex() == VECTOR_ROOM_ID


def test_seal_open_roundtrip():
    keys = _keys()
    sealed = keys.seal(b"hello", b"device-a")
    # nonce(24) + ciphertext(5) + tag(16)
    assert len(sealed) == 24 + 5 + 16
    assert keys.open(sealed, b"device-a") == b"hello"


def test_aad_mismatch_is_rejected():
    keys = _keys()
    sealed = keys.seal(b"hello", b"device-a")
    with pytest.raises(ValueError):
        keys.open(sealed, b"device-b")


def test_tampering_is_detected():
    keys = _keys()
    sealed = bytearray(keys.seal(b"hello", b"aad"))
    sealed[-1] ^= 0x01
    with pytest.raises(ValueError):
        keys.open(bytes(sealed), b"aad")


def test_nonce_is_fresh_per_message():
    keys = _keys()
    first = keys.seal(b"same", b"aad")
    second = keys.seal(b"same", b"aad")
    assert first[:24] != second[:24]


def test_short_codes_are_refused():
    with pytest.raises(ValueError):
        crypto.RoomKeys("ab-cd")


def test_generated_codes_are_formatted():
    code = crypto.generate_invite_code()
    assert len(code) == 19  # 16 字符 + 3 个连字符
    assert crypto.normalise_code(code).isalnum()
