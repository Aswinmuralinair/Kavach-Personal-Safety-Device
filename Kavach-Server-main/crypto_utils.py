"""
crypto_utils.py — Kavach Server

AES-CBC and ChaCha20-Poly1305 encrypt/decrypt helpers. Used by the device
(Pi) to encrypt telemetry before upload, and by the server to decrypt it.
"""

import os
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305


# ── Key loaders ──────────────────────────────────────────────────────────────

def load_aes_key() -> bytes:
    # Resolve relative to this module, not the current working directory
    base_dir = os.path.dirname(os.path.abspath(__file__))
    key_path = os.path.join(base_dir, "keys", "aes.key")
    with open(key_path, "rb") as f:
        return f.read()


def load_chacha_key() -> bytes:
    base_dir = os.path.dirname(os.path.abspath(__file__))
    key_path = os.path.join(base_dir, "keys", "chacha.key")
    with open(key_path, "rb") as f:
        return f.read()


# ── AES-CBC helpers ───────────────────────────────────────────────────────────

def aes_encrypt_text(plaintext: str) -> bytes:
    key = load_aes_key()
    iv  = os.urandom(16)                      # AES block size = 16 bytes
    padder      = padding.PKCS7(128).padder()
    padded_data = padder.update(plaintext.encode()) + padder.finalize()
    cipher      = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
    encryptor   = cipher.encryptor()
    ciphertext  = encryptor.update(padded_data) + encryptor.finalize()
    return iv + ciphertext


def aes_decrypt_text(encrypted_data: bytes) -> str:
    key        = load_aes_key()
    iv         = encrypted_data[:16]
    ciphertext = encrypted_data[16:]
    cipher     = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
    decryptor  = cipher.decryptor()
    padded_plaintext = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder   = padding.PKCS7(128).unpadder()
    plaintext  = unpadder.update(padded_plaintext) + unpadder.finalize()
    return plaintext.decode()


# ── ChaCha20-Poly1305 helpers ─────────────────────────────────────────────────

def chacha_encrypt_text(plaintext: str) -> bytes:
    key    = load_chacha_key()
    chacha = ChaCha20Poly1305(key)
    nonce  = os.urandom(12)                   # ChaCha20 requires 12-byte nonce
    ciphertext = chacha.encrypt(nonce, plaintext.encode(), None)
    return nonce + ciphertext


def chacha_decrypt_text(encrypted_data: bytes) -> str:
    key    = load_chacha_key()
    chacha = ChaCha20Poly1305(key)
    nonce      = encrypted_data[:12]
    ciphertext = encrypted_data[12:]
    plaintext  = chacha.decrypt(nonce, ciphertext, None)
    return plaintext.decode()


def chacha_decrypt_bytes(encrypted_data: bytes) -> bytes:
    """Decrypt raw bytes encrypted with ChaCha20-Poly1305.

    Used to decrypt evidence files (video clips, images) that the device
    encrypted before uploading.  The device calls chacha_encrypt_bytes()
    in its own crypto_utils.py before sending to the server.
    """
    key    = load_chacha_key()
    chacha = ChaCha20Poly1305(key)
    nonce      = encrypted_data[:12]
    ciphertext = encrypted_data[12:]
    return chacha.decrypt(nonce, ciphertext, None)


# ── Self-test (merged into a single block) ────────────────────────────────────

if __name__ == "__main__":
    # AES test
    msg_aes = "SOS ALERT: GPS LOCATION"
    enc_aes = aes_encrypt_text(msg_aes)
    dec_aes = aes_decrypt_text(enc_aes)
    print("=== AES CBC ===")
    print("Original: ", msg_aes)
    print("Encrypted:", enc_aes.hex())
    print("Decrypted:", dec_aes)
    assert dec_aes == msg_aes, "AES round-trip failed"

    print()

    # ChaCha20-Poly1305 test
    msg_cha = "HELLO SERVER, THIS IS ENCRYPTED"
    enc_cha = chacha_encrypt_text(msg_cha)
    dec_cha = chacha_decrypt_text(enc_cha)
    print("=== ChaCha20-Poly1305 ===")
    print("Original: ", msg_cha)
    print("Encrypted:", enc_cha.hex())
    print("Decrypted:", dec_cha)
    assert dec_cha == msg_cha, "ChaCha20 round-trip failed"

    print("\nAll crypto self-tests passed.")