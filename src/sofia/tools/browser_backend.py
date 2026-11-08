"""Private synchronous Chromium backend and bounded numeric-loopback HTTP broker.

No request is continued or fetched by Chromium. Only reviewed exact fixture bytes
are fulfilled, without forwarding server headers or following redirects.
"""

import base64
import hashlib
import http.client
from collections.abc import Callable
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit

from sofia.tools.browser_policy import (
    FIXTURE_SCRIPT,
    BrowserDownload,
    BrowserElement,
    BrowserError,
    BrowserForm,
    BrowserOriginPolicy,
    safe_filename,
)
from sofia.tools.registry import ToolContext


def fixture_request(
    policy: BrowserOriginPolicy,
    path: str,
    expected: bytes,
    context: ToolContext,
    timeout: float,
    *,
    payload: bytes | None = None,
) -> bytes:
    """No DNS, proxy environment, cookies, credentials, redirect or response logging."""
    context.checkpoint()
    policy.path(policy.origin + path)
    remaining = min(timeout, context.deadline - context._clock())
    if remaining <= 0:
        context.checkpoint()
    connection = http.client.HTTPConnection(
        "127.0.0.1", urlsplit(policy.origin).port, timeout=remaining
    )
    try:
        connection.request(
            "GET" if payload is None else "POST",
            path,
            body=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"}
            if payload is not None
            else {},
        )
        context.checkpoint()
        response = connection.getresponse()
        if response.status != 200:
            raise BrowserError("fixture_response_denied")
        if response.getheader("Content-Encoding") not in (None, "identity"):
            raise BrowserError("fixture_response_denied")
        if response.getheader("Content-Length") != str(len(expected)):
            raise BrowserError("fixture_response_denied")
        body = bytearray()
        while len(body) <= len(expected):
            context.checkpoint()
            if connection.sock is not None:
                connection.sock.settimeout(
                    max(0.001, min(timeout, context.deadline - context._clock()))
                )
            chunk = response.read1(min(4096, len(expected) + 1 - len(body)))
            if not chunk:
                break
            body.extend(chunk)
        if bytes(body) != expected:
            raise BrowserError("fixture_response_denied")
        context.checkpoint()
        return bytes(body)
    except (OSError, http.client.HTTPException):
        raise BrowserError("fixture_unavailable") from None
    finally:
        connection.close()


class BrowserBackend(Protocol):
    def start(self, session: str, context: ToolContext) -> None: ...
    def close(self, session: str) -> None: ...
    def close_all(self) -> None: ...
    def navigate(
        self, session: str, policy: BrowserOriginPolicy, path: str, context: ToolContext
    ) -> None: ...
    def observe(
        self, session: str, rule: BrowserElement, context: ToolContext
    ) -> str: ...
    def fill(
        self, session: str, rule: BrowserElement, text: str, context: ToolContext
    ) -> bool: ...
    def click(
        self, session: str, rule: BrowserElement, context: ToolContext
    ) -> None: ...
    def form_state(
        self,
        session: str,
        policy: BrowserOriginPolicy,
        form: BrowserForm,
        context: ToolContext,
    ) -> str: ...
    def submit(
        self,
        session: str,
        policy: BrowserOriginPolicy,
        form: BrowserForm,
        state: str,
        context: ToolContext,
    ) -> None: ...
    def download(
        self,
        session: str,
        policy: BrowserOriginPolicy,
        rule: BrowserDownload,
        context: ToolContext,
    ) -> bytes: ...
    def download_state(
        self,
        session: str,
        policy: BrowserOriginPolicy,
        rule: BrowserDownload,
        context: ToolContext,
    ) -> str: ...


class PlaywrightBrowserBackend:
    def __init__(self, timeout: float = 5.0) -> None:
        self._timeout = timeout
        self._driver: Any = None
        self._browser: Any = None
        self._sessions: dict[str, tuple[Any, Any]] = {}
        self._tickets: dict[str, Callable[[Any], None]] = {}
        self._downloads: set[str] = set()
        self._faults: set[str] = set()

    def start(self, session: str, context: ToolContext) -> None:
        context.checkpoint()
        if self._driver is None:
            try:
                from playwright.sync_api import sync_playwright
            except ImportError:
                raise BrowserError("browser_dependency_missing") from None
            try:
                self._driver = sync_playwright().start()
                self._browser = self._driver.chromium.launch(
                    headless=True,
                    chromium_sandbox=True,
                    timeout=self._timeout * 1000,
                    # Do not inherit protocol debugging or a user's browser environment.
                    env={},
                )
            except Exception:
                self.close_all()
                raise BrowserError("browser_launch_failed") from None
        native = None
        try:
            context.checkpoint()
            native = self._browser.new_context(
                offline=True,
                service_workers="block",
                permissions=[],
                accept_downloads=True,
                storage_state={"cookies": [], "origins": []},
            )
            native.set_default_timeout(self._timeout * 1000)
            native.set_default_navigation_timeout(self._timeout * 1000)
            page = native.new_page()
            self._sessions[session] = native, page
            native.route("**/*", lambda route: self._route(session, route))
            # Offline networking and CSP connect-src deny WebSockets. Do not create
            # a WebSocket route: that API can itself override offline emulation.
            native.on(
                "page",
                lambda new: (
                    self._event(session, lambda: new.close())
                    if new is not page
                    else None
                ),
            )
            page.on("dialog", lambda dialog: self._event(session, dialog.dismiss))
            page.on(
                "download",
                lambda download: (
                    self._event(session, download.cancel)
                    if session not in self._downloads
                    else None
                ),
            )
            page.on("filechooser", lambda chooser: None)
            context.checkpoint()
        except BaseException:
            if native is not None:
                self._event(session, native.close)
            self._sessions.pop(session, None)
            raise

    def _route(self, session: str, route: Any) -> None:
        ticket = self._tickets.pop(session, None)
        try:
            page = self._sessions[session][1]
            request = route.request
            if ticket is None or request.frame != page.main_frame:
                self._event(session, route.abort)
                return
            ticket(route)
        except Exception:
            self._faults.add(session)
            self._event(session, route.abort)

    def _event(self, session: str, action: Callable[[], None]) -> None:
        # Event callback exceptions otherwise reach asyncio's raw exception logger.
        try:
            action()
        except Exception:
            self._faults.add(session)

    def close(self, session: str) -> None:
        self._tickets.pop(session, None)
        self._downloads.discard(session)
        self._faults.discard(session)
        native = self._sessions.pop(session, None)
        if native:
            native[0].close()

    def close_all(self) -> None:
        failed = False
        for session in tuple(self._sessions):
            try:
                self.close(session)
            except Exception:
                failed = True
        browser, driver = self._browser, self._driver
        self._browser = self._driver = None
        for resource, method in ((browser, "close"), (driver, "stop")):
            if resource is not None:
                try:
                    getattr(resource, method)()
                except Exception:
                    failed = True
        if failed:
            raise BrowserError("cleanup_failed")

    @staticmethod
    def _csp() -> str:
        # Even controller-supplied manifests cannot opt into arbitrary JS/WebRTC.
        script_hash = base64.b64encode(hashlib.sha256(FIXTURE_SCRIPT).digest()).decode()
        return (
            "default-src 'none'; base-uri 'none'; frame-src 'none'; worker-src 'none'; connect-src 'none'; object-src 'none'; script-src "
            + "'sha256-"
            + script_hash
            + "'"
            + "; form-action 'self'"
        )

    def _transfer(
        self,
        session: str,
        policy: BrowserOriginPolicy,
        path: str,
        expected: bytes,
        context: ToolContext,
        trigger: Callable[[], None],
        *,
        payload: bytes | None = None,
        filename: str | None = None,
    ) -> None:
        admitted = False
        failure: BaseException | None = None

        def ticket(route: Any) -> None:
            nonlocal admitted, failure
            try:
                context.checkpoint()
                req = route.request
                if (
                    req.url != policy.origin + path
                    or req.method != ("GET" if payload is None else "POST")
                    or (payload is not None and req.post_data_buffer != payload)
                ):
                    raise BrowserError("request_denied")
                body = fixture_request(
                    policy, path, expected, context, self._timeout, payload=payload
                )
                headers = {
                    "Content-Type": "text/html; charset=utf-8",
                    "Content-Security-Policy": self._csp(),
                    "Cache-Control": "no-store",
                }
                if filename is not None:
                    headers = {
                        "Content-Type": "text/plain; charset=utf-8",
                        "Content-Disposition": f'attachment; filename="{safe_filename(filename)}"',
                    }
                context.checkpoint()
                route.fulfill(status=200, headers=headers, body=body)
                admitted = True
            except BaseException as error:
                failure = error
                self._event(session, route.abort)

        context.checkpoint()
        self._tickets[session] = ticket
        try:
            trigger()
        finally:
            self._tickets.pop(session, None)
            if failure is not None:
                raise failure
        context.checkpoint()
        if session in self._faults:
            raise BrowserError("browser_failed")
        if not admitted:
            raise BrowserError("request_denied")

    def navigate(
        self, session: str, policy: BrowserOriginPolicy, path: str, context: ToolContext
    ) -> None:
        document = next((x for x in policy.documents if x.path == path), None)
        if document is None:
            raise BrowserError("unknown_document")
        page = self._sessions[session][1]
        self._transfer(
            session,
            policy,
            path,
            document.body,
            context,
            lambda: page.goto(policy.origin + path, wait_until="load"),
        )
        if page.url != policy.origin + path:
            raise BrowserError("page_changed")

    def _element(self, session: str, test_id: str, context: ToolContext) -> Any:
        context.checkpoint()
        if session in self._faults:
            raise BrowserError("browser_failed")
        page = self._sessions[session][1]
        target = page.get_by_test_id(test_id)
        if len(page.frames) != 1 or target.count() != 1:
            raise BrowserError("ambiguous_target")
        if (
            not target.is_visible()
            or not target.is_enabled()
            or target.get_attribute("contenteditable") is not None
            or target.evaluate("e => e.isContentEditable")
        ):
            raise BrowserError("unsafe_target")
        return target

    def observe(self, session: str, rule: BrowserElement, context: ToolContext) -> str:
        target = self._element(session, rule.test_id, context)
        # Read a bounded prefix in the renderer, never transfer a complete DOM/text.
        text = target.evaluate(
            "e => { if (!['SPAN','P','OUTPUT'].includes(e.tagName)) throw Error(); return e.textContent.slice(0,257); }"
        )
        if type(text) is not str or len(text) > 256:
            raise BrowserError("observation_limit")
        return text

    def fill(
        self, session: str, rule: BrowserElement, text: str, context: ToolContext
    ) -> bool:
        target = self._element(session, rule.test_id, context)
        if not target.evaluate(
            "e => e.tagName === 'INPUT' && e.type === 'text' && !e.readOnly && !e.autocomplete.match(/password|cc-|one-time-code/)"
        ):
            raise BrowserError("unsafe_target")
        context.checkpoint()
        target.fill(text)
        return target.input_value() == text

    def click(self, session: str, rule: BrowserElement, context: ToolContext) -> None:
        target = self._element(session, rule.test_id, context)
        if not target.evaluate(
            "e => e.tagName === 'BUTTON' && e.type === 'button' && !e.form"
        ):
            raise BrowserError("unsafe_target")
        context.checkpoint()
        target.click()

    def form_state(
        self,
        session: str,
        policy: BrowserOriginPolicy,
        form: BrowserForm,
        context: ToolContext,
    ) -> str:
        page = self._sessions[session][1]
        target = self._element(session, form.test_id, context)
        state = target.evaluate("""e => {
            if(e.tagName !== 'FORM' || e.method !== 'post' || e.enctype !== 'application/x-www-form-urlencoded' || e.target) throw Error();
            const controls = Array.from(e.elements);
            if(controls.length > 9) throw Error();
            const fields = controls.filter(x => x.tagName === 'INPUT');
            if(fields.some(x => x.type !== 'text' || x.disabled || x.readOnly || !x.name || x.value.length > 512 || !x.checkVisibility())) throw Error();
            if(controls.some(x => x.tagName !== 'INPUT' && !(x.tagName === 'BUTTON' && x.type === 'submit' && !x.name && !x.hasAttribute('formaction') && !x.hasAttribute('formmethod')))) throw Error();
            return {action:e.action, fields:fields.map(x=>[x.name,x.value]), html:e.outerHTML.slice(0,16385)};
        }""")
        if (
            state["action"] != policy.origin + form.destination
            or tuple(x[0] for x in state["fields"]) != form.fields
            or len(state["html"]) > 16384
        ):
            raise BrowserError("unsafe_form")
        # Includes exact URL and bounded markup to invalidate navigation/form changes.
        import json

        return json.dumps([page.url, state], sort_keys=True)

    def submit(
        self,
        session: str,
        policy: BrowserOriginPolicy,
        form: BrowserForm,
        state: str,
        context: ToolContext,
    ) -> None:
        import json

        fields = json.loads(state)[1]["fields"]
        payload = urlencode([tuple(pair) for pair in fields]).encode("ascii")
        page = self._sessions[session][1]
        target = self._element(session, form.submit_test_id, context)
        if not target.evaluate(
            "(e, id) => e.tagName === 'BUTTON' && e.type === 'submit' && e.form?.dataset.testid === id",
            form.test_id,
        ):
            raise BrowserError("unsafe_form")
        self._transfer(
            session,
            policy,
            form.destination,
            form.response.body,
            context,
            lambda: target.click(),
            payload=payload,
        )
        page.wait_for_load_state("load")
        if page.url != policy.origin + form.destination:
            raise BrowserError("submission_unverified")

    def download(
        self,
        session: str,
        policy: BrowserOriginPolicy,
        rule: BrowserDownload,
        context: ToolContext,
    ) -> bytes:
        page = self._sessions[session][1]
        target = self._element(session, rule.test_id, context)
        if not target.evaluate(
            "(e, url) => e.tagName === 'A' && e.href === url && !e.target",
            policy.origin + rule.path,
        ):
            raise BrowserError("unsafe_target")
        download = None
        try:
            self._downloads.add(session)
            with page.expect_download(timeout=self._timeout * 1000) as pending:
                self._transfer(
                    session,
                    policy,
                    rule.path,
                    rule.body,
                    context,
                    lambda: target.click(),
                    filename=rule.filename,
                )
            download = pending.value
            if safe_filename(download.suggested_filename) != rule.filename:
                raise BrowserError("unsafe_filename")
            context.checkpoint()
            path = download.path()
            if path is None or path.stat().st_size != len(rule.body):
                raise BrowserError("download_unverified")
            with path.open("rb") as stream:
                body = stream.read(len(rule.body) + 1)
            if body != rule.body:
                raise BrowserError("download_unverified")
            return body
        finally:
            self._downloads.discard(session)
            if download is not None:
                download.delete()

    def download_state(
        self,
        session: str,
        policy: BrowserOriginPolicy,
        rule: BrowserDownload,
        context: ToolContext,
    ) -> str:
        page = self._sessions[session][1]
        policy.path(page.url)
        target = self._element(session, rule.test_id, context)
        if not target.evaluate(
            "(e, url) => e.tagName === 'A' && e.href === url && !e.target",
            policy.origin + rule.path,
        ):
            raise BrowserError("unsafe_target")
        markup = target.evaluate("e => e.outerHTML.slice(0,1025)")
        if type(markup) is not str or len(markup) > 1024:
            raise BrowserError("unsafe_target")
        return page.url + "\n" + markup
