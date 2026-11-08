"""Synthetic loopback-only fixture. Importing this module starts nothing."""

from collections import Counter
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from time import sleep
from typing import Iterator

from sofia.tools.browser_policy import (
    BrowserDownload,
    BrowserElement,
    BrowserForm,
    BrowserOriginPolicy,
    FixtureDocument,
)

DOWNLOAD = b"SOFIA synthetic fixture download.\n"
LANDING = b"""<!doctype html><html><head><title>Synthetic fixture</title></head><body>
<a data-testid="next" href="/next">Next</a>
<p data-testid="status">ready</p>
<input data-testid="search" type="text" autocomplete="off">
<button data-testid="benign" type="button">Synthetic change</button>
<form data-testid="contact" method="post" action="/submitted">
<input data-testid="message" type="text" name="message" autocomplete="off">
<button data-testid="send" type="submit">Send synthetic form</button></form>
<a data-testid="download" href="/download">Download synthetic text</a>
<script>
document.querySelector('[data-testid="benign"]').addEventListener('click', () => {
document.querySelector('[data-testid="status"]').textContent = 'changed';
});
</script></body></html>"""
SUCCESS = b"""<!doctype html><html><head><title>Fixture result</title></head><body>
<p data-testid="status">submitted</p>
<a data-testid="download" href="/download">Download synthetic text</a>
</body></html>"""


def fixture_policy(origin: str) -> BrowserOriginPolicy:
    return BrowserOriginPolicy(
        "fixture",
        origin,
        (
            FixtureDocument("/", LANDING),
            FixtureDocument("/next", LANDING),
            FixtureDocument("/redirect", LANDING),
        ),
        (
            BrowserElement("status", "status", "observe"),
            BrowserElement("search", "search", "fill"),
            BrowserElement("message", "message", "fill"),
            BrowserElement("benign", "benign", "click", "status", "changed"),
        ),
        (
            BrowserForm(
                "contact",
                "contact",
                "send",
                ("message",),
                "/submitted",
                FixtureDocument("/submitted", SUCCESS),
            ),
        ),
        (BrowserDownload("text", "download", "/download", "fixture.txt", DOWNLOAD),),
    )


class FixtureServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), FixtureHandler)
        self.counts: Counter[tuple[str, str]] = Counter()
        self.pages = {
            "/": LANDING,
            "/next": LANDING,
            "/submitted": SUCCESS,
            "/download": DOWNLOAD,
        }
        self.redirect = "http://127.0.0.1:1/denied"
        self.last_submission = b""

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"


class FixtureHandler(BaseHTTPRequestHandler):
    server: FixtureServer

    def log_message(self, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        self.server.counts[("GET", self.path)] += 1
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", self.server.redirect)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = self.server.pages.get(self.path)
        self.send_response(200 if body is not None else 404)
        body = body or b"missing"
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        self.server.counts[("POST", self.path)] += 1
        size = int(self.headers.get("Content-Length", "0"))
        if self.path != "/submitted" or not 0 < size <= 8192:
            self.send_error(400)
            return
        self.server.last_submission = self.rfile.read(size)
        self.send_response(200)
        self.send_header("Content-Length", str(len(SUCCESS)))
        self.end_headers()
        self.wfile.write(SUCCESS)


@contextmanager
def local_fixture() -> Iterator[FixtureServer]:
    server = FixtureServer()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    with local_fixture() as server:
        print(
            "Synthetic fixture running on an ephemeral loopback port. Ctrl+C to stop."
        )
        try:
            while True:
                sleep(1)
        except KeyboardInterrupt:
            pass
