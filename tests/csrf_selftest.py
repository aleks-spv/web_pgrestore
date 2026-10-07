"""Self-test: dedicated httpOnly CSRF cookie fixes the /login lockout.

Run: python3 tests/csrf_selftest.py

Scenario A: normal flow — GET /login, then POST with session + dedicated cookie.
Scenario B: THE BUG — browser lost the session cookie, only the dedicated
            httpOnly `_csrf_token` cookie survives. POST must NOT return
            400 "CSRF token missing or invalid"; it must reach credential
            checking (401 "wrong password" or 302 redirect).
Scenario C: forged POST with no token / wrong token must still be 400.
"""
import os
import sys
import re

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("FLASK_SECRET_KEY", "selftest-secret")
os.environ.setdefault("AUTH_USER", "admin")
os.environ.setdefault("AUTH_PASS", "secret")
os.environ.setdefault("RATE_LIMIT_ENABLED", "0")

from web_pgrestore import create_app  # noqa: E402

CSRF_COOKIE = "_csrf_token"


def token_from_set_cookie(resp):
    raw = resp.headers.getlist("Set-Cookie")
    for c in raw:
        if c.startswith(CSRF_COOKIE + "="):
            return re.match(rf"{CSRF_COOKIE}=([^;]+)", c).group(1)
    return None


def main():
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    failures = []

    # --- Scenario A: normal flow -------------------------------------
    r = client.get("/login")
    assert r.status_code == 200, f"GET /login -> {r.status_code}"
    token = token_from_set_cookie(r)
    assert token, "dedicated _csrf_token cookie NOT set on GET /login"
    print(f"[A] GET /login 200, _csrf_token cookie set (len={len(token)})")

    # normal POST: keep both cookies (test client does this automatically)
    r = client.post("/login", data={"username": "admin",
                                    "password": "wrong",
                                    "_csrf": token})
    if r.status_code == 400:
        failures.append("A: normal POST got 400 CSRF")
    else:
        print(f"[A] normal POST /login -> {r.status_code} (not 400) OK")

    # --- Scenario B: session cookie LOST, only dedicated cookie ------
    # Simulate by clearing cookies, then re-injecting ONLY the csrf cookie.
    client.delete_cookie("session")
    client.set_cookie(CSRF_COOKIE, token)
    r = client.post("/login", data={"username": "admin",
                                    "password": "wrong",
                                    "_csrf": token})
    if r.status_code == 400:
        body = r.get_data(as_text=True)
        if "CSRF" in body:
            failures.append("B: session-cookie-lost POST still 400 CSRF "
                            "(THE ORIGINAL BUG)")
        else:
            failures.append(f"B: unexpected 400: {body[:120]}")
    elif r.status_code == 401:
        print("[B] session lost, only dedicated cookie: 401 wrong-password "
              "-> bug FIXED (CSRF no longer blocks)")
    elif r.status_code == 302:
        print("[B] session lost, only dedicated cookie: 302 redirect "
              "(credentials matched) -> bug FIXED")
    else:
        failures.append(f"B: unexpected status {r.status_code}")

    # --- Scenario C: forged requests must still be blocked -----------
    client.delete_cookie("session")
    client.delete_cookie(CSRF_COOKIE)
    r = client.post("/login", data={"username": "admin", "password": "secret"})
    if r.status_code == 400 and "CSRF" in r.get_data(as_text=True):
        print("[C] POST with NO token -> 400 blocked OK")
    else:
        failures.append(f"C: no-token POST -> {r.status_code} (must be 400)")

    client.set_cookie(CSRF_COOKIE, "deadbeef" * 8)
    r = client.post("/login", data={"username": "admin",
                                    "password": "secret",
                                    "_csrf": "00" * 32})
    if r.status_code == 400 and "CSRF" in r.get_data(as_text=True):
        print("[C] POST with WRONG token -> 400 blocked OK")
    else:
        failures.append(f"C: wrong-token POST -> {r.status_code} (must be 400)")

    # --- Scenario D: correct login rotates cookie & redirects --------
    client.delete_cookie("session")
    client.delete_cookie(CSRF_COOKIE)
    r = client.get("/login")
    token = token_from_set_cookie(r)
    r = client.post("/login", data={"username": "admin",
                                    "password": "secret",
                                    "_csrf": token})
    if r.status_code == 302:
        new_token = token_from_set_cookie(r)
        if new_token and new_token != token:
            print("[D] correct login -> 302 + CSRF token ROTATED OK")
        else:
            failures.append("D: login 302 but token not rotated")
    else:
        failures.append(f"D: correct login -> {r.status_code} (want 302)")

    print()
    if failures:
        print("=== SELF-TEST FAILED ===")
        for f in failures:
            print("  FAIL:", f)
        sys.exit(1)
    print("=== ALL SELF-TESTS PASSED ===")


if __name__ == "__main__":
    main()
