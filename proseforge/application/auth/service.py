from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from passlib.context import CryptContext


@dataclass(frozen=True)
class AuthUser:
    id: str
    email: str
    role: str = "USER"
    session_version: int = 1


class AuthService:
    def __init__(self, jwt_secret: str, token_minutes: int = 60):
        self.jwt_secret = jwt_secret
        self.token_minutes = token_minutes
        self.passwords = CryptContext(schemes=["argon2"], deprecated="auto")

    def hash_password(self, password: str) -> str:
        if len(password) < 12:
            raise ValueError("password must be at least 12 characters")
        return self.passwords.hash(password)

    def verify_password(self, password: str, password_hash: str) -> bool:
        return self.passwords.verify(password, password_hash)

    def issue_token(self, user: AuthUser) -> str:
        now = datetime.now(UTC)
        return jwt.encode({"sub": user.id, "email": user.email, "role": user.role, "sv": user.session_version, "iat": now, "exp": now + timedelta(minutes=self.token_minutes)}, self.jwt_secret, algorithm="HS256")

    def decode_token(self, token: str) -> AuthUser:
        payload = jwt.decode(token, self.jwt_secret, algorithms=["HS256"])
        if not payload.get("sub") or not payload.get("email"):
            raise ValueError("invalid session token")
        return AuthUser(str(payload["sub"]), str(payload["email"]), str(payload.get("role", "USER")), int(payload.get("sv", 0)))
