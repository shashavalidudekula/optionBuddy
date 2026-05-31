"""
secrets.py — Credential encryption/decryption for broker credentials.

Uses Fernet (symmetric encryption) to encrypt/decrypt broker API keys and secrets.
Encryption key should be generated once and stored securely (e.g., environment variable).

Example:
    import os
    from web.secrets import encrypt_json, decrypt_json

    creds = {'api_key': 'xxx', 'api_secret': 'yyy'}
    encrypted = encrypt_json(creds)

    # Store encrypted in DB
    db.save_credentials(encrypted)

    # Retrieve and decrypt
    encrypted = db.get_credentials()
    creds = decrypt_json(encrypted)
"""

import os
import json
from cryptography.fernet import Fernet
from config.logger import get_logger

log = get_logger("secrets")

# Generate or load encryption key
def get_encryption_key() -> bytes:
    """Get or generate encryption key."""
    key_str = os.getenv("ENCRYPTION_KEY")

    if not key_str:
        # Generate new key if not provided
        key = Fernet.generate_key()
        log.warning("No ENCRYPTION_KEY in environment. Generated new key.")
        log.warning(f"Set ENCRYPTION_KEY={key.decode()} in .env to use this key")
        return key

    return key_str.encode() if isinstance(key_str, str) else key_str


ENCRYPTION_KEY = get_encryption_key()
cipher = Fernet(ENCRYPTION_KEY)


def encrypt_json(data: dict) -> bytes:
    """
    Encrypt a dictionary to bytes.

    Args:
        data: Dictionary to encrypt (e.g., {'api_key': '...', 'api_secret': '...'})

    Returns:
        Encrypted bytes
    """
    try:
        json_str = json.dumps(data)
        encrypted = cipher.encrypt(json_str.encode())
        return encrypted
    except Exception as e:
        log.error(f"Encryption failed: {e}")
        raise


def decrypt_json(encrypted: bytes) -> dict:
    """
    Decrypt bytes to dictionary.

    Args:
        encrypted: Encrypted bytes from encrypt_json()

    Returns:
        Decrypted dictionary
    """
    try:
        decrypted = cipher.decrypt(encrypted)
        data = json.loads(decrypted.decode())
        return data
    except Exception as e:
        log.error(f"Decryption failed: {e}")
        raise


def hash_password(password: str) -> str:
    """
    Hash password using bcrypt.

    Args:
        password: Plain text password

    Returns:
        Bcrypt hash
    """
    try:
        import bcrypt
        salt = bcrypt.gensalt()
        hashed = bcrypt.hashpw(password.encode(), salt)
        return hashed.decode()
    except Exception as e:
        log.error(f"Password hashing failed: {e}")
        raise


def verify_password(password: str, hashed: str) -> bool:
    """
    Verify password against bcrypt hash.

    Args:
        password: Plain text password to verify
        hashed: Bcrypt hash to check against

    Returns:
        True if password matches, False otherwise
    """
    try:
        import bcrypt
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except Exception as e:
        log.error(f"Password verification failed: {e}")
        return False
