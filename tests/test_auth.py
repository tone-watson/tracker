"""Real JWT signatures and isolated Farm responses; no external requests."""

import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from tracker import auth as module
from tracker.auth import (
    AUDIENCE,
    ISSUER,
    FarmAuth,
    NoRedirect,
    ProxyHandler,
    TransportConfigError,
    authority_transport_origin,
)
from tracker.errors import TrackerError


@pytest.fixture(scope="module")
def signing_keys():
    return [rsa.generate_private_key(public_exponent=65537, key_size=2048) for _ in range(2)]


def public_jwk(key, kid="trusted-key"):
    return {
        **json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())),
        "kid": kid,
        "alg": "RS256",
        "use": "sig",
    }


@pytest.fixture
def farm(signing_keys):
    clock = [2_000_000_000]
    state = SimpleNamespace(
        clock=clock,
        calls=[],
        keys=signing_keys,
        jwks={"keys": [public_jwk(signing_keys[0])]},
        jwks_error=None,
        entitlement_error=None,
        reply={
            "success": True,
            "data": {
                "contract": "farm.service-access/1",
                "service_id": "tracker",
                "user_id": "42",
                "app_session_id": "family-42",
                "allowed": True,
                "session_active": True,
                "scopes": ["todos.read", "todos.write"],
                "expires_in": 0,
            },
        },
    )

    def transport(endpoint, body=None):
        state.calls.append((endpoint, copy.deepcopy(body)))
        if endpoint == "jwks":
            if state.jwks_error:
                raise state.jwks_error
            return copy.deepcopy(state.jwks)
        assert endpoint == "service-entitlement"
        if state.entitlement_error:
            raise state.entitlement_error
        return copy.deepcopy(state.reply)

    def sign(claims=None, headers=None, key=None, omit=()):
        payload = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "42",
            "sid": "family-42",
            "client_id": "farm-mcp",
            "jti": "request-42",
            "iat": int(clock[0]),
            "exp": int(clock[0]) + 300,
            "scope": "todos.read todos.write",
        }
        payload.update(claims or {})
        for field in omit:
            payload.pop(field, None)
        header = {"kid": "trusted-key", "typ": "at+jwt"}
        header.update(headers or {})
        return "Bearer " + jwt.encode(
            payload, key or signing_keys[0], algorithm="RS256", headers=header
        )

    state.sign = sign
    state.auth = FarmAuth("fsvc_" + "a" * 64, transport, now=lambda: clock[0])
    return state


def expect_error(farm, authorization=None, scope="todos.read", code="invalid_token", status=401):
    with pytest.raises(TrackerError) as caught:
        farm.auth.authenticate(authorization or farm.sign(), scope)
    assert (caught.value.code, caught.value.status) == (code, status)


def test_signed_user_token_checks_the_bound_session_and_preserves_actor(farm):
    principal = farm.auth.authenticate(farm.sign(), "todos.write")
    assert principal.actor() == {"subject": "42", "client_id": "farm-mcp"}
    assert principal.sid == "family-42"
    assert principal.scopes == frozenset({"todos.read", "todos.write"})
    assert farm.calls == [
        ("jwks", None),
        ("service-entitlement", {"user_id": "42", "app_session_id": "family-42"}),
    ]


def test_invalid_signature_never_reaches_live_entitlement(farm):
    expect_error(farm, farm.sign(key=farm.keys[1]))
    assert farm.calls == [("jwks", None)]


@pytest.mark.parametrize(
    "claims",
    [
        {"iss": "https://attacker.invalid"},
        {"aud": "urn:farm:service:manager"},
        {"aud": [AUDIENCE]},
        {"sub": "machine:bot"},
        {"sub": ""},
        {"sub": " user "},
        {"sub": "u" * 129},
        {"sub": 42},
        {"sid": " "},
        {"client_id": None},
        {"jti": []},
        {"scope": "todos.read todos.read"},
        {"scope": "todos.read  todos.write"},
        {"scope": "todos.read tokens.admin"},
        {"scope": ["todos.read"]},
    ],
)
def test_invalid_identity_audience_or_scope_claims_fail_before_entitlement(farm, claims):
    expect_error(farm, farm.sign(claims))
    assert not any(endpoint == "service-entitlement" for endpoint, _ in farm.calls)


@pytest.mark.parametrize(
    "field", ["iss", "aud", "sub", "sid", "client_id", "jti", "iat", "exp", "scope"]
)
def test_required_claims_cannot_be_omitted(farm, field):
    expect_error(farm, farm.sign(omit=[field]))


@pytest.mark.parametrize(
    "headers",
    [
        {"typ": "farm-machine+jwt"},
        {"typ": "JWT"},
        {"kid": ""},
        {"jku": "https://attacker.invalid/keys"},
        {"x5u": "https://attacker.invalid/cert"},
        {"crit": []},
    ],
)
def test_untrusted_key_locations_and_other_token_profiles_are_rejected(farm, headers):
    expect_error(farm, farm.sign(headers=headers))
    assert farm.calls == []


@pytest.mark.parametrize(
    "authorization", [None, "", "Basic abc", "Bearer bad token", "Bearer x.y.z"]
)
def test_bad_bearer_transport_is_rejected(farm, authorization):
    with pytest.raises(TrackerError) as caught:
        farm.auth.authenticate(authorization, "todos.read")
    assert caught.value.status == 401
    assert farm.calls == []


def test_symmetric_token_cannot_use_a_public_rsa_key_as_a_secret(farm):
    token = jwt.encode(
        {"sub": "42"},
        "different-secret" * 3,
        algorithm="HS256",
        headers={"kid": "trusted-key", "typ": "at+jwt"},
    )
    expect_error(farm, "Bearer " + token)
    assert farm.calls == []


@pytest.mark.parametrize(
    "changes",
    [
        {"iat": True},
        {"iat": 2_000_000_000.0},
        {"exp": False},
        {"exp": 2_000_000_300.0},
        {"exp": 2_000_000_301},
        {"exp": 2_000_000_000},
        {"iat": 2_000_000_031, "exp": 2_000_000_300},
        {"iat": 1_999_999_670, "exp": 1_999_999_970},
        {"nbf": 2_000_000_031},
        {"nbf": 2_000_000_000.0},
        {"nbf": True},
    ],
)
def test_invalid_expiry_lifetime_and_numeric_dates_are_rejected(farm, changes):
    expect_error(farm, farm.sign(changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"iat": 2_000_000_030, "exp": 2_000_000_300},
        {"iat": 1_999_999_671, "exp": 1_999_999_971},
        {"nbf": 2_000_000_030},
    ],
)
def test_valid_tokens_within_the_thirty_second_skew_are_accepted(farm, changes):
    assert farm.auth.authenticate(farm.sign(changes), "todos.read").subject == "42"


def test_read_token_cannot_write_even_if_live_role_can_write(farm):
    expect_error(farm, farm.sign({"scope": "todos.read"}), "todos.write", "insufficient_scope", 403)
    assert farm.calls == [("jwks", None)]


@pytest.mark.parametrize(
    "revocation",
    [
        {"allowed": False, "session_active": False, "scopes": []},
        {"session_active": False},
        {"scopes": ["todos.read"]},
    ],
)
def test_session_or_live_client_permission_revocation_takes_effect_without_a_cache(
    farm, revocation
):
    token = farm.sign()
    farm.auth.authenticate(token, "todos.write")
    farm.reply["data"].update(revocation)
    expect_error(farm, token, "todos.write", "service_access_denied", 403)
    assert sum(endpoint == "jwks" for endpoint, _ in farm.calls) == 1
    assert sum(endpoint == "service-entitlement" for endpoint, _ in farm.calls) == 2


def test_authorization_outage_never_reuses_an_earlier_allow(farm):
    token = farm.sign()
    farm.auth.authenticate(token, "todos.read")
    farm.entitlement_error = module.unavailable()
    expect_error(farm, token, code="authorization_unavailable", status=503)


def test_returned_permissions_intersect_signed_and_live_authority(farm):
    farm.reply["data"]["scopes"] = ["todos.read"]
    principal = farm.auth.authenticate(farm.sign(), "todos.read")
    assert principal.scopes == frozenset({"todos.read"})


@pytest.mark.parametrize("reply", [None, [], "unexpected", {}, {"success": True, "data": []}])
def test_malformed_entitlement_envelopes_fail_closed_with_service_unavailable(farm, reply):
    farm.reply = reply
    expect_error(farm, code="authorization_unavailable", status=503)


@pytest.mark.parametrize(
    "changes",
    [
        {"contract": "other"},
        {"service_id": "manager"},
        {"user_id": "different-user"},
        {"user_id": 42},
        {"app_session_id": "different-session"},
        {"allowed": 1},
        {"session_active": 1},
        {"expires_in": 60},
        {"expires_in": False},
        {"expires_in": 0.0},
        {"scopes": "todos.read"},
        {"scopes": ["tokens.admin"]},
        {"scopes": [{}]},
        {"scopes": [[]]},
        {"scopes": ["todos.read", "todos.read"]},
    ],
)
def test_malformed_or_mismatched_live_entitlements_are_unavailable(farm, changes):
    farm.reply["data"].update(changes)
    expect_error(farm, code="authorization_unavailable", status=503)


def test_concurrent_verification_coalesces_the_initial_jwks_fetch(farm):
    token = farm.sign()
    with ThreadPoolExecutor(max_workers=6) as pool:
        principals = list(
            pool.map(lambda _: farm.auth.authenticate(token, "todos.read"), range(12))
        )
    assert all(principal.subject == "42" for principal in principals)
    assert sum(endpoint == "jwks" for endpoint, _ in farm.calls) == 1
    assert sum(endpoint == "service-entitlement" for endpoint, _ in farm.calls) == 12


def test_unknown_keys_have_a_bounded_refresh_and_accept_trusted_rotation(farm):
    expect_error(farm, farm.sign(headers={"kid": "unknown"}))
    for _ in range(5):
        expect_error(farm, farm.sign(headers={"kid": "unknown"}))
    assert farm.calls == [("jwks", None)]
    farm.clock[0] += 30
    farm.jwks["keys"].append(public_jwk(farm.keys[1], "rotated-key"))
    principal = farm.auth.authenticate(
        farm.sign(headers={"kid": "rotated-key"}, key=farm.keys[1]), "todos.read"
    )
    assert principal.subject == "42"
    assert sum(endpoint == "jwks" for endpoint, _ in farm.calls) == 2


def test_cached_keys_have_a_finite_outage_window(farm):
    farm.auth.authenticate(farm.sign(), "todos.read")
    farm.clock[0] += 300
    farm.jwks_error = module.unavailable()
    assert farm.auth.authenticate(farm.sign(), "todos.read").subject == "42"
    farm.clock[0] += 301
    expect_error(farm, code="authorization_unavailable", status=503)
    assert sum(endpoint == "service-entitlement" for endpoint, _ in farm.calls) == 2


def test_initial_jwks_outage_is_unavailable_and_never_authorizes(farm):
    farm.jwks_error = module.unavailable()
    expect_error(farm, code="authorization_unavailable", status=503)
    expect_error(farm, code="authorization_unavailable", status=503)
    assert farm.calls == [("jwks", None)]


@pytest.mark.parametrize("document", [None, [], {}, {"keys": []}, {"keys": [{}]}])
def test_invalid_jwks_documents_are_unavailable(farm, document):
    farm.jwks = document
    expect_error(farm, code="authorization_unavailable", status=503)


@pytest.mark.parametrize(
    "change",
    [
        {"alg": "RS512"},
        {"use": "enc"},
        {"d": "private"},
        {"p": "private"},
        {"key_ops": ["encrypt"]},
        {"kid": ""},
    ],
)
def test_jwks_must_contain_public_signing_keys(farm, change):
    farm.jwks["keys"][0].update(change)
    expect_error(farm, code="authorization_unavailable", status=503)


def test_jwks_rejects_duplicate_key_identifiers(farm):
    farm.jwks["keys"].append(public_jwk(farm.keys[1]))
    expect_error(farm, code="authorization_unavailable", status=503)


def test_jwks_rejects_weak_rsa_keys(farm):
    weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    farm.jwks = {"keys": [public_jwk(weak)]}
    expect_error(farm, code="authorization_unavailable", status=503)


def test_jwks_bounds_the_number_of_registered_keys(farm):
    farm.jwks = {"keys": [public_jwk(farm.keys[0], "key-" + str(i)) for i in range(33)]}
    expect_error(farm, code="authorization_unavailable", status=503)


@pytest.mark.parametrize(
    "credential", [None, "fsvc_" + "a" * 32, "fsvc_" + "a" * 129, "fbff_" + "a" * 64]
)
def test_tracker_entitlement_credential_uses_the_exact_farm_format(credential):
    with pytest.raises(ValueError):
        FarmAuth(credential)


def test_http_transport_only_sends_service_proof_to_the_fixed_entitlement_url(monkeypatch):
    calls = []
    payloads = [b'{"keys": []}', b'{"success": true}']

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, limit):
            assert limit == 131073
            return payloads.pop(0)

    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            assert timeout == 5
            return Response()

    def opener(*handlers):
        assert len(handlers) == 2
        assert isinstance(handlers[0], ProxyHandler) and handlers[0].proxies == {}
        assert handlers[1] is NoRedirect
        return Opener()

    monkeypatch.setattr(module, "build_opener", opener)
    auth = FarmAuth("fsvc_" + "a" * 64)
    auth._transport("jwks")
    auth._transport("service-entitlement", {"user_id": "42", "app_session_id": "family-42"})
    assert all(request.get_header("User-agent") == "farm-tracker/1" for request in calls)
    assert all(request.get_header("Host") == "noiseflood.farm" for request in calls)
    assert calls[0].full_url == "http://127.0.0.1/v1/auth/jwks"
    assert calls[0].get_header("Authorization") is None
    assert calls[1].full_url == "http://127.0.0.1/v1/auth/service-entitlement"
    assert calls[1].get_header("Authorization") == "Bearer " + auth.credential
    assert json.loads(calls[1].data) == {"user_id": "42", "app_session_id": "family-42"}
    assert (
        NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://attacker.invalid")
        is None
    )


@pytest.mark.parametrize(
    "failure",
    [
        URLError("offline"),
        TimeoutError(),
        HTTPError(ISSUER, 302, "redirect", {}, None),
        HTTPError(ISSUER, 403, "access denied", {}, None),
    ],
)
def test_http_network_errors_and_redirects_are_authorization_unavailable(monkeypatch, failure):
    class Opener:
        def open(self, *_args, **_kwargs):
            raise failure

    monkeypatch.setattr(module, "build_opener", lambda *_: Opener())
    with pytest.raises(TrackerError) as caught:
        FarmAuth("fsvc_" + "a" * 64)._transport("jwks")
    assert (caught.value.status, caught.value.code) == (503, "authorization_unavailable")


@pytest.mark.parametrize("payload", [b"x" * 131073, b"invalid-json", b"\xff"])
def test_http_response_size_and_json_are_bounded(monkeypatch, payload):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, limit):
            assert limit == 131073
            return payload

    class Opener:
        def open(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(module, "build_opener", lambda *_: Opener())
    with pytest.raises(TrackerError) as caught:
        FarmAuth("fsvc_" + "a" * 64)._transport("jwks")
    assert (caught.value.status, caught.value.code) == (503, "authorization_unavailable")


@pytest.mark.parametrize(
    "origin",
    [
        "http://127.0.0.1", "http://127.0.0.1:9000/", "http://[::1]:9000",
        "https://127.0.0.1", "https://noiseflood.farm", "https://noiseflood.farm:443/",
    ],
)
def test_transport_configuration_accepts_only_local_or_canonical_public_origin(origin):
    assert authority_transport_origin(origin) == origin.rstrip("/")


@pytest.mark.parametrize(
    "origin",
    [
        "", "http://noiseflood.farm", "https://attacker.invalid", "http://localhost",
        "http://127.1", "http://2130706433", "http://0x7f000001", "http://127.0.0.2",
        "http://127.0.0.1.attacker.invalid", "https://noiseflood.farm:444",
        "http://user:secret@127.0.0.1", "http://127.0.0.1/path", "http://127.0.0.1/..",
        "http://127.0.0.1?", "http://127.0.0.1#", "http://127.0.0.1:0",
        "http://127.0.0.1:65536", "http://127.0.0.1:bad", "http://[::1",
        "http://[::1]attacker.invalid", "http://127.0.0.1:", "http://%31%32%37.0.0.1",
        "http://127.0.0.1\\@attacker.invalid", " http://127.0.0.1", "http://127.0.0.1\n",
        "http://127.0.0.1\t", "file:///tmp/auth", "//127.0.0.1",
    ],
)
def test_transport_configuration_rejects_untrusted_or_ambiguous_origin(origin):
    with pytest.raises(TransportConfigError) as caught:
        authority_transport_origin(origin)
    assert "secret" not in str(caught.value)


def test_transport_environment_does_not_change_public_identity(monkeypatch):
    monkeypatch.setenv("TRACKER_FARM_TRANSPORT_ORIGIN", "http://[::1]:9000")
    auth = FarmAuth("fsvc_" + "a" * 64)
    assert auth.transport_origin == "http://[::1]:9000"
    assert ISSUER == "https://noiseflood.farm"
    assert AUDIENCE == "urn:farm:service:tracker"


@pytest.mark.parametrize(
    "endpoint,body",
    [("jwks", {}), ("service-entitlement", None), ("service-entitlement", []),
     ("../login", {}), ("jwks?next=elsewhere", None), ("https://attacker.invalid", {})],
)
def test_transport_rejects_unapproved_requests_before_network(monkeypatch, endpoint, body):
    def unexpected(*_):
        pytest.fail("invalid authority request reached the network")

    monkeypatch.setattr(module, "build_opener", unexpected)
    with pytest.raises(ValueError, match="Untrusted Farm authorization request"):
        FarmAuth("fsvc_" + "a" * 64)._transport(endpoint, body)


@pytest.fixture
def local_authority(farm, monkeypatch):
    requests = []
    farm.http_status = 200

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            requests.append((self.command, self.path, dict(self.headers), body))
            self.send_response(farm.http_status)
            if farm.http_status == 302:
                self.send_header("Location", "/redirected")
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            data = farm.jwks if self.path == "/v1/auth/jwks" else farm.reply
            self.wfile.write(json.dumps(data).encode())

        do_GET = respond
        do_POST = respond

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    farm.auth = FarmAuth("fsvc_" + "a" * 64, now=lambda: farm.clock[0], transport_origin=origin)
    try:
        yield requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_local_wire_transport_keeps_public_jwt_and_live_revocation(farm, local_authority):
    principal = farm.auth.authenticate(farm.sign(), "todos.write")
    assert principal.subject == "42"
    assert [(r[0], r[1]) for r in local_authority] == [
        ("GET", "/v1/auth/jwks"), ("POST", "/v1/auth/service-entitlement"),
    ]
    assert all(r[2]["Host"] == "noiseflood.farm" for r in local_authority)
    assert "Authorization" not in local_authority[0][2]
    assert local_authority[1][2]["Authorization"] == "Bearer " + farm.auth.credential
    assert json.loads(local_authority[1][3]) == {
        "user_id": "42", "app_session_id": "family-42",
    }
    expect_error(farm, farm.sign({"iss": farm.auth.transport_origin}))
    expect_error(farm, farm.sign({"aud": "urn:farm:service:manager"}))
    expect_error(farm, farm.sign(key=farm.keys[1]))
    assert len(local_authority) == 2
    farm.reply["data"]["session_active"] = False
    expect_error(farm, code="service_access_denied", status=403)
    assert len(local_authority) == 3
    assert local_authority[-1][1] == "/v1/auth/service-entitlement"


def test_local_transport_never_follows_redirects(farm, local_authority):
    farm.http_status = 302
    expect_error(farm, code="authorization_unavailable", status=503)
    assert [r[1] for r in local_authority] == ["/v1/auth/jwks"]
