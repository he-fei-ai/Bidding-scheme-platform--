"""API Key 加解密单元测试

测试覆盖:
- encrypt_api_key / decrypt_api_key 往返
- 兼容未加密的历史明文数据
- 脱敏函数 mask_api_key
- 边界情况:空字符串、None、非 ENC: 前缀
"""
import os
import sys
from pathlib import Path

# 注入 backend 到 Python 路径
BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from app.services.crypto import (  # noqa: E402
    decrypt_api_key,
    encrypt_api_key,
    is_encrypted,
    mask_api_key,
)


def test_encrypt_decrypt_roundtrip():
    """加密后解密应还原原文"""
    plaintext = "sk-test-1234567890abcdef"
    encrypted = encrypt_api_key(plaintext)
    assert is_encrypted(encrypted), f"应返回加密结果,但得到 {encrypted[:20]}"
    decrypted = decrypt_api_key(encrypted)
    assert decrypted == plaintext, f"解密失败: {decrypted} != {plaintext}"
    print("[PASS] test_encrypt_decrypt_roundtrip")


def test_encrypt_empty():
    """空字符串加密应返回原值"""
    assert encrypt_api_key("") == ""
    assert decrypt_api_key("") == ""
    print("[PASS] test_encrypt_empty")


def test_decrypt_plaintext():
    """未加密的明文应原样返回(向后兼容)"""
    plaintext = "sk-legacy-plaintext-key"
    assert decrypt_api_key(plaintext) == plaintext
    print("[PASS] test_decrypt_plaintext")


def test_encrypt_idempotent():
    """对已加密的字符串再次加密应保持不变"""
    plaintext = "sk-test-abcdef"
    encrypted = encrypt_api_key(plaintext)
    encrypted_again = encrypt_api_key(encrypted)
    assert encrypted == encrypted_again, "重复加密应保持稳定"
    assert decrypt_api_key(encrypted_again) == plaintext
    print("[PASS] test_encrypt_idempotent")


def test_mask_api_key():
    """脱敏函数应保留首尾字符"""
    plaintext = "sk-1234567890abcdef-test"
    encrypted = encrypt_api_key(plaintext)
    masked = mask_api_key(encrypted)
    assert "****" in masked
    assert masked.startswith("sk-1")
    assert masked.endswith("test")
    # 短字符串脱敏
    assert mask_api_key("") == ""
    assert mask_api_key("abc") == "****"
    print("[PASS] test_mask_api_key")


def test_is_encrypted():
    """is_encrypted 应正确识别"""
    assert is_encrypted("ENC:abc") is True
    assert is_encrypted("sk-plain") is False
    assert is_encrypted("") is False
    print("[PASS] test_is_encrypted")


def test_decrypt_corrupted():
    """被破坏的密文应优雅降级(返回原文)"""
    corrupted = "ENC:this-is-not-valid-base64!!!"
    result = decrypt_api_key(corrupted)
    # 应返回原文(降级),不抛异常
    assert result == corrupted
    print("[PASS] test_decrypt_corrupted")


if __name__ == "__main__":
    test_encrypt_decrypt_roundtrip()
    test_encrypt_empty()
    test_decrypt_plaintext()
    test_encrypt_idempotent()
    test_mask_api_key()
    test_is_encrypted()
    test_decrypt_corrupted()
    print("\n[OK] 全部 7 个 crypto 测试通过")
