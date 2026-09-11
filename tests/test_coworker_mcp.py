"""Exercise the maintained SDK without network access or real credentials."""

import asyncio
import importlib.util
import time
import unittest
from unittest.mock import patch

from code_mower.context_contract import ContextError

if importlib.util.find_spec("mcp") is not None:
    import httpx2
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from code_mower import coworker_mcp as mcp
else:
    mcp = None


@unittest.skipIf(mcp is None, "optional Coworker extra not installed")
class SDKBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.jwk = jwt.algorithms.RSAAlgorithm.to_jwk(cls.key.public_key(), as_dict=True)
        cls.jwk.update(kid="example-key", alg="RS256", use="sig")

    def setUp(self):
        self.requests = []
        self.refresh_status = 200
        self.claim_overrides = {}
        self.expected = {"principal": "one@example.invalid", "workspace": "example", "subject": "example-subject"}
        self.metadata = {
            "issuer": mcp.ORIGIN, "authorization_endpoint": mcp.ORIGIN + "/oauth/authorize",
            "token_endpoint": mcp.ORIGIN + "/oauth/token", "registration_endpoint": mcp.ORIGIN + "/oauth/register",
            "revocation_endpoint": mcp.ORIGIN + "/oauth/revoke", "jwks_uri": mcp.ORIGIN + "/oauth/jwks",
            "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"], "token_endpoint_auth_methods_supported": ["none"],
        }
        self.original_transport = mcp.PinnedTransport

    def token(self):
        claims = {"iss": mcp.ORIGIN, "aud": [mcp.ORIGIN], "iat": int(time.time()), "exp": int(time.time()) + 3600,
                  "sub": "example-subject", "email": "one@example.invalid", "network": "example",
                  "client_id": "example-client", "scope": "mcp:tools", **self.claim_overrides}
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "example-key"})

    def credentials(self):
        return {"tokens": {"access_token": self.token(), "refresh_token": "example-refresh", "expires_in": 3600,
                           "token_type": "Bearer", "scope": "mcp:tools"},
                "client": {"client_id": "example-client", "redirect_uris": ["http://127.0.0.1/callback"],
                           "token_endpoint_auth_method": "none"}}

    def handler(self, request):
        self.requests.append(request)
        path = request.url.path
        if path == "/.well-known/oauth-protected-resource/mcp":
            return httpx2.Response(200, json={"resource": mcp.ENDPOINT, "authorization_servers": [mcp.ORIGIN]})
        if path == "/.well-known/oauth-authorization-server":
            return httpx2.Response(200, json=self.metadata)
        if path == "/oauth/token":
            if self.refresh_status != 200:
                return httpx2.Response(self.refresh_status, json={"error": "invalid_grant", "error_description": "private provider detail"})
            return httpx2.Response(200, json={"access_token": self.token(), "refresh_token": "example-rotated",
                                              "token_type": "Bearer", "expires_in": 3600, "scope": "mcp:tools"})
        if path == "/mcp":
            return httpx2.Response(405)
        if path == "/oauth/jwks":
            return httpx2.Response(200, json={"keys": [self.jwk]})
        raise AssertionError("unexpected endpoint")

    def transport(self, **kwargs):
        return self.original_transport(transport=httpx2.MockTransport(self.handler), **kwargs)

    def test_forced_online_refresh_uses_sdk_and_verifies_signed_identity(self):
        with patch.object(mcp, "PinnedTransport", self.transport):
            proof = mcp.CoworkerBackend().refresh(self.expected, self.credentials())
        self.assertEqual(proof.principal, self.expected["principal"])
        self.assertEqual(proof.workspace, self.expected["workspace"])
        self.assertEqual(proof.credentials["tokens"]["refresh_token"], "example-rotated")
        token_requests = [r for r in self.requests if r.url.path == "/oauth/token"]
        self.assertEqual(len(token_requests), 1)
        self.assertIn(b"grant_type=refresh_token", token_requests[0].content)
        self.assertTrue(next(r for r in self.requests if r.url.path == "/mcp").headers.get("Authorization"))

    def test_revoked_refresh_is_not_mistaken_for_successful_bare_get(self):
        self.refresh_status = 400
        with patch.object(mcp, "PinnedTransport", self.transport):
            with self.assertRaises(ContextError) as raised:
                mcp.CoworkerBackend().refresh(self.expected, self.credentials())
        self.assertNotIn("private provider detail", str(raised.exception))
        self.assertFalse(any(r.url.path == "/oauth/jwks" for r in self.requests))
        self.assertFalse(next(r for r in self.requests if r.url.path == "/mcp").headers.get("Authorization"))

    def test_refreshed_identity_workspace_subject_client_and_expiry_are_checked(self):
        cases = [{"email": "other@example.invalid"}, {"network": "other"}, {"sub": "other"},
                 {"client_id": "other"}, {"iss": "https://example.invalid"}, {"aud": ["https://example.invalid"]},
                 {"exp": int(time.time()) - 1}, {"scope": "other"}]
        for override in cases:
            with self.subTest(field=next(iter(override))):
                self.claim_overrides = override
                with patch.object(mcp, "PinnedTransport", self.transport), self.assertRaises(ContextError):
                    mcp.CoworkerBackend().refresh(self.expected, self.credentials())

    def test_changed_token_endpoint_is_rejected_before_credentials_are_sent(self):
        self.metadata["token_endpoint"] = "https://example.invalid/token"
        with patch.object(mcp, "PinnedTransport", self.transport), self.assertRaises(ContextError):
            mcp.CoworkerBackend().refresh(self.expected, self.credentials())
        self.assertFalse(any(r.method == "POST" for r in self.requests))

    def test_transport_rejects_foreign_origins_queries_and_unapproved_paths(self):
        async def run():
            for target in ["https://example.invalid/mcp", mcp.ENDPOINT + "?secret=example", mcp.ORIGIN + "/admin"]:
                transport = self.transport()
                async with httpx2.AsyncClient(transport=transport) as client:
                    with self.assertRaises(ContextError):
                        await client.get(target)
        asyncio.run(run())
        self.assertEqual(self.requests, [])

    def test_transport_bounds_response_requests_redirects_and_compression(self):
        async def run():
            cases = [(200, {}, b"x" * 20, False), (200, {}, b"x" * 20, True),
                     (302, {"location": "https://example.invalid"}, b"", True),
                     (200, {"Content-Encoding": "gzip"}, b"compressed", True)]
            for code, headers, body, streamed in cases:
                def reply(request, code=code, headers=headers, body=body, streamed=streamed):
                    payload = {"stream": httpx2.ByteStream(body)} if streamed else {"content": body}
                    return httpx2.Response(code, headers=headers, **payload)
                async with httpx2.AsyncClient(transport=self.original_transport(maximum_bytes=8,
                        transport=httpx2.MockTransport(reply))) as client:
                    with self.assertRaises(ContextError):
                        await client.get(mcp.ENDPOINT)
            async with httpx2.AsyncClient(transport=self.transport(maximum_requests=0)) as client:
                with self.assertRaises(ContextError):
                    await client.get(mcp.ENDPOINT)
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
