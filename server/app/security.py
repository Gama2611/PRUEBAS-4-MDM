"""Hash de contraseñas (scrypt, stdlib) y tokens de dispositivo."""
import base64
import hashlib
import hmac
import secrets

_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_b64, dk_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
        dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
        return hmac.compare_digest(dk, expected)
    except (ValueError, TypeError):
        return False


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def token_hash(token: str) -> str:
    """Los tokens se guardan sólo como SHA-256: una fuga de la BD no los expone."""
    return hashlib.sha256(token.encode()).hexdigest()


def pin_hash(pin: str, salt: str) -> str:
    """Hash del PIN de salida del kiosco; el agente lo verifica sin conexión."""
    return hashlib.sha256((salt + ":" + pin).encode()).hexdigest()
