"""Runtime-boundary tests for AgentPact browser adapters."""

from __future__ import annotations

import hashlib

import pytest

from enterprise.browser_loop.contracts import ActionKind, AuthorizedAction, BrowserAction
from enterprise.browser_loop.ports import StaleObservationError
from enterprise.browser_loop.runtime import PlaywrightPageRuntime, SkyvernScraperRuntimeAdapter
from enterprise.governance.contracts import ExecutionAuthorization, ExecutionEffect
from enterprise.governance.execution_profiles import ExecutionMechanism, ExecutionProfile


class FakeLocator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.first = self

    async def count(self) -> int:
        return 1

    async def click(self) -> None:
        self.calls.append(("click", None))

    async def fill(self, value: str) -> None:
        self.calls.append(("fill", value))

    async def select_option(self, value: str) -> None:
        self.calls.append(("select_option", value))

    async def press(self, value: str) -> None:
        self.calls.append(("press", value))


class FakePage:
    url = "https://enterprise.example.test/form"

    def __init__(self) -> None:
        self.html = "<html><button id='submit'>Submit</button></html>"
        self.locators: dict[str, FakeLocator] = {}

    async def content(self) -> str:
        return self.html

    async def title(self) -> str:
        return "Enterprise form"

    async def screenshot(self, **_kwargs) -> bytes:
        return b"png"

    async def evaluate(self, script: str, arg=None):
        if "querySelectorAll" in script:
            return [
                {
                    "selector": "#submit",
                    "tag_name": "button",
                    "role": None,
                    "name": "Submit",
                    "text": "Submit",
                    "enabled": True,
                }
            ]
        return [script, arg]

    def locator(self, selector: str) -> FakeLocator:
        return self.locators.setdefault(selector, FakeLocator())

    async def goto(self, url: str, **_kwargs) -> None:
        self.url = url

    async def wait_for_timeout(self, _milliseconds: float) -> None:
        return None


def _command(*, page: FakePage, element_id: str = "ap-0000") -> AuthorizedAction:
    fingerprint = "a" * 64
    observation_id = "b" * 64
    snapshot_hash = hashlib.sha256(f"{page.url}\n{page.html}".encode()).hexdigest()
    return AuthorizedAction(
        action=BrowserAction(kind=ActionKind.CLICK, operation="submit", element_id=element_id),
        action_fingerprint=fingerprint,
        observation_id=observation_id,
        expected_snapshot_hash=snapshot_hash,
        authorization=ExecutionAuthorization(
            permit_id="permit-runtime-001",
            action_fingerprint=fingerprint,
            observation_hash=observation_id,
            idempotency_key="idem-runtime-001",
            effect=ExecutionEffect.EXTERNAL_WRITE,
        ),
        execution_profile=ExecutionProfile(
            mechanism=ExecutionMechanism.LOCATOR,
            evidence_refs=[observation_id],
        ),
    )


@pytest.mark.asyncio
async def test_playwright_runtime_observes_and_executes_fresh_authorized_locator() -> None:
    page = FakePage()
    runtime = PlaywrightPageRuntime(page, capture_screenshot=True)

    observation = await runtime.observe()
    result = await runtime.execute(_command(page=page))

    assert observation.elements[0].element_id == "ap-0000"
    assert observation.screenshots == (b"png",)
    assert result.completed is True
    assert page.locators["#submit"].calls == [("click", None)]


@pytest.mark.asyncio
async def test_playwright_runtime_rejects_dom_change_before_browser_call() -> None:
    page = FakePage()
    runtime = PlaywrightPageRuntime(page, capture_screenshot=False)
    await runtime.observe()
    command = _command(page=page)
    page.html = "<html><button id='submit'>Changed</button></html>"

    with pytest.raises(StaleObservationError):
        await runtime.execute(command)

    assert not page.locators


class FakeScrapedPage:
    id_to_css_dict = {"skyvern-1": "#submit"}
    id_to_element_dict = {
        "skyvern-1": {
            "tagName": "button",
            "text": "Submit",
            "attributes": {"aria-label": "Submit"},
        }
    }
    screenshots = [b"skyvern-png"]

    def build_element_tree(self) -> str:
        return '<button unique_id="skyvern-1">Submit</button>'


class FakeSkyvernBrowserState:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.scrape_calls = []

    async def must_get_working_page(self):
        return self.page

    async def scrape_website(self, **kwargs):
        self.scrape_calls.append(kwargs)
        return FakeScrapedPage()


@pytest.mark.asyncio
async def test_skyvern_adapter_uses_only_scraper_snapshot_and_playwright_page() -> None:
    page = FakePage()
    browser_state = FakeSkyvernBrowserState(page)
    runtime = SkyvernScraperRuntimeAdapter(browser_state)

    observation = await runtime.observe()
    result = await runtime.execute(_command(page=page, element_id="skyvern-1"))

    assert observation.model_dom.startswith("<button")
    assert observation.screenshots == (b"skyvern-png",)
    assert browser_state.scrape_calls[0]["draw_boxes"] is False
    assert result.completed is True
    assert page.locators["#submit"].calls == [("click", None)]
