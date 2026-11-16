"""PBKDF2 credentials and opaque session/invitation tokens."""
import hashlib
import hmac
import secrets

PASSWORD_ITERATIONS = 310_000


class ResearchPasswordSecurity:
    def hash_password(self, password: str) -> str:
        salt = secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), PASSWORD_ITERATIONS)
        return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt}${digest.hex()}"

    def verify_password(self, password: str, encoded: str) -> bool:
        if not isinstance(password, str) or len(password) > 1024:
            return False
        try:
            algorithm, iterations, salt, expected = encoded.split("$")
            if algorithm != "pbkdf2_sha256":
                return False
            actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations)).hex()
        except (ValueError, TypeError, OverflowError):
            return False
        return hmac.compare_digest(actual, expected)

    def new_token(self) -> str:
        return secrets.token_urlsafe(32)

    def hash_token(self, token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()
