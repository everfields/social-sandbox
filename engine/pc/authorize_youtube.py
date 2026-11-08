"""The founder's one-time YouTube consent, in one command, on the founder's own PC.

    python engine/pc/authorize_youtube.py --repo everfields/<company>-social

It asks for the OAuth client's id and secret without showing them, opens Google's consent page, catches the answer on
this machine (a Desktop-app client allows any loopback port), and stores the three secrets in the social repo through
`gh secret set`. No value is printed or written to disk. Nothing leaves this PC except to Google and GitHub.
"""

from __future__ import annotations

import argparse
import getpass
import http.server
import json
import secrets
import subprocess
import sys
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from social.youtube import SCOPES, TOKEN_URL  # noqa: E402

AUTH = "https://accounts.google.com/o/oauth2/v2/auth"


class Catch(http.server.BaseHTTPRequestHandler):
    query: dict = {}

    def do_GET(self) -> None:  # noqa: N802
        Catch.query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write("<h2>Listo. Puedes cerrar esta pestaña.</h2>".encode())

    def log_message(self, *a) -> None:
        pass


def store(repo: str, name: str, value: str) -> None:
    subprocess.run(["gh", "secret", "set", name, "-R", repo], input=value, text=True, check=True, capture_output=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    repo = ap.parse_args().repo
    cid = getpass.getpass("ID de cliente OAuth (no se ve al escribir): ").strip()
    secret = getpass.getpass("Secreto de cliente OAuth (no se ve al escribir): ").strip()
    server = http.server.HTTPServer(("127.0.0.1", 0), Catch)
    redirect = f"http://127.0.0.1:{server.server_address[1]}"
    state = secrets.token_urlsafe(16)
    url = f"{AUTH}?" + urllib.parse.urlencode({"client_id": cid, "redirect_uri": redirect, "response_type": "code", "scope": SCOPES,
                                               "access_type": "offline", "prompt": "consent", "state": state})
    print("\nSe abre el navegador. Entra con la cuenta del canal y acepta.\n")
    webbrowser.open(url)
    server.handle_request()
    q = Catch.query
    if q.get("state", [""])[0] != state or "code" not in q:
        raise SystemExit(f"Google no devolvió el permiso: {q.get('error', ['respuesta inesperada'])[0]}")
    data = urllib.parse.urlencode({"client_id": cid, "client_secret": secret, "code": q["code"][0], "grant_type": "authorization_code",
                                   "redirect_uri": redirect}).encode()
    with urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=data), timeout=60) as r:
        body = json.loads(r.read())
    if "refresh_token" not in body:
        raise SystemExit("Google no dio un permiso duradero. Revoca el acceso de la app en tu cuenta de Google y repite.")
    store(repo, "YOUTUBE_CLIENT_ID", cid)
    store(repo, "YOUTUBE_CLIENT_SECRET", secret)
    store(repo, "YOUTUBE_REFRESH_TOKEN", body["refresh_token"])
    print(f"Hecho: tres secretos guardados en {repo}. Ningún valor se ha mostrado.")


if __name__ == "__main__":
    main()
