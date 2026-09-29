"""
Symmetric encryption for secrets that must be stored reversibly (unlike
login passwords, which only ever need password_hash's one-way hash — see
helpers/jwt_utils.py). Users.outlook_password is the first user of this:
the Outlook automation needs the real password back to type it into
Microsoft's login form, so a one-way hash can't work here.

OUTLOOK_CREDENTIALS_KEY must be a Fernet key (44 base64 chars) — generate
one with:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
and put it in .env. Losing/rotating this key makes every previously-encrypted
outlook_password unreadable, so treat it like JWT_SECRET: generate once per
environment, never commit it.
"""

import os

from cryptography.fernet import Fernet, InvalidToken

_KEY = os.getenv("OUTLOOK_CREDENTIALS_KEY")
_fernet = Fernet(_KEY.encode()) if _KEY else None


def encrypt_secret(plaintext: str) -> str:
    if not _fernet:
        raise RuntimeError(
            "OUTLOOK_CREDENTIALS_KEY is not set in .env — required to store an "
            "Outlook password. Generate one with: python -c \"from cryptography.fernet "
            "import Fernet; print(Fernet.generate_key().decode())\""
        )
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    if not _fernet:
        raise RuntimeError("OUTLOOK_CREDENTIALS_KEY is not set in .env — cannot decrypt stored secrets.")
    try:
        return _fernet.decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        raise RuntimeError("Stored secret could not be decrypted — OUTLOOK_CREDENTIALS_KEY may have changed.")
