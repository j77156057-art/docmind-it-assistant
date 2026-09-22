"""OIDC authentication, normalized identity claims, and role checks."""
from __future__ import annotations

from dataclasses import dataclass
import base64
import hashlib
import hmac
import time
import secrets
from typing import Callable, Mapping
from urllib.parse import urlencode

import httpx
import jwt
from jwt import InvalidTokenError


ROLE_LEVELS = {"viewer": 1, "auditor": 2, "admin": 3}

# -- Document classification ---------------------------------------------------
#
# `classification` is a label stored on the document itself (public / internal / confidential).
# It takes part in authorization in exactly one direction: a higher classification can only
# *narrow* who may read the document, never widen it. Widening remains the job of `access_scope`
# and the document ACL, so a confidential document marked `public` is still unreadable to a
# principal without clearance.
PUBLIC = "public"
INTERNAL = "internal"
CONFIDENTIAL = "confidential"
CLASSIFICATIONS = (PUBLIC, INTERNAL, CONFIDENTIAL)
OPEN_CLASSIFICATIONS = frozenset({PUBLIC, INTERNAL})
# A higher rank is stricter. The rank is only used to decide whether a label change would be a
# relaxation: raising a classification is allowed, lowering it is not.
CLASSIFICATION_RANK = {PUBLIC: 0, INTERNAL: 1, CONFIDENTIAL: 2}

# Reading a confidential document requires this explicit clearance. Level 2 (auditor) is the
# lowest level that already reads documents, which makes it the natural clearance floor.
# Knowledge-governance roles stay at level 0 and are unaffected: they hold no `query.read`, so
# they never reach the retrieval API, and they read documents through the capability-checked
# admin surface instead.
CONFIDENTIAL_CLEARANCE = "document.read.confidential"

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
    CONFIDENTIAL_CLEARANCE: 2,
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

def normalize_classification(classification: str | None) -> str:
    """Canonical form of a classification label. Rejects anything unknown, so a typo fails at
    the import boundary instead of silently landing in the database as an unenforced label."""
    normalized = str(classification or "").strip().lower()
    if normalized not in CLASSIFICATIONS:
        raise ValueError("文档密级无效")
    return normalized


def is_open_classification(classification: str | None) -> bool:
    """True when the label is readable by any authenticated principal.

    Anything that is not an explicitly open label fails closed, so an unknown or malformed value
    restricts a document rather than leaking it. The retrieval SQL builds its predicate from
    ``OPEN_CLASSIFICATIONS`` for the same reason.
    """
    return str(classification or "").strip().lower() in OPEN_CLASSIFICATIONS


def allows_classification(classification: str | None, capabilities) -> bool:
    """Whether a principal holding ``capabilities`` may read a document of this classification.

    This is an *extra* restriction, never a grant: callers still have to satisfy the document
    ACL before a document becomes readable.
    """
    return is_open_classification(classification) or CONFIDENTIAL_CLEARANCE in capabilities


SESSION_COOKIE = "docmind_session"
# Transient cookie holding the PKCE verifier, the CSRF state and the nonce between the redirect to
# the identity provider and the callback. It cannot be `SameSite=Strict`: the browser reaches the
# callback through a cross-site redirect, so a Strict cookie would be withheld and every login
# would fail its state check. It is short-lived, single-use, and typed so it can never be accepted
# as a session.
OIDC_FLOW_COOKIE = "docmind_oidc_flow"
OIDC_FLOW_SECONDS = 600
PKCE_CHALLENGE_METHOD = "S256"
# Signing keys rotate on the provider's schedule, not ours, so the JWKS document is cached for a
# bounded window and refetched once when an unknown key id shows up.
JWKS_CACHE_SECONDS = 300
SESSION_TYPE = "session"
FLOW_TYPE = "flow"


def _session_signing_key(salt: str) -> bytes:
    return hashlib.sha256(salt.encode("utf-8")).digest()


def _cookie_value(header: str, name: str) -> str:
    for part in header.split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value.strip()
    return ""


def _base64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def create_code_verifier() -> str:
    """RFC 7636 verifier: 43-128 characters drawn from the unreserved set."""
    return _base64url(secrets.token_bytes(48))


def code_challenge_for(verifier: str) -> str:
    """S256 challenge. `plain` is deliberately not offered: it discards the protection PKCE
    exists to provide against authorization-code interception."""
    return _base64url(hashlib.sha256(verifier.encode("ascii")).digest())


@dataclass(frozen=True)
class ProviderEndpoints:
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str


@dataclass(frozen=True)
class AuthorizationRequest:
    """Everything the callback needs to verify the response, plus the URL to send the browser to."""

    authorization_url: str
    state: str
    nonce: str
    code_verifier: str


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
    departments: frozenset[str] = frozenset()
    oidc_sub: str | None = None

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
    def confidential_clearance(self) -> bool:
        """Whether this principal may read documents classified `confidential`.

        Callers pass this to retrieval as an explicit flag; classification is an additional
        restriction, so `False` only ever hides documents — it never unlocks any.
        """
        return CONFIDENTIAL_CLEARANCE in self.capabilities

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
                 subject_salt: str, session_secret: str = "",
                 role_claim: str = "roles", group_claim: str = "groups",
                 department_claim: str = "department",
                 algorithms: tuple[str, ...] = ("RS256",), leeway_seconds: int = 30,
                 key_resolver: Callable[[str], object] | None = None,
                 local_username: str = "admin", local_password_hash: str = "",
                 local_display_name: str = "本地管理员", local_session_hours: int = 12,
                 local_roles: tuple[str, ...] = ("admin", "auditor", "viewer"),
                 guest_session_hours: int = 2,
                 client_id: str = "", client_secret: str = "", redirect_uri: str = "",
                 scopes: tuple[str, ...] = ("openid",), session_hours: int = 12,
                 timeout_seconds: float = 10.0, authorization_endpoint: str = "",
                 token_endpoint: str = "", end_session_url: str = "",
                 http_transport: httpx.BaseTransport | None = None):
        self.mode = mode
        self.issuer = issuer.rstrip("/")
        self.audience = audience
        self.jwks_url = jwks_url
        self.subject_salt = subject_salt
        # Session JWT signing key. Falls back to the subject HMAC salt when unset so existing
        # deployments keep validating sessions; set IT_AUTH_SESSION_SECRET to split the two apart.
        self.session_secret = session_secret or subject_salt
        self.role_claim = role_claim
        self.group_claim = group_claim
        self.department_claim = department_claim
        self.algorithms = algorithms
        self.leeway_seconds = leeway_seconds
        self._key_resolver = key_resolver
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()
        self.redirect_uri = redirect_uri.strip()
        self.scopes = tuple(scopes) or ("openid",)
        self.session_hours = max(1, min(session_hours, 168))
        self.timeout_seconds = timeout_seconds
        self.authorization_endpoint = authorization_endpoint.strip()
        self.token_endpoint = token_endpoint.strip()
        self.end_session_url = end_session_url.strip()
        self._http_transport = http_transport
        self._endpoints_cache: ProviderEndpoints | None = None
        self._jwks_cache: tuple[float, dict] | None = None
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
        if self.jwks_url and not self.jwks_url.startswith("https://"):
            return False, "oidc_jwks_url_invalid"
        if not self.client_id:
            return False, "oidc_client_id_missing"
        if not self.redirect_uri:
            return False, "oidc_redirect_uri_missing"
        return True, "ok"

    @property
    def login_enabled(self) -> bool:
        """Whether this instance can run the browser login flow at all."""
        return self.mode == "oidc" and bool(self.client_id and self.redirect_uri and self.issuer)

    def begin_authorization(self) -> AuthorizationRequest:
        """Start an authorization-code + PKCE login.

        The verifier never leaves the service: only its S256 challenge travels to the identity
        provider, so an intercepted authorization code cannot be redeemed without the cookie.
        """
        if not self.login_enabled:
            raise AuthenticationError("oidc_login_not_configured")
        endpoints = self.endpoints()
        if not endpoints.authorization_endpoint:
            raise AuthenticationError("oidc_authorization_endpoint_missing")
        state = secrets.token_urlsafe(24)
        nonce = secrets.token_urlsafe(24)
        verifier = create_code_verifier()
        query = urlencode({
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(self.scopes),
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge_for(verifier),
            "code_challenge_method": PKCE_CHALLENGE_METHOD,
        })
        separator = "&" if "?" in endpoints.authorization_endpoint else "?"
        return AuthorizationRequest(
            authorization_url=f"{endpoints.authorization_endpoint}{separator}{query}",
            state=state, nonce=nonce, code_verifier=verifier,
        )

    def issue_flow_token(self, request: AuthorizationRequest) -> str:
        now = int(time.time())
        return jwt.encode({
            "typ": FLOW_TYPE, "state": request.state, "nonce": request.nonce,
            "code_verifier": request.code_verifier,
            "iat": now, "exp": now + OIDC_FLOW_SECONDS,
        }, _session_signing_key(self.session_secret), algorithm="HS256")

    def read_flow_token(self, token: str) -> dict:
        """Open the transient flow cookie. Every failure collapses to one error: the caller is an
        unauthenticated browser, which should not learn which part of the flow was malformed."""
        if not token:
            raise AuthenticationError("login_state_missing")
        try:
            claims = jwt.decode(
                token, _session_signing_key(self.session_secret), algorithms=["HS256"],
                options={"require": ["exp", "iat", "state", "nonce", "code_verifier"]},
            )
        except InvalidTokenError:
            raise AuthenticationError("login_state_invalid") from None
        if str(claims.get("typ") or "") != FLOW_TYPE:
            raise AuthenticationError("login_state_invalid")
        return claims

    def complete_authorization(self, *, code: str, state: str, flow: Mapping
                               ) -> tuple[str, Principal]:
        """Redeem the authorization code and return the verified principal.

        The `state` comparison lives here rather than in the route so every caller is forced
        through it: the provider echoes `state` back untouched, and a mismatch means this response
        did not originate from the request we issued.
        """
        if not code.strip():
            raise AuthenticationError("authorization_code_missing")
        expected_state = str(flow.get("state") or "")
        if not expected_state or not hmac.compare_digest(str(state or ""), expected_state):
            raise AuthenticationError("login_state_mismatch")
        endpoints = self.endpoints()
        if not endpoints.token_endpoint:
            raise AuthenticationError("oidc_token_endpoint_missing")
        payload = {
            "grant_type": "authorization_code",
            "code": code.strip(),
            "redirect_uri": self.redirect_uri,
            "client_id": self.client_id,
            "code_verifier": str(flow.get("code_verifier") or ""),
        }
        try:
            with httpx.Client(timeout=self.timeout_seconds,
                              transport=self._http_transport) as client:
                response = client.post(
                    endpoints.token_endpoint, data=payload,
                    auth=(self.client_id, self.client_secret) if self.client_secret else None,
                )
        except httpx.TransportError:
            raise AuthenticationError("identity_provider_unavailable") from None
        if response.status_code != 200:
            raise AuthenticationError("authorization_code_rejected")
        try:
            tokens = response.json()
        except ValueError:
            raise AuthenticationError("identity_provider_invalid_response") from None
        id_token = str(tokens.get("id_token") or "")
        if not id_token:
            raise AuthenticationError("id_token_missing")
        # The id_token is issued to the *client*, so its audience is the client id -- the API
        # audience only ever applies to access tokens presented on the API surface.
        claims = self._verify_token(
            id_token, audience=self.client_id, expected_nonce=str(flow.get("nonce") or ""),
        )
        return self.issue_oidc_session(claims)

    def logout_url(self) -> str:
        """RP-initiated logout target, or empty when the provider publishes none."""
        return self.end_session_url

    def endpoints(self) -> ProviderEndpoints:
        """Resolve the provider endpoints, preferring explicit configuration.

        Discovery is the default because it makes swapping identity providers a configuration-only
        change; the explicit overrides exist for on-premise providers that publish no document.
        """
        needs_discovery = not (
            self.authorization_endpoint and self.token_endpoint and self.jwks_url
        )
        if needs_discovery and self._endpoints_cache is None:
            document = self._get_json(f"{self.issuer}/.well-known/openid-configuration")
            self._endpoints_cache = ProviderEndpoints(
                authorization_endpoint=str(document.get("authorization_endpoint") or ""),
                token_endpoint=str(document.get("token_endpoint") or ""),
                jwks_uri=str(document.get("jwks_uri") or ""),
            )
        discovered = self._endpoints_cache or ProviderEndpoints("", "", "")
        return ProviderEndpoints(
            self.authorization_endpoint or discovered.authorization_endpoint,
            self.token_endpoint or discovered.token_endpoint,
            self.jwks_url or discovered.jwks_uri,
        )

    def jwks_document(self, *, force: bool = False) -> dict:
        now = time.time()
        if not force and self._jwks_cache and now < self._jwks_cache[0]:
            return self._jwks_cache[1]
        uri = self.jwks_url or self.endpoints().jwks_uri
        if not uri:
            raise AuthenticationError("oidc_jwks_url_invalid")
        document = self._get_json(uri)
        self._jwks_cache = (now + JWKS_CACHE_SECONDS, document)
        return document

    def _get_json(self, url: str) -> dict:
        try:
            with httpx.Client(timeout=self.timeout_seconds,
                              transport=self._http_transport) as client:
                response = client.get(url, headers={"accept": "application/json"})
                response.raise_for_status()
                document = response.json()
        except (httpx.HTTPError, ValueError):
            raise AuthenticationError("identity_provider_unavailable") from None
        if not isinstance(document, dict):
            raise AuthenticationError("identity_provider_invalid_response")
        return document

    def _signing_key(self, token: str, algorithm: str) -> object:
        if self._key_resolver:
            return self._key_resolver(token)
        kid = str(jwt.get_unverified_header(token).get("kid") or "")
        jwk = self._select_jwk(kid) or self._select_jwk(kid, force=True)
        if jwk is None:
            raise AuthenticationError("token_key_not_found")
        try:
            return jwt.PyJWK(jwk, algorithm=algorithm).key
        except Exception:
            raise AuthenticationError("token_key_invalid") from None

    def _select_jwk(self, kid: str, *, force: bool = False) -> dict | None:
        keys = self.jwks_document(force=force).get("keys") or []
        for candidate in keys:
            if not isinstance(candidate, Mapping):
                continue
            if kid and str(candidate.get("kid") or "") != kid:
                continue
            if str(candidate.get("use") or "sig") != "sig":
                continue
            return dict(candidate)
        return None

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
            session = _cookie_value(headers.get("cookie", ""), SESSION_COOKIE)
            authorization = headers.get("authorization", "")
            bearer = authorization.partition(" ")[2].strip()
            return self._authenticate_session_token(session or bearer)
        if self.mode == "oidc":
            session = _cookie_value(headers.get("cookie", ""), SESSION_COOKIE)
            if session:
                return self._authenticate_session_token(session)
            authorization = headers.get("authorization", "")
            scheme, _, token = authorization.partition(" ")
            token = token.strip()
            if scheme.lower() != "bearer" or not token:
                raise AuthenticationError("credentials_missing")
            # A Bearer header may carry either our own session token or a provider access token.
            # The algorithm is the cheap discriminator, and each branch still verifies in full.
            try:
                algorithm = jwt.get_unverified_header(token).get("alg", "")
            except InvalidTokenError:
                raise AuthenticationError("token_invalid") from None
            if algorithm == "HS256":
                return self._authenticate_session_token(token)
            return self._authenticate_token(token)
        authorization = headers.get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise AuthenticationError("credentials_missing")
        return self._authenticate_token(token.strip())

    def issue_session(self, *, subject: str, roles, groups, display_name: str = "",
                      hours: int | None = None) -> str:
        """Mint the session token for an already-verified principal.

        Every login path -- local password, guest, and the OIDC authorization code -- ends here, so
        the claim layout, the type marker and the expiry rule stay in one place instead of drifting
        apart across three near-copies.
        """
        now = int(time.time())
        lifetime = self.local_session_hours if hours is None else hours
        return jwt.encode({
            "typ": SESSION_TYPE,
            "sub": subject,
            "name": display_name,
            "roles": sorted({str(role).strip().lower() for role in roles if str(role).strip()}),
            "groups": sorted({str(g).strip().lower() for g in groups if str(g).strip()}),
            "iat": now, "exp": now + lifetime * 3600,
        }, _session_signing_key(self.session_secret), algorithm="HS256")

    def issue_oidc_session(self, claims: Mapping) -> tuple[str, Principal]:
        """Exchange verified id_token claims for our own session.

        The id_token is deliberately not reused as the session: it is validated once at login and
        never replayed on the request path, so the query service does not need the identity
        provider to be reachable for every request. The claims written into the session are the
        *normalized* ones, so the cookie never carries a role that would be dropped on read.
        """
        principal = self._principal_from_claims(claims)
        session = self.issue_session(
            subject=str(claims["sub"]), roles=principal.roles, groups=principal.groups,
            display_name=principal.display_name, hours=self.session_hours,
        )
        return session, principal

    def login(self, username: str, password: str) -> str:
        if not self.local_username or not verify_password(password, self.local_password_hash):
            raise AuthenticationError("credentials_invalid")
        if not hmac.compare_digest(username.strip(), self.local_username):
            raise AuthenticationError("credentials_invalid")
        return self.issue_session(
            subject=username.strip(), roles=self.local_roles, groups=("local",),
            display_name=self.local_display_name, hours=self.local_session_hours,
        )

    def guest_login(self) -> str:
        return self.issue_session(
            subject=f"guest:{secrets.token_urlsafe(18)}", roles=("viewer",), groups=("guest",),
            display_name="游客", hours=self.guest_session_hours,
        )

    def _authenticate_session_token(self, token: str) -> Principal:
        if not token:
            raise AuthenticationError("credentials_missing")
        try:
            claims = jwt.decode(
                token, _session_signing_key(self.session_secret), algorithms=["HS256"],
                options={"require": ["exp", "iat", "sub"]},
            )
        except InvalidTokenError:
            raise AuthenticationError("token_invalid") from None
        if str(claims.get("typ") or SESSION_TYPE) != SESSION_TYPE:
            # The PKCE flow cookie is signed with the same key, so the type marker is what keeps a
            # transient login artefact from ever being accepted as a session.
            raise AuthenticationError("token_invalid")
        return self._principal(str(claims["sub"]), _claim_values(claims, "roles"),
                               _claim_values(claims, "groups"), str(claims.get("name") or ""))

    def _authenticate_token(self, token: str, *, audience: str | None = None,
                            expected_nonce: str = "") -> Principal:
        return self._principal_from_claims(
            self._verify_token(token, audience=audience, expected_nonce=expected_nonce)
        )

    def _verify_token(self, token: str, *, audience: str | None = None,
                      expected_nonce: str = "") -> dict:
        try:
            algorithm = jwt.get_unverified_header(token).get("alg", "")
            if algorithm not in self.algorithms:
                raise AuthenticationError("token_algorithm_rejected")
            claims = jwt.decode(
                token,
                key=self._signing_key(token, algorithm),
                algorithms=list(self.algorithms),
                audience=audience or self.audience,
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
        if expected_nonce and not hmac.compare_digest(
            str(claims.get("nonce") or ""), expected_nonce
        ):
            # The nonce binds this id_token to the authorization request we issued; without the
            # check, a token minted for a different login could be replayed into this one.
            raise AuthenticationError("id_token_nonce_mismatch")
        return claims

    def _principal_from_claims(self, claims: Mapping) -> Principal:
        departments = _claim_values(claims, self.department_claim)
        # oidc_sub carries the app-internal (hashed) identity, never the raw IdP ``sub`` claim,
        # so the original subject value never leaks into logs or ``repr``.
        oidc_sub = subject_identifier(str(claims["sub"]), self.subject_salt)
        return self._principal(
            str(claims["sub"]), _claim_values(claims, self.role_claim),
            _claim_values(claims, self.group_claim), self._display_name(claims),
            departments=departments, oidc_sub=oidc_sub,
        )

    @staticmethod
    def _display_name(claims: Mapping) -> str:
        return str(claims.get("name") or claims.get("preferred_username") or "")[:128]

    def _principal(self, subject: str, roles, groups, display_name: str = "",
                   departments: frozenset[str] = frozenset(),
                   oidc_sub: str | None = None) -> Principal:
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
            departments=departments,
            oidc_sub=oidc_sub,
        )
