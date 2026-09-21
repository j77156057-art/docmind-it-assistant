"""OIDC authentication, normalized identity claims, and role checks."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import time
import secrets
from typing import Callable, Mapping

import jwt
from jwt import InvalidTokenError, PyJWKClient


ROLE_LEVELS = {"viewer": 1, "auditor": 2, "admin": 3}

# Knowledge-governance roles are deliberately NOT levels. Making review or publish a higher
# level than admin would let one role inherit the other's duties, which makes separation of
# duties impossible to enforce. They grant capabilities instead.
GOVERNANCE_ROLES = frozenset({
    "knowledge_editor", "knowledge_reviewer", "knowledge_publisher", "evaluation_runner",
})
KNOWN_ROLES = frozenset(ROLE_LEVELS) | GOVERNANCE_ROLES

# Capabilities granted by role level. Every entry is an explicit minimum level.
LEVEL_CAPABILITIES = {
    "query.read": 1,
    "history.self.read": 1,
    "document.read": 2,
    "audit.read": 2,
    "usage.read": 2,
    "artifact.read": 2,
    "document.write": 3,
    "acl.write": 3,
    "model.write": 3,
    "artifact.write": 3,
    "governance.override": 3,
}

# Capabilities granted by an explicit governance role, independent of level.
# `admin` intentionally does NOT gain document.review/document.publish:
# it may only act through the audited override switch.
ROLE_CAPABILITIES = {
    "knowledge_editor": frozenset({"document.read", "document.write"}),
    "knowledge_reviewer": frozenset({"document.read", "document.review", "evaluation.run"}),
    "knowledge_publisher": frozenset({
        "document.read", "document.publish", "document.withdraw", "document.rollback",
    }),
    "evaluation_runner": frozenset({"document.read", "evaluation.run"}),
}

SESSION_COOKIE = "docmind_session"


class AuthenticationError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Principal:
    subject_id: str
    roles: frozenset[str]
    groups: frozenset[str]
    display_name: str = ""

    def allows(self, required_role: str) -> bool:
        required = ROLE_LEVELS.get(required_role, 99)
        return any(ROLE_LEVELS.get(role, 0) >= required for role in self.roles)

    @property
    def capabilities(self) -> frozenset[str]:
        granted = {
            capability
            for capability, level in LEVEL_CAPABILITIES.items()
            if any(ROLE_LEVELS.get(role, 0) >= level for role in self.roles)
        }
        for role in self.roles:
            granted |= ROLE_CAPABILITIES.get(role, frozenset())
        return frozenset(granted)

    def has_capability(self, capability: str) -> bool:
        return capability in self.capabilities

    @property
    def acl_roles(self) -> tuple[str, ...]:
        if "guest" in self.groups:
            return ()
        # Governance roles never widen document visibility: only level roles are ACL subjects.
        implied = {role for role in self.roles if role in ROLE_LEVELS}
        if "admin" in implied:
            implied.update({"auditor", "viewer"})
        elif "auditor" in implied:
            implied.add("viewer")
        return tuple(sorted(implied))

    @property
    def acl_groups(self) -> tuple[str, ...]:
        if "guest" in self.groups:
            return ()
        return tuple(sorted(self.groups))


def subject_identifier(subject: str, salt: str) -> str:
    value = (subject or "").strip()
    if not value:
        raise AuthenticationError("subject_missing")
    return hmac.new(salt.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()


def hash_password(password: str, *, iterations: int = 260_000) -> str:
    if not password or len(password) < 8:
        raise ValueError("密码至少需要 8 个字符")
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations)
    return f"pbkdf2_sha256${iterations}${salt}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(rounds))
        return hmac.compare_digest(actual.hex(), expected)
    except (AttributeError, TypeError, ValueError):
        return False


def _claim_values(claims: Mapping, path: str) -> frozenset[str]:
    value = claims
    for part in (path or "").split("."):
        if not part or not isinstance(value, Mapping):
            return frozenset()
        value = value.get(part)
    if isinstance(value, str):
        values = value.replace(",", " ").split()
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        return frozenset()
    return frozenset(str(item).strip().lower() for item in values if str(item).strip())


class OIDCAuthenticator:
    def __init__(self, *, mode: str, issuer: str, audience: str, jwks_url: str,
                 subject_salt: str, role_claim: str = "roles", group_claim: str = "groups",
                 algorithms: tuple[str, ...] = ("RS256",), leeway_seconds: int = 30,
                 key_resolver: Callable[[str], object] | None = None,
                 local_username: str = "admin", local_password_hash: str = "",
                 local_display_name: str = "本地管理员", local_session_hours: int = 12,
                 local_roles: tuple[str, ...] = ("admin", "auditor", "viewer"),
                 guest_session_hours: int = 2):
        self.mode = mode
        self.issuer = issuer.rstrip("/")
        self.audience = audience
        self.jwks_url = jwks_url
        self.subject_salt = subject_salt
        self.role_claim = role_claim
        self.group_claim = group_claim
        self.algorithms = algorithms
        self.leeway_seconds = leeway_seconds
        self._key_resolver = key_resolver
        self._jwks_client = PyJWKClient(jwks_url, cache_keys=True) if jwks_url else None
        self.local_username = local_username.strip()
        self.local_password_hash = local_password_hash.strip()
        self.local_display_name = local_display_name.strip()[:128]
        normalized_local_roles = tuple(
            role for role in (str(item).strip().lower() for item in local_roles)
            if role in KNOWN_ROLES
        )
        self.local_roles = normalized_local_roles or ("viewer",)
        self.local_session_hours = max(1, min(local_session_hours, 168))
        self.guest_session_hours = max(1, min(guest_session_hours, 24))

    def healthcheck(self) -> tuple[bool, str]:
        if not self.subject_salt:
            return False, "auth_subject_salt_missing"
        if self.mode in {"development", "trusted_headers"}:
            return True, "ok"
        if self.mode == "local":
            return (True, "ok") if self.local_username and self.local_password_hash else (False, "local_credentials_missing")
        if not self.issuer:
            return False, "oidc_issuer_missing"
        if not self.audience:
            return False, "oidc_audience_missing"
        if not self.jwks_url.startswith("https://"):
            return False, "oidc_jwks_url_invalid"
        return True, "ok"

    def authenticate(self, headers: Mapping[str, str]) -> Principal:
        if self.mode == "development":
            return self._principal("local-development", KNOWN_ROLES, {"local"})
        if self.mode == "trusted_headers":
            subject = headers.get("x-auth-subject", "").strip()
            roles = _claim_values({"roles": headers.get("x-auth-roles", "")}, "roles")
            groups = _claim_values({"groups": headers.get("x-auth-groups", "")}, "groups")
            if not subject:
                raise AuthenticationError("credentials_missing")
            return self._principal(subject, roles, groups, headers.get("x-auth-name", ""))
        if self.mode == "local":
            cookie = headers.get("cookie", "")
            token = next((part.split("=", 1)[1] for part in cookie.split(";")
                          if part.strip().startswith(f"{SESSION_COOKIE}=")), "")
            authorization = headers.get("authorization", "")
            bearer = authorization.partition(" ")[2].strip()
            return self._authenticate_local_token(token or bearer)
        authorization = headers.get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise AuthenticationError("credentials_missing")
        return self._authenticate_token(token.strip())

    def login(self, username: str, password: str) -> str:
        if not self.local_username or not verify_password(password, self.local_password_hash):
            raise AuthenticationError("credentials_invalid")
        if not hmac.compare_digest(username.strip(), self.local_username):
            raise AuthenticationError("credentials_invalid")
        now = int(time.time())
        signing_key = hashlib.sha256(self.subject_salt.encode("utf-8")).digest()
        return jwt.encode({
            "sub": username.strip(), "name": self.local_display_name,
            "roles": list(self.local_roles), "groups": ["local"],
            "iat": now, "exp": now + self.local_session_hours * 3600,
        }, signing_key, algorithm="HS256")

    def guest_login(self) -> str:
        now = int(time.time())
        signing_key = hashlib.sha256(self.subject_salt.encode("utf-8")).digest()
        return jwt.encode({
            "sub": f"guest:{secrets.token_urlsafe(18)}", "name": "游客",
            "roles": ["viewer"], "groups": ["guest"],
            "iat": now, "exp": now + self.guest_session_hours * 3600,
        }, signing_key, algorithm="HS256")

    def _authenticate_local_token(self, token: str) -> Principal:
        if not token:
            raise AuthenticationError("credentials_missing")
        try:
            signing_key = hashlib.sha256(self.subject_salt.encode("utf-8")).digest()
            claims = jwt.decode(token, signing_key, algorithms=["HS256"],
                                options={"require": ["exp", "iat", "sub"]})
        except InvalidTokenError:
            raise AuthenticationError("token_invalid") from None
        return self._principal(str(claims["sub"]), _claim_values(claims, "roles"),
                               _claim_values(claims, "groups"), str(claims.get("name") or ""))

    def _authenticate_token(self, token: str) -> Principal:
        try:
            algorithm = jwt.get_unverified_header(token).get("alg", "")
            if algorithm not in self.algorithms:
                raise AuthenticationError("token_algorithm_rejected")
            if self._key_resolver:
                key = self._key_resolver(token)
            elif self._jwks_client:
                key = self._jwks_client.get_signing_key_from_jwt(token).key
            else:
                raise AuthenticationError("oidc_jwks_url_invalid")
            claims = jwt.decode(
                token,
                key=key,
                algorithms=list(self.algorithms),
                audience=self.audience,
                issuer=self.issuer,
                leeway=self.leeway_seconds,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except AuthenticationError:
            raise
        except InvalidTokenError:
            raise AuthenticationError("token_invalid") from None
        except Exception:
            raise AuthenticationError("identity_provider_unavailable") from None
        roles = _claim_values(claims, self.role_claim)
        groups = _claim_values(claims, self.group_claim)
        name = str(claims.get("name") or claims.get("preferred_username") or "")[:128]
        return self._principal(str(claims["sub"]), roles, groups, name)

    def _principal(self, subject: str, roles, groups, display_name: str = "") -> Principal:
        normalized_roles = frozenset(
            str(role).strip().lower() for role in roles
        ) & KNOWN_ROLES
        normalized_groups = frozenset(
            str(group).strip().lower() for group in groups if str(group).strip()
        )
        if len(normalized_groups) > 256 or any(len(group) > 256 for group in normalized_groups):
            raise AuthenticationError("claims_invalid")
        if not self.subject_salt:
            raise AuthenticationError("auth_subject_salt_missing")
        return Principal(
            subject_id=subject_identifier(subject, self.subject_salt),
            roles=normalized_roles,
            groups=normalized_groups,
            display_name=display_name[:128],
        )
