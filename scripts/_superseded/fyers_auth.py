import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

from fyers_apiv3 import fyersModel


CLIENT_ID = os.environ["FYERS_CLIENT_ID"]
SECRET_KEY = os.environ["FYERS_SECRET_KEY"]
REDIRECT_URI = "http://127.0.0.1:5000/callback"

auth_code = None


class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code

        parsed = urlparse(self.path)

        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return

        params = parse_qs(parsed.query)
        auth_code = params.get("auth_code", [None])[0]

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()

        if auth_code:
            self.wfile.write(
                b"<h2>FYERS authentication successful.</h2>"
                b"<p>You can close this browser tab and return to PowerShell.</p>"
            )
        else:
            self.wfile.write(
                b"<h2>FYERS authentication failed.</h2>"
                b"<p>No auth_code was received.</p>"
            )

    def log_message(self, format, *args):
        pass


def main():
    global auth_code

    session = fyersModel.SessionModel(
        client_id=CLIENT_ID,
        secret_key=SECRET_KEY,
        redirect_uri=REDIRECT_URI,
        response_type="code",
        grant_type="authorization_code",
    )

    auth_url = session.generate_authcode()

    server = HTTPServer(("127.0.0.1", 5000), CallbackHandler)

    print()
    print("=" * 60)
    print("FYERS AUTHENTICATION")
    print("=" * 60)
    print()
    print("Starting local callback server:")
    print(REDIRECT_URI)
    print()
    print("Opening FYERS login in your browser...")
    print()

    threading.Thread(target=server.serve_forever, daemon=True).start()

    webbrowser.open(auth_url)

    print("Complete the FYERS login in your browser.")
    print("Waiting for the callback...")
    print()

    while auth_code is None:
        pass

    server.shutdown()

    print("Auth code received.")
    print("Exchanging auth code for access token...")
    print()

    session.set_token(auth_code)
    response = session.generate_token()

    if response.get("s") != "ok":
        print("TOKEN GENERATION FAILED")
        print(response)
        return

    token = response.get("access_token")

    if not token:
        print("No access token returned.")
        print(response)
        return

    print("=" * 60)
    print("SUCCESS — ACCESS TOKEN GENERATED")
    print("=" * 60)
    print()
    print("The token was generated successfully.")
    print("It will NOT be printed to the terminal.")
    print()

    with open(".env", "a", encoding="utf-8") as f:
        f.write("\nFYERS_CLIENT_ID=" + CLIENT_ID + "\n")
        f.write("FYERS_ACCESS_TOKEN=" + token + "\n")

    print("Access token saved to .env")
    print()


if __name__ == "__main__":
    main()