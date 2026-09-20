"""OIDC authentication, normalized identity claims, and role checks."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
from typing import Callable, Mapping

import jwt
from jwt import InvalidTokenError, PyJWKClient


ROLE_LEVELS = {"viewer": 1, "auditor": 2, "admin": 3}


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
    def acl_roles(self) -> tuple[str, ...]:
        implied = set(self.roles)
        if "admin" in implied:
            implied.update({"auditor", "viewer"})
        elif "auditor" in implied:
            implied.add("viewer")
        return tuple(sorted(implied))

    @property
    def acl_groups(self) -> tuple[str, ...]:
        return tuple(sorted(self.groups))


def subject_identifier(subject: str, salt: str) -> str:
    value = (subject or "").strip()
    if not value:
        raise AuthenticationError("subject_missing")
    return hmac.new(salt.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()


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
                 key_resolver: Callable[[str], object] | None = None):
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

    def healthcheck(self) -> tuple[bool, str]:
        if not self.subject_salt:
            return False, "auth_subject_salt_missing"
        if self.mode in {"development", "trusted_headers"}:
            return True, "ok"
        if not self.issuer:
            return False, "oidc_issuer_missing"
        if not self.audience:
            return False, "oidc_audience_missing"
        if not self.jwks_url.startswith("https://"):
            return False, "oidc_jwks_url_invalid"
        return True, "ok"

    def authenticate(self, headers: Mapping[str, str]) -> Principal:
        if self.mode == "development":
            return self._principal("local-development", {"viewer", "auditor", "admin"}, {"local"})
        if self.mode == "trusted_headers":
            subject = headers.get("x-auth-subject", "").strip()
            roles = _claim_values({"roles": headers.get("x-auth-roles", "")}, "roles")
            groups = _claim_values({"groups": headers.get("x-auth-groups", "")}, "groups")
            if not subject:
                raise AuthenticationError("credentials_missing")
            return self._principal(subject, roles, groups, headers.get("x-auth-name", ""))
        authorization = headers.get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise AuthenticationError("credentials_missing")
        return self._authenticate_token(token.strip())

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
        normalized_roles = frozenset(str(role).strip().lower() for role in roles) & ROLE_LEVELS.keys()
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
