import base64
import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from starlette.testclient import TestClient

import import_ui
import mcp_server
from archive import account_state
from manual_import import import_file
from plugin_access import AccessStore, LOCAL_ORIGIN, SCOPE, USER_COOKIE


class PluginAccessTest(unittest.TestCase):
    def test_profile_logout_allows_switching_accounts(self):
        origin = "http://127.0.0.1:8081"
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            with patch.object(import_ui, "STATE", state), TestClient(
                import_ui.app, base_url=origin, follow_redirects=False,
            ) as browser:
                login = browser.get("/login").text
                self.assertIn('name="username"', login)
                self.assertNotIn('href="/admin"', login)
                self.assertEqual(browser.get("/admin").headers["location"], "/login")
                store = AccessStore(state)
                alice = {"username": "anna.test", "password": "First account password"}
                boris = {"username": "boris.test", "password": "Second account password"}
                store.change_user("create", name="Анна", **alice)
                store.change_user("create", name="Борис", **boris)

                headers = {"Origin": origin}
                self.assertEqual(browser.post("/login", data=alice, headers=headers).status_code, 303)
                old_session = browser.cookies[USER_COOKIE]
                profile = browser.get("/").text
                self.assertIn("Профиль anna.test", profile)
                self.assertNotIn('href="/admin"', profile)
                self.assertEqual(browser.get("/admin").headers["location"], "/")
                key = (state / "admin.key").read_text().strip()
                self.assertEqual(browser.post("/login", data={"username": "admin", "password": alice["password"]},
                                              headers=headers).status_code, 401)
                self.assertEqual(browser.post("/login", data={"username": "admin", "password": key},
                                              headers=headers).headers["location"], "/")
                self.assertEqual(browser.get("/api/admin/users").status_code, 200)
                self.assertIn("Профиль администратора", browser.get("/").text)
                self.assertEqual(browser.post("/logout").status_code, 403)
                self.assertEqual(browser.post("/logout", headers=headers).headers["location"], "/login")
                self.assertIn('name="username"', browser.get("/login").text)
                self.assertEqual(browser.get("/api/admin/users").status_code, 403)
                self.assertEqual(browser.get("/api/archive").status_code, 401)
                self.assertEqual(store.web_access(old_session), (True, None))
                self.assertEqual(browser.post("/login", data=boris, headers=headers).status_code, 303)
                self.assertIn("Профиль boris.test", browser.get("/").text)

    def test_owner_login_pkce_and_revocation(self):
        password = "Test password for WhatsApp"
        callback = "https://chatgpt.com/connector/oauth/test-connection"
        verifier = "a" * 64
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        admin_url = "http://127.0.0.1:8081"
        content_headers = {"Accept": "application/json, text/event-stream"}
        initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "Test", "version": "1"}}}
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            row = import_file(state, io.BytesIO(b"15.05.20, 09:30 - Test: Archive entry"), "chat.txt", name="Test chat")
            user_state = account_state(state, "test.user")
            import_file(user_state, io.BytesIO(b"15.05.20, 09:30 - Test: Member entry"), "chat.txt", name="Member chat")
            with (
                patch.object(import_ui, "STATE", state),
                patch.object(mcp_server, "STATE", state),
                patch.dict("os.environ", {"WA_HISTORY_MCP_ORIGIN": LOCAL_ORIGIN}),
                TestClient(import_ui.app, base_url=admin_url) as admin,
                TestClient(mcp_server.protected_http(), base_url=LOCAL_ORIGIN, follow_redirects=False) as client,
            ):
                origin = {"Origin": admin_url}
                self.assertEqual(admin.post("/api/plugin/password", json={"password": password},
                                            headers=origin).status_code, 403)
                key = (state / "admin.key").read_text().strip()
                self.assertEqual(admin.post("/login", data={"username": "admin", "password": key},
                                            headers=origin, follow_redirects=False).status_code, 303)
                saved = admin.post("/api/plugin/password", json={"password": password}, headers=origin)
                self.assertTrue(saved.json()["password_set"])
                self.assertNotIn(password.encode(), (state / "plugin-access.sqlite3").read_bytes())
                anonymous = client.post("/mcp", headers=content_headers, json=initialize)
                self.assertEqual(anonymous.status_code, 401)
                metadata = client.get("/.well-known/oauth-authorization-server").json()
                self.assertIn("S256", metadata["code_challenge_methods_supported"])
                self.assertEqual(client.post("/register", json={"redirect_uris": ["https://example.com/callback"]}).status_code, 400)
                registration = client.post("/register", json={
                    "redirect_uris": [callback], "grant_types": ["authorization_code", "refresh_token"],
                    "token_endpoint_auth_method": "client_secret_post", "scope": SCOPE,
                })
                self.assertLess(registration.status_code, 300, registration.text)
                credentials = {key: registration.json()[key] for key in ("client_id", "client_secret")}
                start = client.get("/authorize", params={
                    "client_id": credentials["client_id"], "code_challenge": challenge, "code_challenge_method": "S256",
                    "state": "test-state", "redirect_uri": callback, "response_type": "code",
                    "scope": SCOPE, "resource": LOCAL_ORIGIN + "/mcp",
                })
                self.assertEqual(start.status_code, 302, start.text)
                login = urlsplit(start.headers["location"])
                nonce = parse_qs(login.query)["request"][0]
                form = client.get(start.headers["location"])
                self.assertIn("Доступ к архиву", form.text)
                self.assertEqual(form.headers["referrer-policy"], "same-origin")
                self.assertEqual(client.post(login.path, data={"request": nonce, "password": password}).status_code, 403)
                login_headers = {"Origin": LOCAL_ORIGIN}
                login_data = {"request": nonce, "username": "admin"}
                denied = client.post(login.path, data={**login_data, "password": "incorrect"}, headers=login_headers)
                self.assertEqual(denied.status_code, 401)
                granted = client.post(login.path, data={**login_data, "password": password}, headers=login_headers)
                self.assertEqual(granted.status_code, 303, granted.text)
                self.assertEqual(granted.headers["referrer-policy"], "no-referrer")
                reply = parse_qs(urlsplit(granted.headers["location"]).query)
                self.assertEqual(reply["state"], ["test-state"])
                token_request = {**credentials, "grant_type": "authorization_code", "code": reply["code"][0],
                                 "code_verifier": verifier, "redirect_uri": callback, "resource": LOCAL_ORIGIN + "/mcp"}
                token = client.post("/token", data=token_request)
                self.assertEqual(token.status_code, 200, token.text)
                headers = {**content_headers, "Authorization": "Bearer " + token.json()["access_token"]}
                self.assertEqual(client.post("/mcp", json=initialize, headers=headers).status_code, 200)
                search = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "search_messages", "arguments": {"query": "Archive entry"}}})
                self.assertIn("Archive entry", search.text)
                self.assertEqual(client.post("/token", data=token_request).status_code, 400)
                self.assertEqual(admin.post("/api/plugin/revoke", json={}).status_code, 403)
                revoked = admin.post("/api/plugin/revoke", json={}, headers=origin)
                self.assertEqual(revoked.json()["sessions"], 0)
                self.assertEqual(client.post("/mcp", json=initialize, headers=headers).status_code, 401)

                users_path = "/api/admin/users"
                user = {"action": "create", "name": "Test user", "username": "TEST.User", "password": password}
                self.assertEqual(admin.post(users_path, json=user).status_code, 403)
                created = admin.post(users_path, json=user, headers=origin)
                self.assertEqual(created.status_code, 200, created.text)
                self.assertNotIn(password.encode(), (state / "plugin-access.sqlite3").read_bytes())
                self.assertEqual(created.json()["users"][0]["username"], "test.user")
                with TestClient(import_ui.app, base_url=admin_url, follow_redirects=False) as member:
                    self.assertEqual(member.get("/").headers["location"], "/login")
                    for path in ("/api/archive", "/api/archive/history", "/api/source", "/api/plugin"):
                        self.assertEqual(member.get(path).status_code, 401)
                    self.assertEqual(member.get("/api/admin/history", params={"chat": "test"}).status_code, 403)
                    self.assertNotIn("Test chat", member.get("/admin").text)
                    self.assertEqual(member.get("/login").status_code, 200)
                    login_data = {"username": "test.user", "password": password}
                    self.assertEqual(member.post("/login", data=login_data).status_code, 403)
                    self.assertEqual(member.post("/login", data=login_data, headers=origin).status_code, 303)
                    self.assertIn("Member chat", member.get("/").text)
                    self.assertNotIn("Test chat", member.get("/").text)
                    self.assertNotIn("Member chat", admin.get("/").text)
                    self.assertIn("Member chat", admin.get("/admin", params={"owner": "test.user"}).text)
                    chats = member.get("/api/archive").json()["chats"]
                    history = member.get("/api/archive/history", params={"chat": chats[0]["chat_id"]}).json()
                    self.assertEqual(history["messages"][0]["text"], "Member entry")
                    other_history = member.get("/api/archive/history", params={"chat": row["chat_id"], "owner": "admin"})
                    self.assertEqual(other_history.json()["messages"], [])
                    self.assertEqual(member.get(users_path).status_code, 403)
                    self.assertEqual(member.post("/api/source/reconnect?owner=admin", headers=origin,
                                                 json={"confirm": True}).status_code, 403)
                    with patch.object(import_ui.source_hub(user_state), "connect") as reconnect:
                        self.assertEqual(member.post("/api/source/reconnect", headers=origin,
                                                     json={"confirm": True}).status_code, 202)
                        reconnect.assert_called_once_with(user_state, reconnect=True)
                    self.assertEqual(admin.post("/api/source/reconnect", headers=origin,
                                                json={"confirm": False}).status_code, 409)
                    with patch.object(import_ui.hub, "connect") as reconnect:
                        self.assertEqual(admin.post("/api/source/reconnect", headers=origin,
                                                    json={"confirm": True}).status_code, 202)
                        reconnect.assert_called_once_with(state, reconnect=True)

                    start = client.get("/authorize", params={
                        "client_id": credentials["client_id"], "code_challenge": challenge, "code_challenge_method": "S256",
                        "redirect_uri": callback, "response_type": "code", "scope": SCOPE,
                        "resource": LOCAL_ORIGIN + "/mcp",
                    })
                    self.assertIn('name="username"', client.get(start.headers["location"]).text)
                    nonce = parse_qs(urlsplit(start.headers["location"]).query)["request"][0]
                    granted = client.post(login.path, data={"request": nonce, "username": "test.user", "password": password},
                                          headers=login_headers)
                    self.assertEqual(granted.status_code, 303, granted.text)
                    code = parse_qs(urlsplit(granted.headers["location"]).query)["code"][0]
                    token = client.post("/token", data={**token_request, "code": code})
                    self.assertEqual(token.status_code, 200, token.text)
                    bearer = {**content_headers, "Authorization": "Bearer " + token.json()["access_token"]}
                    self.assertEqual(client.post("/mcp", json=initialize, headers=bearer).status_code, 200)
                    search = client.post("/mcp", headers=bearer, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                        "params": {"name": "search_messages", "arguments": {"query": "entry"}}})
                    self.assertIn("Member entry", search.text)
                    self.assertNotIn("Archive entry", search.text)
                    match = search.json()["result"]["structuredContent"]["matches"][0]
                    self.assertNotIn("id", match)
                    location = {field: match[field] for field in ("chat", "timestamp", "author")}
                    result = client.post("/mcp", headers=bearer, json={"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                        "params": {"name": "get_context", "arguments": location}})
                    message = result.json()["result"]["structuredContent"]["message"]
                    self.assertEqual(message["text"], "Member entry")
                    self.assertNotIn("id", message)
                    refreshed = admin.post(users_path, headers=origin, json={"action": "password",
                        "username": "test.user", "password": "A new test password"})
                    self.assertEqual(refreshed.status_code, 200)
                    self.assertEqual(member.get("/api/archive").status_code, 401)
                    self.assertEqual(client.post("/mcp", json=initialize, headers=bearer).status_code, 401)
                    refresh = client.post("/token", data={**credentials, "grant_type": "refresh_token",
                        "refresh_token": token.json()["refresh_token"]})
                    self.assertEqual(refresh.status_code, 400)
                    login_data = {"username": "test.user", "password": "A new test password"}
                    self.assertEqual(member.post("/login", headers=origin, data=login_data).status_code, 303)
                    for action, code in (("disable", 401), ("enable", 303)):
                        self.assertEqual(admin.post(users_path, headers=origin,
                            json={"action": action, "username": "test.user"}).status_code, 200)
                        self.assertEqual(member.post("/login", headers=origin, data=login_data).status_code, code)
