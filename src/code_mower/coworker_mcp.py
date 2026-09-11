"""Optional SDK boundary for the live-qualified Coworker OAuth contract.

Imported only by context operations. No ambient host MCP settings or credentials
are consulted. All credential-bearing HTTP stays on the pinned provider origin.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

import httpx2
import jwt
from mcp import Client
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import (
    AuthorizationCodeResult, OAuthClientInformationFull, OAuthClientMetadata,
    OAuthMetadata, OAuthToken, ProtectedResourceMetadata,
)

from .context_contract import ContextError
from .context_store import strict_json

ORIGIN = "https://odin.coworker.ai"
ENDPOINT = ORIGIN + "/mcp"
SDK_RANGE = "mcp>=2.2.0,<2.3"
_PATHS = frozenset({
    "/mcp", "/.well-known/oauth-protected-resource/mcp",
    "/.well-known/oauth-protected-resource", "/.well-known/oauth-authorization-server",
    "/oauth/register", "/oauth/token", "/oauth/jwks", "/oauth/revoke",
})

# SDK diagnostics can include provider error bodies. Our boundary emits only
# fixed ContextError messages; do not propagate those bodies to CLI/review logs.
logging.getLogger("mcp").addHandler(logging.NullHandler())
logging.getLogger("mcp").propagate = False


@dataclass(frozen=True, repr=False)
class ConnectionProof:
    principal: str
    workspace: str
    subject: str
    expires_at: int
    credentials: dict[str, Any] = field(repr=False)


class _BoundedStream(httpx2.AsyncByteStream):
    def __init__(self, stream, maximum):
        self.stream, self.maximum = stream, maximum

    async def __aiter__(self):
        count = 0
        async for chunk in self.stream:
            count += len(chunk)
            if count > self.maximum:
                raise ContextError("context response exceeded its byte limit")
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


class PinnedTransport(httpx2.AsyncBaseTransport):
    def __init__(self, *, maximum_bytes=262_144, maximum_requests=16, transport=None):
        self.transport = transport or httpx2.AsyncHTTPTransport(retries=0)
        self.maximum_bytes, self.remaining = maximum_bytes, maximum_requests
        self.responses: list[tuple[str, int]] = []

    async def handle_async_request(self, request):
        url = urlsplit(str(request.url))
        if (url.scheme != "https" or url.netloc != "odin.coworker.ai"
                or url.path not in _PATHS or url.query or url.fragment):
            raise ContextError("context endpoint or redirect is not approved")
        self.remaining -= 1
        if self.remaining < 0:
            raise ContextError("context request limit exceeded")
        request.headers["Accept-Encoding"] = "identity"
        response = await self.transport.handle_async_request(request)
        self.responses.append((url.path, response.status_code))
        # A transport redirect cannot be used to move credentials or requests.
        if 300 <= response.status_code < 400 or response.headers.get("Content-Encoding", "identity") != "identity":
            await response.aclose()
            raise ContextError("context endpoint redirects are not supported")
        # A custom transport may return an already-buffered response. Rebuild
        # it so neither cached content nor an absent/false Content-Length can
        # bypass the same streaming limit used by the network transport.
        stream = httpx2.ByteStream(response.content) if response.is_stream_consumed else response.stream
        return httpx2.Response(response.status_code, headers=response.headers,
                               stream=_BoundedStream(stream, self.maximum_bytes),
                               extensions=response.extensions)

    async def aclose(self):
        await self.transport.aclose()


class _MemoryStorage:
    def __init__(self, credentials=None):
        credentials = credentials or {}
        self.tokens = OAuthToken.model_validate(credentials["tokens"]) if credentials.get("tokens") else None
        self.client = OAuthClientInformationFull.model_validate(credentials["client"]) if credentials.get("client") else None

    async def get_tokens(self):
        return self.tokens

    async def set_tokens(self, value):
        self.tokens = value

    async def get_client_info(self):
        return self.client

    async def set_client_info(self, value):
        self.client = value


async def _discovery(http):
    resource = await http.get(ORIGIN + "/.well-known/oauth-protected-resource/mcp")
    resource.raise_for_status()
    resource_data = strict_json(resource.content)
    metadata = await http.get(ORIGIN + "/.well-known/oauth-authorization-server")
    metadata.raise_for_status()
    data = strict_json(metadata.content)
    expected = {
        "issuer": ORIGIN, "authorization_endpoint": ORIGIN + "/oauth/authorize",
        "token_endpoint": ORIGIN + "/oauth/token", "registration_endpoint": ORIGIN + "/oauth/register",
        "revocation_endpoint": ORIGIN + "/oauth/revoke", "jwks_uri": ORIGIN + "/oauth/jwks",
    }
    if (any(data.get(k) != v for k, v in expected.items())
            or resource_data.get("resource") != ENDPOINT
            or resource_data.get("authorization_servers") != [ORIGIN]
            or "S256" not in data.get("code_challenge_methods_supported", [])
            or "refresh_token" not in data.get("grant_types_supported", [])):
        raise ContextError("Coworker discovery changed; connection requires requalification")
    return OAuthMetadata.model_validate(data), ProtectedResourceMetadata.model_validate(resource_data)


async def _proof(http, storage: _MemoryStorage, expected) -> ConnectionProof:
    if storage.tokens is None or storage.client is None or not storage.tokens.refresh_token:
        raise ContextError("Coworker did not provide renewable credentials; reconnect")
    response = await http.get(ORIGIN + "/oauth/jwks")
    response.raise_for_status()
    keys = jwt.PyJWKSet.from_dict(strict_json(response.content))
    token = storage.tokens.access_token
    header = jwt.get_unverified_header(token)
    if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
        raise ContextError("Coworker identity signature is unsupported")
    matching = [key for key in keys.keys if key.key_id == header["kid"]]
    if len(matching) != 1:
        raise ContextError("Coworker identity signing key is missing or ambiguous")
    key = matching[0]
    claims = jwt.decode(token, key.key, algorithms=["RS256"], issuer=ORIGIN, audience=ORIGIN,
                        options={"require": ["exp", "iat", "iss", "aud", "sub", "email", "network", "client_id", "scope"]})
    if (claims["email"] != expected["principal"] or claims["network"] != expected["workspace"]
            or claims["client_id"] != storage.client.client_id or claims["scope"] != "mcp:tools"
            or not isinstance(claims["sub"], str) or not claims["sub"]
            or type(claims["exp"]) is not int
            or (expected.get("subject") is not None and claims["sub"] != expected["subject"])):
        raise ContextError("Coworker account or workspace does not match; reconnect with the selected account")
    return ConnectionProof(claims["email"], claims["network"], claims["sub"], claims["exp"], {
        "tokens": storage.tokens.model_dump(mode="json"),
        "client": storage.client.model_dump(mode="json"),
    })


def _auth(storage, redirect_uri="http://127.0.0.1/callback", redirect=None, callback=None):
    return OAuthClientProvider(ENDPOINT, OAuthClientMetadata(
        client_name="Code Mower context", redirect_uris=[redirect_uri],
        token_endpoint_auth_method="none", scope="mcp:tools",
    ), storage, redirect, callback)


class CoworkerBackend:
    """Synchronous lifecycle facade; each operation owns a bounded SDK session."""

    def _run(self, operation):
        try:
            return asyncio.run(operation)
        except ContextError:
            raise
        except Exception:
            raise ContextError("Coworker authorization unavailable; reconnect or retry after service recovery") from None

    def refresh(self, expected, credentials) -> ConnectionProof:
        return self._run(self._refresh(expected, credentials))

    async def _refresh(self, expected, credentials):
        async with asyncio.timeout(30):
            storage = _MemoryStorage(credentials)
            auth = _auth(storage)
            transport = PinnedTransport(maximum_requests=12)
            async with httpx2.AsyncClient(transport=transport, timeout=15, follow_redirects=False) as public:
                metadata, resource = await _discovery(public)
                auth.context.oauth_metadata = metadata
                auth.context.protected_resource_metadata = resource
                auth.context.auth_server_url = ORIGIN
                # The C1 probe proved that only the renewable grant provides
                # revocation checking. Never reuse a merely unexpired JWT.
                auth.context.token_expiry_time = time.time() - 1
                response = await public.get(ENDPOINT, auth=auth)
                if (response.status_code != 405 or ("/oauth/token", 200) not in transport.responses
                        or auth.context.current_tokens is None):
                    raise ContextError("Coworker authorization was revoked or could not refresh; reconnect")
                return await _proof(public, storage, expected)

    def login(self, expected, open_url: Callable[[str], Any]) -> ConnectionProof:
        return self._run(self._login(expected, open_url))

    async def _login(self, expected, open_url):
        async with asyncio.timeout(900):
            callback = asyncio.get_running_loop().create_future()
            state = None

            async def handle(reader, writer):
                try:
                    line = await asyncio.wait_for(reader.readline(), 5)
                    method, target, _ = line.decode("ascii").strip().split(" ", 2)
                    url = urlsplit(target)
                    values = parse_qs(url.query)
                    valid = (method == "GET" and url.path == "/callback" and state is not None
                             and values.get("state") == [state] and len(values.get("code", [])) == 1)
                    if valid and not callback.done():
                        callback.set_result(AuthorizationCodeResult(code=values["code"][0], state=state,
                                                                  iss=values.get("iss", [None])[0]))
                        status, body = "200 OK", b"Code Mower sign-in received. You can close this tab."
                    else:
                        status, body = "400 Bad Request", b"Invalid sign-in callback."
                    writer.write((f"HTTP/1.1 {status}\r\nContent-Type: text/plain\r\nCache-Control: no-store\r\nConnection: close\r\nContent-Length: {len(body)}\r\n\r\n").encode() + body)
                    await writer.drain()
                except Exception:
                    pass
                finally:
                    writer.close()
                    await writer.wait_closed()

            server = await asyncio.start_server(handle, "127.0.0.1", 0, limit=16_384)
            port = server.sockets[0].getsockname()[1]

            async def redirect(url):
                nonlocal state
                parsed = urlsplit(url)
                if parsed.scheme != "https" or parsed.netloc != "odin.coworker.ai" or parsed.path != "/oauth/authorize":
                    raise ContextError("Coworker sign-in destination is not approved")
                state = parse_qs(parsed.query)["state"][0]
                open_url(url)

            async def receive():
                return await callback

            storage = _MemoryStorage()
            auth = _auth(storage, f"http://127.0.0.1:{port}/callback", redirect, receive)
            async with server, httpx2.AsyncClient(transport=PinnedTransport(), auth=auth, timeout=20,
                                                  follow_redirects=False) as http:
                async with httpx2.AsyncClient(transport=PinnedTransport(maximum_requests=2), timeout=15) as public:
                    metadata, resource = await _discovery(public)
                auth.context.oauth_metadata = metadata
                auth.context.protected_resource_metadata = resource
                auth.context.auth_server_url = ORIGIN
                async with Client(streamable_http_client(ENDPOINT, http_client=http), mode="legacy",
                                  read_timeout_seconds=900, cache=None):
                    # Use the same pinned client without auth for public JWKS.
                    async with httpx2.AsyncClient(transport=PinnedTransport(maximum_requests=1), timeout=15) as public:
                        return await _proof(public, storage, expected)

    def revoke(self, credentials) -> None:
        self._run(self._revoke(credentials))

    async def _revoke(self, credentials):
        async with asyncio.timeout(20):
            tokens = credentials["tokens"]
            client = credentials["client"]
            async with httpx2.AsyncClient(transport=PinnedTransport(maximum_requests=1), timeout=15,
                                          follow_redirects=False) as http:
                response = await http.post(ORIGIN + "/oauth/revoke", data={
                    "token": tokens["refresh_token"], "token_type_hint": "refresh_token",
                    "client_id": client["client_id"],
                })
                response.raise_for_status()
