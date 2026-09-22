"""OIDC authorization-code + PKCE login.

CI has no identity provider to talk to, so the provider is stubbed rather than mocked: a real RSA
key pair, a real discovery document and a real token endpoint served over `httpx.MockTransport`.
The stub recomputes the S256 challenge from the `code_verifier` it receives, which is what makes
these tests evidence that the verifier is actually wired through -- not merely that a cookie came
out the far end.
"""
import base64
from datetime import datetime, timedelta, timezone
import tempfile
from pathlib import Path
import unittest
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
import httpx
import jwt
from pydantic import ValidationError

from admin_app import create_admin_app
from app import create_app
from backend import AppSettings
from backend.auth import OIDC_FLOW_COOKIE, SESSION_COOKIE, code_challenge_for


ISSUER = "https://identity.example.com"
AUTHORIZATION_ENDPOINT = f"{ISSUER}/authorize"
TOKEN_ENDPOINT = f"{ISSUER}/token"
JWKS_URL = f"{ISSUER}/jwks"
REDIRECT_URI = "https://docmind.example.com/api/auth/oidc/callback"
CLIENT_ID = "docmind-portal"
API_AUDIENCE = "docmind-api"
SUBJECT_SALT = "unit-test-subject-salt"
GOOD_CODE = "good-code"
# Secret the stub uses only for the algorithm-confusion case: the service must reject the token
# on the `alg` header alone, before any signature check, so the value here is irrelevant.
HS256_SECRET = "stub-hs256-secret-for-algorithm-confusion-rejection"


def _b64url_int(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class StubIdentityProvider:
    """A minimal OpenID Provider that is strict about everything the service must get right."""

    def __init__(self, *, kid: str = "key-1"):
        self.kid = kid
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.paths: list[str] = []
        self.token_forms: list[dict] = []
        # Knobs the individual tests turn to reproduce a misbehaving or hostile provider.
        self.token_status = 200
        self.audience_override: str | None = None
        self.nonce_override: str | None = None
        self.sign_with_other_key = False
        self.algorithm = "RS256"
        self.expected_challenge = ""
        self.nonce = ""
        self.state = ""
        self.claims: dict = {
            "sub": "alice@example.com", "name": "Alice",
            "roles": ["viewer", "auditor"], "groups": ["it-support"],
        }

    def expect_authorization(self, query: dict) -> None:
        """Record what the service asked for, so the token endpoint can hold it to account."""
        self.expected_challenge = query["code_challenge"][0]
        self.nonce = query["nonce"][0]
        self.state = query["state"][0]

    def jwks(self) -> dict:
        numbers = self.key.public_key().public_numbers()
        return {"keys": [{
            "kty": "RSA", "use": "sig", "alg": "RS256", "kid": self.kid,
            "n": _b64url_int(numbers.n), "e": _b64url_int(numbers.e),
        }]}

    def token_for(self, *, audience: str, with_nonce: str | None) -> str:
        now = datetime.now(timezone.utc)
        claims = {
            "iss": ISSUER, "aud": self.audience_override or audience, "iat": now,
            "exp": now + timedelta(minutes=5), **self.claims,
        }
        if with_nonce is not None:
            claims["nonce"] = self.nonce_override or with_nonce
        key = self.other_key if self.sign_with_other_key else self.key
        if self.algorithm == "HS256":
            return jwt.encode(claims, HS256_SECRET, algorithm="HS256")
        return jwt.encode(claims, key, algorithm=self.algorithm, headers={"kid": self.kid})

    def access_token(self) -> str:
        return self.token_for(audience=API_AUDIENCE, with_nonce=None)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json={
                "issuer": ISSUER,
                "authorization_endpoint": AUTHORIZATION_ENDPOINT,
                "token_endpoint": TOKEN_ENDPOINT,
                "jwks_uri": JWKS_URL,
            })
        if request.url.path == "/jwks":
            return httpx.Response(200, json=self.jwks())
        if request.url.path == "/token":
            form = {key: value[0] for key, value in parse_qs(request.content.decode()).items()}
            self.token_forms.append(form)
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_grant"})
            if form.get("grant_type") != "authorization_code" or form.get("code") != GOOD_CODE:
                return httpx.Response(400, json={"error": "invalid_grant"})
            # This is the assertion that PKCE is real: a provider rejects the code unless the
            # verifier hashes back to the challenge issued with the authorization request.
            verifier = form.get("code_verifier", "")
            if not verifier or code_challenge_for(verifier) != self.expected_challenge:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={
                "access_token": "stub-access-token", "token_type": "Bearer",
                "id_token": self.token_for(audience=CLIENT_ID, with_nonce=self.nonce),
            })
        return httpx.Response(404, json={"error": "not_found"})


class OidcLoginTestBase(unittest.TestCase):
    def setUp(self):
        self.provider = StubIdentityProvider()

    def make_settings(self, root: str, **overrides) -> AppSettings:
        project = Path(root)
        knowledge = project / "knowledge.md"
        knowledge.write_text("# IT\n## VPN\n请重新登录 VPN。\n", encoding="utf-8")
        web = project / "web"
        web.mkdir(parents=True, exist_ok=True)
        (web / "index.html").write_text("<!doctype html>", encoding="utf-8")
        (web / "admin.html").write_text("<!doctype html>", encoding="utf-8")
        values = {
            "project_root": project,
            "environment": "test",
            "database_url": f"sqlite:///{(project / 'queries.db').as_posix()}",
            "knowledge_path": knowledge,
            "web_index_path": web / "index.html",
            "admin_index_path": web / "admin.html",
            "artifact_output_path": project / "artifacts",
            "auth_mode": "oidc",
            "auth_subject_salt": SUBJECT_SALT,
            "log_level": "CRITICAL",
            "oidc_issuer": ISSUER,
            "oidc_audience": API_AUDIENCE,
            "oidc_client_id": CLIENT_ID,
            "oidc_redirect_uri": REDIRECT_URI,
        }
        values.update(overrides)
        return AppSettings(**values)

    def start_login(self, client: TestClient) -> dict:
        """Kick off a login and hand the provider what the service asked for."""
        response = client.get("/api/auth/oidc/start", follow_redirects=False)
        self.assertEqual(response.status_code, 302, response.text)
        parsed = urlsplit(response.headers["location"])
        query = parse_qs(parsed.query)
        self.provider.expect_authorization(query)
        return {"location": parsed, "query": query, "response": response}

    def complete_login(self, client: TestClient):
        started = self.start_login(client)
        state = started["query"]["state"][0]
        return client.get(
            f"/api/auth/oidc/callback?code={GOOD_CODE}&state={state}", follow_redirects=False,
        )


class OidcAuthorizationRequestTests(OidcLoginTestBase):
    def test_start_redirects_with_pkce_parameters_and_a_scoped_flow_cookie(self):
        with tempfile.TemporaryDirectory() as root:
            application = create_app(self.make_settings(root), auth_transport=self.provider.transport())
            with TestClient(application) as client:
                started = self.start_login(client)

        query = started["query"]
        self.assertEqual(started["location"].netloc, "identity.example.com")
        self.assertEqual(started["location"].path, "/authorize")
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["client_id"], [CLIENT_ID])
        self.assertEqual(query["redirect_uri"], [REDIRECT_URI])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertIn("openid", query["scope"][0].split())
        # The verifier is what must never leave the service.
        self.assertNotIn("code_verifier", query)
        self.assertEqual(len(query["code_challenge"][0]), 43)

        cookie = started["response"].headers["set-cookie"].lower()
        self.assertIn(f"{OIDC_FLOW_COOKIE}=", cookie)
        self.assertIn("httponly", cookie)
        # Lax, not Strict: the callback arrives via a cross-site redirect and must carry this.
        self.assertIn("samesite=lax", cookie)
        self.assertIn("path=/api/auth/oidc", cookie)

    def test_explicit_endpoints_skip_discovery(self):
        with tempfile.TemporaryDirectory() as root:
            settings = self.make_settings(
                root, oidc_jwks_url=JWKS_URL,
                oidc_authorization_endpoint=AUTHORIZATION_ENDPOINT,
                oidc_token_endpoint=TOKEN_ENDPOINT,
            )
            application = create_app(settings, auth_transport=self.provider.transport())
            with TestClient(application) as client:
                self.complete_login(client)

        self.assertNotIn("/.well-known/openid-configuration", self.provider.paths)

    def test_start_is_unavailable_when_login_is_not_configured(self):
        with tempfile.TemporaryDirectory() as root:
            application = create_app(self.make_settings(root, auth_mode="trusted_headers"),
                                     auth_transport=self.provider.transport())
            with TestClient(application) as client:
                started = client.get("/api/auth/oidc/start", follow_redirects=False)
                callback = client.get("/api/auth/oidc/callback?code=x&state=y",
                                      follow_redirects=False)
                config = client.get("/api/auth/config")

        self.assertEqual(started.status_code, 404)
        self.assertEqual(callback.status_code, 404)
        self.assertFalse(config.json()["sso_login"])
        self.assertFalse(config.json()["login_required"])

    def test_config_reports_that_single_sign_on_is_available(self):
        with tempfile.TemporaryDirectory() as root:
            application = create_app(self.make_settings(root), auth_transport=self.provider.transport())
            with TestClient(application) as client:
                config = client.get("/api/auth/config")

        self.assertTrue(config.json()["sso_login"])
        self.assertTrue(config.json()["login_required"])
        self.assertFalse(config.json()["local_login"])
        self.assertFalse(config.json()["guest_enabled"])


class OidcCallbackTests(OidcLoginTestBase):
    def test_callback_exchanges_the_code_for_a_session(self):
        with tempfile.TemporaryDirectory() as root:
            application = create_app(self.make_settings(root), auth_transport=self.provider.transport())
            with TestClient(application) as client:
                callback = self.complete_login(client)
                session_cookie = callback.headers["set-cookie"].lower()
                me = client.get("/api/me")
                second_request = client.get("/api/me")

        self.assertEqual(callback.status_code, 302)
        self.assertEqual(callback.headers["location"], "/")
        self.assertIn(f"{SESSION_COOKIE}=", session_cookie)
        self.assertIn("httponly", session_cookie)
        self.assertIn("samesite=strict", session_cookie)
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["roles"], ["auditor", "viewer"])
        self.assertEqual(me.json()["groups"], ["it-support"])
        # The session is ours, not the provider's: the id_token is not replayed per request.
        self.assertEqual(len(self.provider.token_forms), 1)
        self.assertEqual(second_request.status_code, 200)
        self.assertEqual(len(self.provider.token_forms), 1)

    def test_session_carries_the_normalized_roles_only(self):
        self.provider.claims = {
            "sub": "alice@example.com", "name": "Alice",
            "roles": ["Viewer", "AUDITOR", "not-a-real-role"], "groups": ["IT-Support"],
        }
        with tempfile.TemporaryDirectory() as root:
            application = create_app(self.make_settings(root), auth_transport=self.provider.transport())
            with TestClient(application) as client:
                self.complete_login(client)
                me = client.get("/api/me")

        self.assertEqual(me.json()["roles"], ["auditor", "viewer"])
        self.assertEqual(me.json()["groups"], ["it-support"])

    def test_callback_rejects_a_mismatched_state(self):
        with tempfile.TemporaryDirectory() as root:
            application = create_app(self.make_settings(root), auth_transport=self.provider.transport())
            with TestClient(application) as client:
                self.start_login(client)
                callback = client.get(
                    f"/api/auth/oidc/callback?code={GOOD_CODE}&state=not-the-state",
                    follow_redirects=False,
                )

        self.assertEqual(callback.status_code, 401)
        self.assertEqual(self.provider.token_forms, [])

    def test_callback_rejects_a_replayed_nonce(self):
        self.provider.nonce_override = "nonce-from-another-login"
        with tempfile.TemporaryDirectory() as root:
            application = create_app(self.make_settings(root), auth_transport=self.provider.transport())
            with TestClient(application) as client:
                callback = self.complete_login(client)

        self.assertEqual(callback.status_code, 401)

    def test_callback_rejects_a_missing_flow_cookie(self):
        with tempfile.TemporaryDirectory() as root:
            application = create_app(self.make_settings(root), auth_transport=self.provider.transport())
            with TestClient(application) as client:
                started = self.start_login(client)
                client.cookies.delete(OIDC_FLOW_COOKIE)
                callback = client.get(
                    f"/api/auth/oidc/callback?code={GOOD_CODE}"
                    f"&state={started['query']['state'][0]}",
                    follow_redirects=False,
                )

        self.assertEqual(callback.status_code, 401)

    def test_callback_rejects_a_tampered_flow_cookie(self):
        with tempfile.TemporaryDirectory() as root:
            application = create_app(self.make_settings(root), auth_transport=self.provider.transport())
            with TestClient(application) as client:
                started = self.start_login(client)
                flow = client.cookies.get(OIDC_FLOW_COOKIE)
                replacement = "a" if flow[-1] != "a" else "b"
                client.cookies.set(OIDC_FLOW_COOKIE, flow[:-1] + replacement,
                                   path="/api/auth/oidc")
                callback = client.get(
                    f"/api/auth/oidc/callback?code={GOOD_CODE}"
                    f"&state={started['query']['state'][0]}",
                    follow_redirects=False,
                )

        self.assertEqual(callback.status_code, 401)
        self.assertEqual(self.provider.token_forms, [])

    def test_flow_cookie_is_never_accepted_as_a_session(self):
        with tempfile.TemporaryDirectory() as root:
            application = create_app(self.make_settings(root), auth_transport=self.provider.transport())
            with TestClient(application) as client:
                self.start_login(client)
                flow = client.cookies.get(OIDC_FLOW_COOKIE)
                client.cookies.set(SESSION_COOKIE, flow, path="/")
                me = client.get("/api/me")

        self.assertEqual(me.status_code, 401)

    def test_provider_error_is_surfaced_without_exchanging_a_code(self):
        with tempfile.TemporaryDirectory() as root:
            application = create_app(self.make_settings(root), auth_transport=self.provider.transport())
            with TestClient(application) as client:
                self.start_login(client)
                callback = client.get(
                    "/api/auth/oidc/callback?error=access_denied&error_description=user+said+no",
                    follow_redirects=False,
                )

        self.assertEqual(callback.status_code, 401)
        self.assertEqual(self.provider.token_forms, [])


class OidcIdTokenValidationTests(OidcLoginTestBase):
    def _callback_status(self, root: str) -> int:
        application = create_app(self.make_settings(root), auth_transport=self.provider.transport())
        with TestClient(application) as client:
            return self.complete_login(client).status_code

    def test_rejects_an_id_token_signed_by_the_wrong_key(self):
        self.provider.sign_with_other_key = True
        with tempfile.TemporaryDirectory() as root:
            status = self._callback_status(root)

        self.assertEqual(status, 401)

    def test_rejects_an_id_token_using_a_hs256_algorithm(self):
        self.provider.algorithm = "HS256"
        with tempfile.TemporaryDirectory() as root:
            status = self._callback_status(root)

        self.assertEqual(status, 401)

    def test_rejects_an_id_token_issued_for_another_audience(self):
        self.provider.audience_override = "some-other-client"
        with tempfile.TemporaryDirectory() as root:
            status = self._callback_status(root)

        self.assertEqual(status, 401)

    def test_rejects_a_code_the_provider_refuses(self):
        self.provider.token_status = 400
        with tempfile.TemporaryDirectory() as root:
            status = self._callback_status(root)

        self.assertEqual(status, 401)

    def test_accepts_a_provider_access_token_on_the_api_surface(self):
        with tempfile.TemporaryDirectory() as root:
            application = create_app(self.make_settings(root), auth_transport=self.provider.transport())
            with TestClient(application) as client:
                me = client.get(
                    "/api/me",
                    headers={"Authorization": f"Bearer {self.provider.access_token()}"},
                )

        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["roles"], ["auditor", "viewer"])

    def test_stale_jwks_is_refetched_when_the_key_id_is_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            application = create_app(self.make_settings(root), auth_transport=self.provider.transport())
            authenticator = application.state.authenticator
            with TestClient(application) as client:
                client.get("/api/me", headers={
                    "Authorization": f"Bearer {self.provider.access_token()}",
                })
                first_fetches = self.provider.paths.count("/jwks")
                self.provider.kid = "key-2"
                rotated = client.get("/api/me", headers={
                    "Authorization": f"Bearer {self.provider.access_token()}",
                })

        self.assertEqual(first_fetches, 1)
        # A rotation must not require a restart: the unknown key id triggers one refetch.
        self.assertEqual(rotated.status_code, 200)
        self.assertEqual(self.provider.paths.count("/jwks"), 2)
        self.assertEqual(authenticator.jwks_document()["keys"][0]["kid"], "key-2")


class OidcAdminSurfaceTests(OidcLoginTestBase):
    def test_admin_surface_runs_the_same_flow(self):
        with tempfile.TemporaryDirectory() as root:
            application = create_admin_app(self.make_settings(root),
                                           auth_transport=self.provider.transport())
            with TestClient(application) as client:
                callback = self.complete_login(client)
                me = client.get("/api/me")
                config = client.get("/api/auth/config")

        self.assertEqual(callback.status_code, 302)
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["roles"], ["auditor", "viewer"])
        self.assertTrue(config.json()["sso_login"])

    def test_admin_surface_rejects_a_mismatched_state(self):
        with tempfile.TemporaryDirectory() as root:
            application = create_admin_app(self.make_settings(root),
                                           auth_transport=self.provider.transport())
            with TestClient(application) as client:
                self.start_login(client)
                callback = client.get(
                    f"/api/auth/oidc/callback?code={GOOD_CODE}&state=not-the-state",
                    follow_redirects=False,
                )

        self.assertEqual(callback.status_code, 401)
        self.assertEqual(self.provider.token_forms, [])


class OidcSettingsTests(unittest.TestCase):
    def base(self, **overrides) -> dict:
        values = {
            "environment": "production",
            "database_url": "postgresql+psycopg://user:secret@db/docmind",
            "embedding_mode": "provider",
            "auth_mode": "oidc",
            "oidc_issuer": ISSUER,
            "oidc_audience": API_AUDIENCE,
            "auth_subject_salt": "a-production-subject-salt-at-least-32-characters",
            "query_field_key": "a-production-field-key-at-least-16-characters",
            "oidc_client_id": CLIENT_ID,
            "oidc_redirect_uri": REDIRECT_URI,
        }
        values.update(overrides)
        return values

    def test_browser_login_requires_a_client_id_and_a_redirect_uri(self):
        for missing in ("oidc_client_id", "oidc_redirect_uri"):
            with self.subTest(missing=missing):
                with self.assertRaises(ValidationError):
                    AppSettings(**self.base(**{missing: ""}))

    def test_redirect_uri_must_be_https_unless_it_loops_back(self):
        with self.assertRaises(ValidationError):
            AppSettings(**self.base(oidc_redirect_uri="http://docmind.example.com/callback"))
        # A developer on loopback may still exercise the flow without a certificate.
        AppSettings(**self.base(environment="test",
                                oidc_redirect_uri="http://127.0.0.1:8000/api/auth/oidc/callback"))

    def test_jwks_url_is_optional_because_discovery_can_supply_it(self):
        settings = AppSettings(**self.base())
        self.assertEqual(settings.oidc_jwks_url, "")

    def test_scopes_must_open_an_oidc_session(self):
        with self.assertRaises(ValidationError):
            AppSettings(**self.base(oidc_scopes="profile email"))
        settings = AppSettings(**self.base(oidc_scopes="openid,profile"))
        self.assertEqual(settings.oidc_scope_list, ("openid", "profile"))

    def test_neither_the_client_secret_nor_the_salt_leaks_into_repr(self):
        settings = AppSettings(**self.base(oidc_client_secret="client-secret-value"))
        self.assertNotIn("client-secret-value", repr(settings))
        self.assertNotIn("client-secret-value", str(settings.public()))
