"""Farm service JWT verification and live, session-bound entitlement checks."""

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

import jwt

from .errors import TrackerError

ISSUER = "https://noiseflood.farm"
AUDIENCE = "urn:farm:service:tracker"
SCOPES = {"todos.read", "todos.write"}
DEFAULT_TRANSPORT_ORIGIN = "http://127.0.0.1"


class TransportConfigError(ValueError):
    """The configured authority transport is not an allowed destination."""


def authority_transport_origin(value=None):
    """Select transport separately from the public JWT issuer and audience."""
    if value is None:
        value = os.environ.get("TRACKER_FARM_TRANSPORT_ORIGIN", DEFAULT_TRANSPORT_ORIGIN)
    try:
        if not isinstance(value, str) or re.search(r"[\s\\?#]", value):
            raise ValueError()
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or not re.fullmatch(
                r"(?:127\.0\.0\.1|\[::1\]|noiseflood\.farm)(?::[0-9]{1,5})?",
                parsed.netloc.lower(),
            )
            or (parsed.port is not None and not 1 <= parsed.port <= 65535)
            or not (
                parsed.hostname in {"127.0.0.1", "::1"}
                or (
                    parsed.scheme == "https"
                    and parsed.hostname == "noiseflood.farm"
                    and parsed.port in {None, 443}
                )
            )
        ):
            raise ValueError()
        return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    except ValueError:
        raise TransportConfigError(
            "TRACKER_FARM_TRANSPORT_ORIGIN must be a literal loopback HTTP(S) origin "
            "or https://noiseflood.farm, without a path or credentials"
        ) from None


@dataclass(frozen=True)
class Principal:
    subject: str
    client_id: str
    sid: str
    scopes: frozenset

    def actor(self):
        return {"subject": self.subject, "client_id": self.client_id}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def unavailable():
    return TrackerError("authorization_unavailable", "Farm authorization is unavailable", 503)


def invalid():
    return TrackerError("invalid_token", "A valid Tracker service token is required", 401)


class FarmAuth:
    def __init__(self, credential, transport=None, now=time.time, *, transport_origin=None):
        if not isinstance(credential, str) or not re.fullmatch(
            r"fsvc_[A-Za-z0-9_-]{43,128}", credential
        ):
            raise ValueError("Tracker requires a private Farm entitlement credential")
        self.credential = credential
        self.transport_origin = authority_transport_origin(transport_origin)
        self.transport = transport or self._transport
        self.now = now
        self.keys = {}
        self.fetched = -float("inf")
        self.attempted = -float("inf")
        self.last_fetch_failed = False
        self.lock = threading.Lock()

    def _transport(self, endpoint, body=None):
        if not (
            (endpoint == "jwks" and body is None)
            or (endpoint == "service-entitlement" and isinstance(body, dict))
        ):
            raise ValueError("Untrusted Farm authorization request")
        headers = {
            "Accept": "application/json",
            "User-Agent": "farm-tracker/1",
            "Host": "noiseflood.farm",
        }
        if body is not None:
            headers.update(
                {"Authorization": "Bearer " + self.credential, "Content-Type": "application/json"}
            )
        request = Request(
            self.transport_origin + "/v1/auth/" + endpoint,
            data=None if body is None else json.dumps(body).encode(),
            headers=headers,
        )
        try:
            with build_opener(ProxyHandler({}), NoRedirect).open(request, timeout=5) as response:
                raw = response.read(131073)
                if len(raw) > 131072:
                    raise unavailable()
                return json.loads(raw)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError):
            raise unavailable() from None

    def _key(self, kid):
        with self.lock:
            now = self.now()
            if (kid not in self.keys or now - self.fetched >= 300) and now - self.attempted >= 30:
                self.attempted = now
                try:
                    document = self.transport("jwks")
                    entries = document.get("keys")
                    if not isinstance(entries, list) or not 0 < len(entries) <= 32:
                        raise ValueError()
                    keys = {}
                    for entry in entries:
                        if (
                            not isinstance(entry, dict)
                            or entry.get("kty") != "RSA"
                            or entry.get("alg") != "RS256"
                            or entry.get("use") != "sig"
                            or not isinstance(entry.get("kid"), str)
                            or not entry["kid"]
                            or entry["kid"] in keys
                            or any(
                                field in entry for field in ("d", "p", "q", "dp", "dq", "qi", "oth")
                            )
                            or entry.get("key_ops", ["verify"]) != ["verify"]
                        ):
                            raise ValueError()
                        key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(entry))
                        if key.key_size < 2048:
                            raise ValueError()
                        keys[entry["kid"]] = key
                    self.keys, self.fetched = keys, now
                    self.last_fetch_failed = False
                except (TrackerError, ValueError, TypeError, AttributeError, jwt.PyJWTError):
                    self.last_fetch_failed = True
                    if kid not in self.keys or now - self.fetched > 600:
                        raise unavailable() from None
            if kid not in self.keys:
                if self.last_fetch_failed:
                    raise unavailable()
                raise invalid()
            if now - self.fetched > 600:
                raise unavailable()
            return self.keys[kid]

    def authenticate(self, authorization, scope):
        match = re.fullmatch(r"Bearer +([^\s]+)", authorization or "", re.I)
        if not match or len(match[1]) > 16384:
            raise invalid()
        token = match[1]
        try:
            header = jwt.get_unverified_header(token)
            if (
                header.get("alg") != "RS256"
                or header.get("typ") != "at+jwt"
                or not isinstance(header.get("kid"), str)
                or not header["kid"]
                or any(k in header for k in ("jku", "x5u", "crit"))
            ):
                raise invalid()
            claims = jwt.decode(
                token,
                self._key(header["kid"]),
                algorithms=["RS256"],
                issuer=ISSUER,
                audience=AUDIENCE,
                options={
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_nbf": False,
                    "require": [
                        "iss",
                        "aud",
                        "sub",
                        "sid",
                        "client_id",
                        "jti",
                        "iat",
                        "exp",
                        "scope",
                    ],
                },
            )
            if (
                claims["aud"] != AUDIENCE
                or claims["iss"] != ISSUER
                or any(type(claims[k]) is not int for k in ("iat", "exp"))
                or not 0 < claims["exp"] - claims["iat"] <= 300
                or claims["iat"] > self.now() + 30
                or claims["exp"] <= self.now() - 30
                or any(
                    not isinstance(claims[k], str) or not claims[k].strip()
                    for k in ("sub", "sid", "client_id", "jti", "scope")
                )
                or claims["sub"].startswith("machine:")
                or len(claims["sub"]) > 128
                or re.search(r"[\s\x00-\x1f\x7f]", claims["sub"])
            ):
                raise invalid()
            if "nbf" in claims and (
                type(claims["nbf"]) is not int or claims["nbf"] > self.now() + 30
            ):
                raise invalid()
            scopes = claims["scope"].split(" ")
            if len(scopes) != len(set(scopes)) or not set(scopes) <= SCOPES:
                raise invalid()
        except (jwt.PyJWTError, TypeError, ValueError, KeyError):
            raise invalid() from None
        if scope not in scopes:
            raise TrackerError("insufficient_scope", "This operation requires " + scope, 403)
        reply = self.transport(
            "service-entitlement", {"user_id": claims["sub"], "app_session_id": claims["sid"]}
        )
        data = reply.get("data") if isinstance(reply, dict) else None
        if (
            not isinstance(reply, dict)
            or reply.get("success") is not True
            or not isinstance(data, dict)
            or data.get("contract") != "farm.service-access/1"
            or data.get("service_id") != "tracker"
            or data.get("user_id") != claims["sub"]
            or data.get("app_session_id") != claims["sid"]
            or type(data.get("allowed")) is not bool
            or type(data.get("session_active")) is not bool
            or not isinstance(data.get("scopes"), list)
            or type(data.get("expires_in")) is not int
            or data["expires_in"] != 0
            or any(not isinstance(s, str) or s not in SCOPES for s in data["scopes"])
            or len(data["scopes"]) != len(set(data["scopes"]))
        ):
            raise unavailable()
        if not data["allowed"] or not data["session_active"] or scope not in data["scopes"]:
            raise TrackerError(
                "service_access_denied", "Tracker permission is no longer active", 403
            )
        return Principal(
            claims["sub"],
            claims["client_id"],
            claims["sid"],
            frozenset(scopes) & frozenset(data["scopes"]),
        )
