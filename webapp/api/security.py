"""Password hashing and JWT issuing/verification for the 15 known DES
accounts. No self-serve signup - see seed_users.py; an admin creates
accounts up front, which is the right amount of process for a fixed,
small internal team rather than a public product."""

from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from . import config

_bearer = HTTPBearer()

# bcrypt directly, not passlib's CryptContext wrapper: passlib 1.7.4's
# bcrypt version-detection shim is broken against bcrypt>=4.1 (a known
# upstream incompatibility - it misreads the library's own self-test and
# fails every hash/verify call). Talking to bcrypt directly sidesteps it.
# bcrypt itself silently ignores anything past 72 bytes of input, so a
# very long pasted password is truncated rather than rejected or hashed
# incompletely - fine at DES-password length, not fine to rely on for
# anything security-critical beyond this internal tool.


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode()[:72], bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode()[:72], password_hash.encode())


def create_token(user_id: int, email: str) -> str:
    payload = {
        "sub": str(user_id), "email": email,
        "exp": datetime.now(timezone.utc) + timedelta(hours=config.JWT_EXPIRE_HOURS),
    }
    return jwt.encode(payload, config.JWT_SECRET, algorithm=config.JWT_ALGORITHM)


def decode_token(token: str) -> int:
    try:
        payload = jwt.decode(token, config.JWT_SECRET, algorithms=[config.JWT_ALGORITHM])
        return int(payload["sub"])
    except (JWTError, KeyError, ValueError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")


def current_user_id(creds: HTTPAuthorizationCredentials = Depends(_bearer)) -> int:
    return decode_token(creds.credentials)
