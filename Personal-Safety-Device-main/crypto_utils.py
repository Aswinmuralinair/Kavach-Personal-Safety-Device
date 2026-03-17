"""
crypto_utils.py — Personal Safety Device (client side)

ChaCha20-Poly1305 encrypt/decrypt helpers used to encrypt the telemetry
payload before it is sent to the Kavach Server.

The keys/chacha.key file must be byte-for-byte identical to the one on the
server — generate it once and copy it to both locations:

    python3 -c "
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    import os, pathlib
    pathlib.Path('keys').mkdir(exist_ok=True)
    key = ChaCha20Poly1305.generate_key()
    open('keys/chacha.key', 'wb').write(key)
    print('Key written to keys/chacha.key')
    "
"""

import os
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305


def _load_chacha_key() -> bytes:
    base_dir = os.path.dirname(os.path.abspath(__file__))
    key_path = os.path.join(base_dir, "keys", "chacha.key")
    with open(key_path, "rb") as f:
        return f.read()


def chacha_encrypt_text(plaintext: str) -> bytes:
    """Encrypt a UTF-8 string with ChaCha20-Poly1305.
    Returns: 12-byte nonce + ciphertext (with 16-byte Poly1305 tag).
    """
    key    = _load_chacha_key()
    chacha = ChaCha20Poly1305(key)
    nonce  = os.urandom(12)   # ChaCha20 requires exactly 12 bytes
    ciphertext = chacha.encrypt(nonce, plaintext.encode(), None)
    return nonce + ciphertext


def chacha_decrypt_text(encrypted_data: bytes) -> str:
    """Decrypt bytes produced by chacha_encrypt_text()."""
    key        = _load_chacha_key()
    chacha     = ChaCha20Poly1305(key)
    nonce      = encrypted_data[:12]
    ciphertext = encrypted_data[12:]
    plaintext  = chacha.decrypt(nonce, ciphertext, None)
    return plaintext.decode()


def chacha_encrypt_bytes(data: bytes) -> bytes:
    """Encrypt raw bytes with ChaCha20-Poly1305.
    Returns: 12-byte nonce + ciphertext (with 16-byte Poly1305 tag).

    Used to encrypt evidence files (video clips, images) before uploading
    to the server.  The server calls chacha_decrypt_bytes() to recover
    the original file.
    """
    key    = _load_chacha_key()
    chacha = ChaCha20Poly1305(key)
    nonce  = os.urandom(12)
    ciphertext = chacha.encrypt(nonce, data, None)
    return nonce + ciphertext


def chacha_decrypt_bytes(encrypted_data: bytes) -> bytes:
    """Decrypt raw bytes produced by chacha_encrypt_bytes()."""
    key        = _load_chacha_key()
    chacha     = ChaCha20Poly1305(key)
    nonce      = encrypted_data[:12]
    ciphertext = encrypted_data[12:]
    return chacha.decrypt(nonce, ciphertext, None)


if __name__ == "__main__":
    msg = "KAVACH DEVICE TEST — ChaCha20"
    enc = chacha_encrypt_text(msg)
    dec = chacha_decrypt_text(enc)
    print("Original: ", msg)
    print("Encrypted:", enc.hex())
    print("Decrypted:", dec)
    assert dec == msg, "Round-trip failed!"
    print("Self-test passed.")