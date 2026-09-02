"""Local browser runtime adapters for the AgentPact operation loop."""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from enterprise.governance.execution_profiles import (
    ExecutionMechanism,
    governed_execution_profile,
    require_allowed_profile,
    require_execution_mechanism,
)

from .contracts import (
    ActionKind,
    AuthorizedAction,
    BrowserActionResult,
    BrowserElement,
    BrowserFrame,
    BrowserPageState,
    RawBrowserObservation,
)
from .ports import BrowserRuntimeError, BrowserSession, StaleObservationError


class PlaywrightPageRuntime:
    """Operate an injected Playwright-compatible page without product services."""

    def __init__(
        self,
        page: Any,
        *,
        capture_screenshot: bool = True,
        max_elements: int = 500,
        clock: Callable[[], datetime] | None = None,
        close_page: Callable[[], Any] | None = None,
    ) -> None:
        if max_elements < 1:
            raise ValueError("max_elements must be positive")
        self._page = page
        self._capture_screenshot = capture_screenshot
        self._max_elements = max_elements
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._close_page = close_page
        self._selectors: dict[str, str] = {}
        self._closed = False

    async def observe(self) -> RawBrowserObservation:
        self._ensure_open()
        page_state = await self.page_state()
        model_dom, elements = await self._capture_interactable_tree()
        screenshots = (await self.screenshot(),) if self._capture_screenshot else ()
        return RawBrowserObservation(
            url=page_state.url,
            title=page_state.title,
            page_html=page_state.page_html,
            model_dom=model_dom,
            screenshots=screenshots,
            elements=tuple(elements),
            iframes=await self.enumerate_iframes(),
            captured_at=self._clock(),
        )

    async def fresh_observation(self) -> RawBrowserObservation:
        """Capture a new observation; callers must never reuse an old snapshot."""

        return await self.observe()

    async def close(self) -> None:
        if self._closed:
            return
        if self._close_page is None:
            raise BrowserRuntimeError(
                "Browser runtime does not own the injected page session", effect_may_have_started=False
            )
        try:
            result = self._close_page()
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            raise BrowserRuntimeError(
                f"Browser runtime close failed: {type(exc).__name__}", effect_may_have_started=False
            ) from exc
        self._closed = True

    async def page_state(self) -> BrowserPageState:
        """Read the current URL, title, and HTML without invoking any action."""

        self._ensure_open()
        try:
            return BrowserPageState(
                url=str(self._page.url),
                title=await self._page.title(),
                page_html=await self._page.content(),
            )
        except Exception as exc:
            raise BrowserRuntimeError(
                f"Browser page state failed: {type(exc).__name__}", effect_may_have_started=False
            ) from exc

    async def screenshot(self) -> bytes:
        """Capture one current viewport screenshot owned by this runtime."""

        self._ensure_open()
        try:
            return await self._page.screenshot(type="png")
        except Exception as exc:
            raise BrowserRuntimeError(
                f"Browser screenshot failed: {type(exc).__name__}", effect_may_have_started=False
            ) from exc

    async def enumerate_iframes(self) -> tuple[BrowserFrame, ...]:
        """Return deterministic metadata for child frames, never frame handles."""

        self._ensure_open()
        try:
            frames = list(getattr(self._page, "frames", ()))
        except Exception as exc:
            raise BrowserRuntimeError(
                f"Browser frame enumeration failed: {type(exc).__name__}", effect_may_have_started=False
            ) from exc
        if not frames:
            return ()
        main_frame = getattr(self._page, "main_frame", None) or frames[0]
        frame_ids: dict[int, str] = {id(main_frame): "main"}
        for frame in frames:
            if frame is not main_frame:
                frame_ids.setdefault(id(frame), f"frame-{len(frame_ids):04d}")
        result: list[BrowserFrame] = []
        try:
            for frame in frames:
                if frame is main_frame:
                    continue
                frame_id = frame_ids[id(frame)]
                parent = getattr(frame, "parent_frame", None)
                parent_id = frame_ids.get(id(parent)) if parent is not None else None
                name = getattr(frame, "name", None)
                result.append(
                    BrowserFrame(
                        frame_id=frame_id,
                        url=str(getattr(frame, "url", "")),
                        name=_optional_string(name),
                        parent_frame_id=parent_id,
                    )
                )
        except Exception as exc:
            raise BrowserRuntimeError(
                f"Browser frame metadata failed: {type(exc).__name__}", effect_may_have_started=False
            ) from exc
        return tuple(result)

    async def normalized_interactable_tree(self) -> str:
        """Capture the normalized main-document interactable tree."""

        self._ensure_open()
        model_dom, _elements = await self._capture_interactable_tree()
        return model_dom

    async def _capture_interactable_tree(self) -> tuple[str, list[BrowserElement]]:
        try:
            raw_elements = await self._page.evaluate(_INTERACTABLES_SCRIPT)
        except Exception as exc:
            raise BrowserRuntimeError(
                f"Browser interactable tree failed: {type(exc).__name__}", effect_may_have_started=False
            ) from exc
        if not isinstance(raw_elements, list):
            raise BrowserRuntimeError(
                "Browser returned an invalid interactable snapshot", effect_may_have_started=False
            )
        elements: list[BrowserElement] = []
        selectors: dict[str, str] = {}
        model_elements: list[dict[str, Any]] = []
        for index, item in enumerate(raw_elements[: self._max_elements]):
            if not isinstance(item, dict) or not isinstance(item.get("selector"), str):
                continue
            element_id = f"ap-{index:04d}"
            selectors[element_id] = item["selector"]
            element = BrowserElement(
                element_id=element_id,
                tag_name=str(item.get("tag_name") or "unknown"),
                role=_optional_string(item.get("role")),
                name=_optional_string(item.get("name")),
                text=_optional_string(item.get("text")),
                enabled=bool(item.get("enabled", True)),
            )
            elements.append(element)
            model_elements.append(element.model_dump(mode="json", exclude_none=True))
        self._selectors = selectors
        return json.dumps(model_elements, ensure_ascii=True, separators=(",", ":")), elements

    async def execute(self, command: AuthorizedAction) -> BrowserActionResult:
        await self.preflight(command)
        return await self.execute_preflighted(command)

    async def preflight(self, command: AuthorizedAction) -> None:
        """Perform only freshness, binding, profile, and selector checks."""

        await self._validate_command(command)
        if command.action.kind in {
            ActionKind.CLICK,
            ActionKind.INPUT_TEXT,
            ActionKind.SELECT_OPTION,
            ActionKind.KEYPRESS,
        }:
            selector = self._selectors.get(command.action.element_id or "")
            if selector is None:
                raise StaleObservationError("Observed element is no longer addressable")
            if await self._page.locator(selector).count() != 1:
                raise StaleObservationError("Observed element is no longer unique")

    async def execute_preflighted(self, command: AuthorizedAction) -> BrowserActionResult:
        """Invoke the browser once after a caller has crossed its durable boundary."""

        action = command.action
        effect_started = False
        try:
            with governed_execution_profile(command.execution_profile):
                if action.kind in {
                    ActionKind.CLICK,
                    ActionKind.INPUT_TEXT,
                    ActionKind.SELECT_OPTION,
                    ActionKind.KEYPRESS,
                }:
                    require_execution_mechanism(ExecutionMechanism.LOCATOR)
                    selector = self._selectors.get(action.element_id or "")
                    if selector is None:
                        raise StaleObservationError("Observed element is no longer addressable")
                    locator = self._page.locator(selector).first
                    effect_started = True
                    if action.kind is ActionKind.CLICK:
                        await locator.click()
                    elif action.kind is ActionKind.INPUT_TEXT:
                        await locator.fill(action.text or "")
                    elif action.kind is ActionKind.SELECT_OPTION:
                        await locator.select_option(action.option_value)
                    else:
                        for key in action.keys:
                            await locator.press(key)
                elif action.kind is ActionKind.GOTO_URL:
                    effect_started = True
                    await self._page.goto(action.url, wait_until="domcontentloaded")
                elif action.kind is ActionKind.SCROLL:
                    effect_started = True
                    await self._page.evaluate("([x, y]) => window.scrollBy(x, y)", [action.scroll_x, action.scroll_y])
                elif action.kind is ActionKind.WAIT:
                    effect_started = True
                    await self._page.wait_for_timeout(action.wait_seconds * 1000)
                else:  # pragma: no cover - exhaustive enum guard
                    raise BrowserRuntimeError("Unsupported AgentPact browser action", effect_may_have_started=False)
        except StaleObservationError:
            raise
        except BrowserRuntimeError:
            raise
        except Exception as exc:
            raise BrowserRuntimeError(
                f"Playwright action failed: {type(exc).__name__}",
                effect_may_have_started=effect_started,
            ) from exc
        return BrowserActionResult(
            completed=True,
            effect_may_have_started=effect_started,
            detail_code="ACTION_COMPLETED",
        )

    async def _validate_command(self, command: AuthorizedAction) -> None:
        self._ensure_open()
        authorization = command.authorization
        if not (
            hmac.compare_digest(authorization.action_fingerprint, command.action_fingerprint)
            and hmac.compare_digest(authorization.observation_hash, command.observation_id)
        ):
            raise BrowserRuntimeError("Execution authorization binding mismatch", effect_may_have_started=False)
        require_allowed_profile(effect=authorization.effect, profile=command.execution_profile)
        current_html = await self._page.content()
        current_hash = _snapshot_hash(str(self._page.url), current_html)
        if not hmac.compare_digest(current_hash, command.expected_snapshot_hash):
            raise StaleObservationError()

    def _ensure_open(self) -> None:
        if self._closed:
            raise BrowserRuntimeError("Browser runtime is closed", effect_may_have_started=False)


class SkyvernScraperRuntimeAdapter(PlaywrightPageRuntime):
    """Temporary adapter around Skyvern's local ``BrowserState.scrape_website``.

    The adapter consumes only the scraper result and the working Playwright page.
    It never imports or invokes ForgeAgent, ActionHandler, routes, workflows, DB,
    model handlers, artifact services, or account infrastructure.
    """

    def __init__(
        self,
        browser_state: Any,
        *,
        capture_screenshot: bool = True,
        max_elements: int = 500,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(
            page=None,
            capture_screenshot=capture_screenshot,
            max_elements=max_elements,
            clock=clock,
        )
        self._browser_state = browser_state

    async def observe(self) -> RawBrowserObservation:
        self._ensure_open()
        async def identity_cleanup(_page: Any, _url: str, tree: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return tree

        page = await self._browser_state.must_get_working_page()
        self._page = page
        scraped = await self._browser_state.scrape_website(
            url=str(page.url),
            cleanup_element_tree=identity_cleanup,
            take_screenshots=self._capture_screenshot,
            draw_boxes=False,
            scroll=False,
            max_screenshot_number=1,
            support_empty_page=True,
        )
        page_html = await page.content()
        elements: list[BrowserElement] = []
        selectors: dict[str, str] = {}
        for element_id, selector in list(scraped.id_to_css_dict.items())[: self._max_elements]:
            raw = scraped.id_to_element_dict.get(element_id, {})
            attributes = raw.get("attributes") if isinstance(raw.get("attributes"), dict) else {}
            elements.append(
                BrowserElement(
                    element_id=str(element_id),
                    tag_name=str(raw.get("tagName") or "unknown"),
                    role=_optional_string(attributes.get("role")),
                    name=_optional_string(attributes.get("aria-label") or attributes.get("name")),
                    text=_optional_string(raw.get("text")),
                    enabled=not bool(attributes.get("disabled", False)),
                )
            )
            selectors[str(element_id)] = str(selector)
        self._selectors = selectors
        return RawBrowserObservation(
            url=str(page.url),
            title=await page.title(),
            page_html=page_html,
            model_dom=scraped.build_element_tree(),
            screenshots=tuple(scraped.screenshots),
            elements=tuple(elements),
            iframes=await self.enumerate_iframes(),
            captured_at=self._clock(),
        )


class ManagedBrowserSession:
    """Own one AgentPact runtime and make close idempotent."""

    def __init__(self, session_id: str, runtime: PlaywrightPageRuntime) -> None:
        if not session_id:
            raise ValueError("session_id must be non-empty")
        self._session_id = session_id
        self._runtime = runtime
        self._closed = False

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def runtime(self) -> PlaywrightPageRuntime:
        return self._runtime

    async def close(self) -> None:
        if self._closed:
            return
        await self._runtime.close()
        self._closed = True


class PlaywrightBrowserSessionFactory:
    """Create AgentPact-owned page sessions from injected lifecycle callbacks."""

    def __init__(
        self,
        open_page: Callable[[str | None], Any],
        *,
        runtime_options: dict[str, Any] | None = None,
        session_id_factory: Callable[[Any, str | None], str] | None = None,
    ) -> None:
        self._open_page = open_page
        self._runtime_options = dict(runtime_options or {})
        self._session_id_factory = session_id_factory or (
            lambda _page, requested: requested or f"agentpact-{uuid4().hex}"
        )

    async def open(self, *, session_id: str | None = None) -> BrowserSession:
        try:
            page = self._open_page(session_id)
            if inspect.isawaitable(page):
                page = await page
        except BrowserRuntimeError:
            raise
        except Exception as exc:
            raise BrowserRuntimeError(
                f"Browser session open failed: {type(exc).__name__}", effect_may_have_started=False
            ) from exc
        if page is None:
            raise BrowserRuntimeError("Browser session did not provide a page", effect_may_have_started=False)
        close = getattr(page, "close", None)
        if not callable(close):
            raise BrowserRuntimeError("Browser page cannot be closed by the session owner", effect_may_have_started=False)
        runtime = PlaywrightPageRuntime(page, close_page=close, **self._runtime_options)
        return ManagedBrowserSession(
            self._session_id_factory(page, session_id),
            runtime,
        )

    async def create(self, *, session_id: str | None = None) -> BrowserSession:
        """Compatibility spelling for callers that use conventional factories."""

        return await self.open(session_id=session_id)


def _snapshot_hash(url: str, html: str) -> str:
    return hashlib.sha256(f"{url}\n{html}".encode("utf-8")).hexdigest()


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


_INTERACTABLES_SCRIPT = r"""
() => {
  const candidates = Array.from(document.querySelectorAll(
    'a[href],button,input,select,textarea,[role="button"],[role="link"],[contenteditable="true"],[tabindex]'
  ));
  const cssPath = (element) => {
    if (element.id) return `#${CSS.escape(element.id)}`;
    const parts = [];
    let node = element;
    while (node && node.nodeType === Node.ELEMENT_NODE && node !== document.documentElement) {
      let part = node.tagName.toLowerCase();
      const siblings = Array.from(node.parentElement?.children || []).filter(
        sibling => sibling.tagName === node.tagName
      );
      if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(node) + 1})`;
      parts.unshift(part);
      node = node.parentElement;
    }
    return parts.join(' > ');
  };
  return candidates.filter((element) => {
    const style = window.getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
  }).map((element) => ({
    selector: cssPath(element),
    tag_name: element.tagName.toLowerCase(),
    role: element.getAttribute('role'),
    name: element.getAttribute('aria-label') || element.getAttribute('name') || null,
    text: (element.innerText || element.getAttribute('placeholder') || '').trim().slice(0, 500),
    enabled: !element.disabled && element.getAttribute('aria-disabled') !== 'true'
  }));
}
"""
