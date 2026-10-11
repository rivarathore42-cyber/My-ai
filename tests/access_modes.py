"""Section Y: access modes and device pairing (Phase 9).

Paired mode (the default) admits a device once, with a code the server prints; the session is an HttpOnly cookie. Local mode
admits every request, but only on a loopback address and never through a public tunnel. The page never asks for a token.
"""
import tempfile
from pathlib import Path

from galaxy import auth
from galaxy import gateway
from galaxy import identity as ID
from galaxy.memory import MemoryStore

ROOT = Path(__file__).resolve().parents[1]
OP = "operator-y-secret-0123456789abcdef"


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def _raises(fn, exc=ValueError) -> bool:
    try:
        fn()
    except exc:
        return True
    return False


async def run(check) -> None:
    check(auth.mode_from_env(None) == "paired" and auth.mode_from_env("LOCAL") == "local",
          "Y-mode: paired is the default, and the value is case-insensitive")
    check(_raises(lambda: auth.mode_from_env("off")), "Y-mode: there is no open 'off' mode; open access is only local")
    check(_raises(lambda: auth.check_startup("local", "0.0.0.0"), RuntimeError),
          "Y-startup: local mode refuses to start on a non-loopback address")
    auth.check_startup("local", "127.0.0.1")
    auth.check_startup("paired", "0.0.0.0")
    check(not auth.tunnel_allowed("local") and auth.tunnel_allowed("paired"), "Y-tunnel: no public tunnel in local mode")
    check(auth.bypasses_guard("POST", "/api/pair") and not auth.bypasses_guard("GET", "/api/pair")
          and not auth.bypasses_guard("POST", "/api/chat"),
          "Y-public: only the pairing route is reachable without a session")

    clock = FakeClock()
    book = auth.PairingBook(clock=clock)
    code = book.issue()
    check(len(code) == 8 and code.isdigit() and book.pending(), "Y-pair: an eight-digit code is issued and pending")
    check(code not in str(vars(book)), "Y-pair: the code is never stored in clear")
    wrong = "11111111" if code != "11111111" else "22222222"
    check(not book.redeem(wrong), "Y-pair: a wrong code is refused")
    check(book.redeem(f"{code[:4]} {code[4:]}"), "Y-pair: the right code is accepted, with or without the space")
    check(not book.redeem(code), "Y-pair: a code works once")

    stale = auth.PairingBook(clock=clock)
    stale_code = stale.issue()
    clock.now += 601
    check(not stale.redeem(stale_code), "Y-pair: a code expires after ten minutes")

    clock.now = 1000.0
    burned = auth.PairingBook(clock=clock)
    burn_code = burned.issue()
    wrong_burn = "11111111" if burn_code != "11111111" else "22222222"
    for _ in range(5):
        burned.redeem(wrong_burn)
    check(not burned.redeem(burn_code), "Y-pair: five wrong tries burn the code")

    replaced = auth.PairingBook(clock=clock)
    first = replaced.issue()
    second = replaced.issue()
    check(not replaced.redeem(first) and replaced.redeem(second),
          "Y-pair: issuing a new code replaces the old one")

    cookie = auth.session_cookie("tok123", 3600, secure=False)
    check("HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/" in cookie and "Secure" not in cookie
          and "Max-Age=3600" in cookie, "Y-cookie: HttpOnly, SameSite=Strict, and no Secure flag over plain http")
    check("Secure" in auth.session_cookie("t", 1, secure=True), "Y-cookie: Secure over https")
    check(auth.cookie_value("a=1; galaxy_session=tok123; b=2") == "tok123" and auth.cookie_value("a=1") is None
          and auth.cookie_value(None) is None, "Y-cookie: the session is read from its own cookie only")

    store = MemoryStore(Path(tempfile.mkdtemp(prefix="access_y_")) / "y.db")
    token = store.token_issue("owner")
    ident = ID.identity_from_bearer(f"Bearer {token}", OP, store.token_resolve)
    check(ident is not None and ident.kind == "user" and ident.user_id == "owner",
          "Y-session: a paired session is the owner's user")
    check(not ID.is_admin(ident), "Y-session: a paired session is never an administrator")
    check(ID.identity_from_bearer(f"Bearer {OP}", OP, store.token_resolve).kind == "operator",
          "Y-admin: the operator token is the only administrator")
    check(ID.identity_from_bearer(f"Bearer {ID.new_user_token()}", OP, store.token_resolve) is None,
          "Y-session: an unknown token gives no identity")

    open_gate = gateway.GuardPolicy(OP, open_access=True)
    check(open_gate.check("POST", "/api/chat", None, "k1").allowed,
          "Y-local: in local mode the gate admits a request with no credential")
    closed = gateway.GuardPolicy(OP, accept=lambda credential: store.token_resolve(credential) is not None)
    check(closed.check("POST", "/api/chat", None, "k2").status == 401,
          "Y-paired: without a session the gate refuses the request")
    check(closed.check("POST", "/api/chat", f"Bearer {token}", "k3").allowed,
          "Y-paired: a valid session passes the gate")

    launcher = (ROOT / "colab_launcher.py").read_text(encoding="utf-8")
    check("local_only" in launcher and "None if local_only" in launcher,
          "Y-launcher: the launcher opens no public tunnel in local mode")
    check("Access token:" not in launcher, "Y-launcher: the launcher never prints the token")
    page = (ROOT / "galaxy" / "web" / "app.html").read_text(encoding="utf-8")
    check('type="password"' not in page and "access token" not in page.lower(),
          "Y-page: the page has no token box")
    app_js = (ROOT / "galaxy" / "web" / "app.js").read_text(encoding="utf-8")
    check("localStorage" not in app_js and "sessionStorage" not in app_js,
          "Y-page: the page keeps no token in browser storage")
