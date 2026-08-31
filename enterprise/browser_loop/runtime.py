"""Local browser runtime adapters for the AgentPact operation loop."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

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
    RawBrowserObservation,
)
from .ports import BrowserRuntimeError, StaleObservationError


class PlaywrightPageRuntime:
    """Operate an injected Playwright-compatible page without product services."""

    def __init__(
        self,
        page: Any,
        *,
        capture_screenshot: bool = True,
        max_elements: int = 500,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if max_elements < 1:
            raise ValueError("max_elements must be positive")
        self._page = page
        self._capture_screenshot = capture_screenshot
        self._max_elements = max_elements
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._selectors: dict[str, str] = {}

    async def observe(self) -> RawBrowserObservation:
        page_html = await self._page.content()
        raw_elements = await self._page.evaluate(_INTERACTABLES_SCRIPT)
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
        screenshots = (await self._page.screenshot(type="png"),) if self._capture_screenshot else ()
        return RawBrowserObservation(
            url=str(self._page.url),
            title=await self._page.title(),
            page_html=page_html,
            model_dom=json.dumps(model_elements, ensure_ascii=True, separators=(",", ":")),
            screenshots=screenshots,
            elements=tuple(elements),
            captured_at=self._clock(),
        )

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
            captured_at=self._clock(),
        )


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
