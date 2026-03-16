HOW TO GENERATE AND DEPLOY THE SHARED CHACHA20 KEY
====================================================

Both the Kavach Server AND the Personal Safety Device must use the
EXACT SAME chacha.key file.  Generate it ONCE, then copy it to both.

STEP 1 — Generate the key (run this on your laptop or the server):

    python3 - <<'EOF'
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    import os, pathlib
    pathlib.Path("keys").mkdir(exist_ok=True)
    key = ChaCha20Poly1305.generate_key()   # always 32 bytes
    open("keys/chacha.key", "wb").write(key)
    print("Key written to keys/chacha.key —", len(key), "bytes")
    EOF

STEP 2 — Copy the key to both locations:

    Server (on the machine running app.py):
        Kavach-Server-main/keys/chacha.key

    Device (on the Raspberry Pi running main.py):
        Personal-Safety-Device-main/keys/chacha.key

    Use SCP, a USB drive, or any secure channel.
    Never commit keys/ to Git — it is in .gitignore.

STEP 3 — Verify both keys are identical:

    sha256sum Kavach-Server-main/keys/chacha.key
    sha256sum Personal-Safety-Device-main/keys/chacha.key
    # Both hashes must be IDENTICAL.

If the hashes differ, decryption will fail with an InvalidTag exception
and every alert upload will return HTTP 500 on the server.