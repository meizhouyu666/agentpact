# ruff: noqa: E402

from __future__ import annotations

import builtins
import importlib
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, AsyncIterator, Iterator
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


def _install_unrelated_runtime_import_shims() -> None:
    """Keep missing optional branches from blocking the real governed locator path."""

    if importlib.util.find_spec("jinja2") is None:
        local_app_data = Path(os.environ.get("LOCALAPPDATA", ""))
        site_packages = sorted(
            local_app_data.glob("Programs/Python/Python*/Lib/site-packages"),
            reverse=True,
        )
        for candidate in site_packages:
            if not (candidate / "jinja2").is_dir():
                continue
            sys.path.append(str(candidate))
            try:
                importlib.import_module("jinja2")
            finally:
                sys.path.remove(str(candidate))
            break
        if importlib.util.find_spec("jinja2") is None:
            raise RuntimeError("M4 could not locate the already-installed Jinja runtime")

    if importlib.util.find_spec("pyotp") is None:
        pyotp = ModuleType("pyotp")

        class UnavailableTOTP:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                raise RuntimeError("The M4 locator proof must not enter the TOTP path")

        pyotp.TOTP = UnavailableTOTP  # type: ignore[attr-defined]
        sys.modules["pyotp"] = pyotp

    if importlib.util.find_spec("cachetools") is None:
        cachetools = ModuleType("cachetools")

        class TTLCache(dict[Any, Any]):
            def __init__(self, *, maxsize: int, ttl: float) -> None:
                super().__init__()
                self.maxsize = maxsize
                self.ttl = ttl

        cachetools.TTLCache = TTLCache  # type: ignore[attr-defined]
        sys.modules["cachetools"] = cachetools

    if importlib.util.find_spec("aiofiles") is None:
        aiofiles = ModuleType("aiofiles")

        class AsyncFile:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                self._args = args
                self._kwargs = kwargs
                self._file: Any = None

            async def __aenter__(self) -> AsyncFile:
                self._file = builtins.open(*self._args, **self._kwargs)
                return self

            async def __aexit__(self, *_exc: Any) -> None:
                self._file.close()

            async def read(self, *args: Any, **kwargs: Any) -> Any:
                return self._file.read(*args, **kwargs)

            async def write(self, *args: Any, **kwargs: Any) -> Any:
                return self._file.write(*args, **kwargs)

        aiofiles.open = lambda *args, **kwargs: AsyncFile(*args, **kwargs)  # type: ignore[attr-defined]
        sys.modules["aiofiles"] = aiofiles


_install_unrelated_runtime_import_shims()


def _install_unrelated_skyvern_module_stubs() -> None:
    """Stub only handler branches that the approved locator proof forbids entering."""

    def unavailable(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("The M4 locator proof entered an unrelated Skyvern branch")

    def deterministic_select_prompt(template_name: str, **_kwargs: Any) -> str:
        if template_name != "normal-select":
            return unavailable(template_name, **_kwargs)
        return "Select the exact synthetic fault value supplied by the M4 test boundary."

    prompts = ModuleType("skyvern.forge.prompts")
    prompts.prompt_engine = SimpleNamespace(load_prompt=deterministic_select_prompt)  # type: ignore[attr-defined]
    sys.modules[prompts.__name__] = prompts

    files = ModuleType("skyvern.forge.sdk.api.files")
    files.check_downloading_files_and_wait_for_download_to_complete = unavailable  # type: ignore[attr-defined]
    files.download_file = unavailable  # type: ignore[attr-defined]
    files.get_download_dir = unavailable  # type: ignore[attr-defined]
    files.list_files_in_directory = unavailable  # type: ignore[attr-defined]
    sys.modules[files.__name__] = files

    llm_factory = ModuleType("skyvern.forge.sdk.api.llm.api_handler_factory")

    class LLMCallerManager:
        @staticmethod
        def get_llm_caller(_task_id: str) -> None:
            return None

    class LLMAPIHandlerFactory:
        get_override_llm_api_handler = staticmethod(unavailable)

    llm_factory.LLMCallerManager = LLMCallerManager  # type: ignore[attr-defined]
    llm_factory.LLMAPIHandlerFactory = LLMAPIHandlerFactory  # type: ignore[attr-defined]
    sys.modules[llm_factory.__name__] = llm_factory

    llm_exceptions = ModuleType("skyvern.forge.sdk.api.llm.exceptions")

    class LLMProviderError(Exception):
        pass

    llm_exceptions.LLMProviderError = LLMProviderError  # type: ignore[attr-defined]
    sys.modules[llm_exceptions.__name__] = llm_exceptions

    schema_validator = ModuleType("skyvern.forge.sdk.api.llm.schema_validator")
    schema_validator.validate_and_fill_extraction_result = unavailable  # type: ignore[attr-defined]
    sys.modules[schema_validator.__name__] = schema_validator

    bitwarden = ModuleType("skyvern.forge.sdk.services.bitwarden")
    bitwarden.BitwardenConstants = type("BitwardenConstants", (), {"TOTP": "__m4_unavailable_totp__"})  # type: ignore[attr-defined]
    sys.modules[bitwarden.__name__] = bitwarden

    credentials = ModuleType("skyvern.forge.sdk.services.credentials")
    credentials.AzureVaultConstants = type("AzureVaultConstants", (), {"TOTP": "__m4_unavailable_totp__"})  # type: ignore[attr-defined]
    credentials.OnePasswordConstants = type("OnePasswordConstants", (), {"TOTP": "__m4_unavailable_totp__"})  # type: ignore[attr-defined]
    sys.modules[credentials.__name__] = credentials

    trace = ModuleType("skyvern.forge.sdk.trace")

    def traced(*_decorator_args: Any, **_decorator_kwargs: Any) -> Any:
        def decorate(function: Any) -> Any:
            return function

        return decorate

    trace.traced = traced  # type: ignore[attr-defined]
    sys.modules[trace.__name__] = trace

    service_utils = ModuleType("skyvern.services.service_utils")
    service_utils.is_cua_task = unavailable  # type: ignore[attr-defined]
    sys.modules[service_utils.__name__] = service_utils

    action_service = ModuleType("skyvern.services.action_service")
    action_service.get_action_history = unavailable  # type: ignore[attr-defined]
    sys.modules[action_service.__name__] = action_service

    prompt_utils = ModuleType("skyvern.utils.prompt_engine")
    prompt_utils.CheckDateFormatResponse = type("CheckDateFormatResponse", (), {"model_validate": unavailable})  # type: ignore[attr-defined]
    prompt_utils.CheckPhoneNumberFormatResponse = type(  # type: ignore[attr-defined]
        "CheckPhoneNumberFormatResponse",
        (),
        {"model_validate": unavailable},
    )
    prompt_utils.load_prompt_with_elements = unavailable  # type: ignore[attr-defined]
    sys.modules[prompt_utils.__name__] = prompt_utils

    browser_factory = ModuleType("skyvern.webeye.browser_factory")
    browser_factory.BrowserCleanupFunc = Any  # type: ignore[attr-defined]
    browser_factory.initialize_download_dir = unavailable  # type: ignore[attr-defined]
    sys.modules[browser_factory.__name__] = browser_factory

    image_resizer = ModuleType("skyvern.utils.image_resizer")
    image_resizer.Resolution = dict  # type: ignore[attr-defined]
    sys.modules[image_resizer.__name__] = image_resizer

    if importlib.util.find_spec("PIL") is None:
        pil = ModuleType("PIL")
        pil.Image = SimpleNamespace(open=unavailable, new=unavailable)  # type: ignore[attr-defined]
        sys.modules["PIL"] = pil

    token_counter = ModuleType("skyvern.utils.token_counter")
    token_counter.count_tokens = lambda text: len(text.encode("utf-8"))  # type: ignore[attr-defined]
    sys.modules[token_counter.__name__] = token_counter


_install_unrelated_skyvern_module_stubs()

from playwright.async_api import Browser, BrowserContext, Page, Playwright, Route, async_playwright
from sqlalchemy import event, pool, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from enterprise.governance.contracts import (
    DecisionOutcome,
    ExecutionAttemptStatus,
    ExecutionAuthorization,
    ExecutionEffect,
    PolicyDecision,
)
from enterprise.governance.execution_attempt_service import resolve_unknown_execution_attempt
from enterprise.governance.execution_profiles import ExecutionMechanism, ExecutionProfile
from enterprise.governance.models import (
    ExecutionAttemptModel,
    PendingActionModel,
    TaskContractModel,
)
from enterprise.governance.permit_service import issue_permit
from skyvern.config import settings
from skyvern.forge import app as forge_app
from skyvern.forge import set_force_app_instance
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.db.models import ActionModel, OrganizationModel, StepModel, TaskModel
from skyvern.forge.sdk.models import Step, StepStatus
from skyvern.forge.sdk.schemas.tasks import Task, TaskStatus
from skyvern.webeye.actions.actions import ClickAction, InputOrSelectContext, SelectOption, SelectOptionAction
from skyvern.webeye.actions.handler import ActionHandler
from skyvern.webeye.actions.responses import ActionSuccess
from skyvern.webeye.scraper import scraper as skyvern_scraper
from skyvern.webeye.scraper.scraped_page import ScrapedPage

RUN_ID = "finrpa-m4-20260728"
ORGANIZATION_ID = "org-m4-synthetic"
TASK_ID = "task-m4-synthetic-governed-browser"
STEP_ID = "step-m4-synthetic-governed-browser"
CONTRACT_ID = "contract-m4-synthetic-governed-browser"
HMAC_SECRET = "finrpa-m4-synthetic-browser-proof-only"


@dataclass
class CleanupEvidence:
    console_pid: int | None = None
    postgres_pid: int | None = None
    console_port: int | None = None
    postgres_port: int | None = None
    console_stopped: bool = False
    postgres_stopped: bool = False
    browser_closed: bool = False
    temp_root: Path | None = None
    temp_root_removed: bool = False
    retained_errors: list[str] = field(default_factory=list)


@dataclass
class IsolatedM4Environment:
    repository: Path
    root: Path
    database_url: str
    console_url: str
    console_process: subprocess.Popen[bytes]
    postgres_bin: Path
    postgres_data: Path
    cleanup: CleanupEvidence


@dataclass
class BrowserRuntime:
    page: Page
    state: M4BrowserState
    browser: Browser


@dataclass(frozen=True)
class SeededGovernanceContext:
    task: Task
    step: Step
    contract_id: str


def _normalize_declared_naive_timestamps(
    _connection: Any,
    _cursor: Any,
    statement: str,
    parameters: Any,
    _context: Any,
    _executemany: bool,
) -> tuple[str, Any]:
    """Adapt UTC-aware service values to the existing timezone-naive columns."""

    def normalize(value: Any) -> Any:
        if isinstance(value, datetime) and value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        if isinstance(value, tuple):
            return tuple(normalize(item) for item in value)
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, dict):
            return {key: normalize(item) for key, item in value.items()}
        return value

    return statement, normalize(parameters)


class M4Database:
    """Real-PostgreSQL adapter for the exact ActionHandler surface used by M4."""

    def __init__(self, database_url: str) -> None:
        self.engine = create_async_engine(database_url, poolclass=pool.NullPool)
        event.listen(
            self.engine.sync_engine,
            "before_cursor_execute",
            _normalize_declared_naive_timestamps,
            retval=True,
        )
        self.Session = async_sessionmaker(bind=self.engine, expire_on_commit=False)

    async def create_action(self, action: ClickAction | SelectOptionAction) -> SimpleNamespace:
        async with self.Session() as session:
            model = ActionModel(
                action_type=action.action_type.value,
                source_action_id=action.source_action_id,
                organization_id=action.organization_id,
                workflow_run_id=action.workflow_run_id,
                task_id=action.task_id,
                step_id=action.step_id,
                step_order=action.step_order,
                action_order=action.action_order,
                status=action.status.value,
                reasoning=action.reasoning,
                intention=action.intention,
                response=action.response,
                element_id=action.element_id,
                skyvern_element_hash=action.skyvern_element_hash,
                skyvern_element_data=action.skyvern_element_data,
                screenshot_artifact_id=action.screenshot_artifact_id,
                action_json=action.model_dump(mode="json"),
                confidence_float=action.confidence_float,
                created_by=action.created_by,
            )
            session.add(model)
            await session.commit()
            await session.refresh(model)
            return SimpleNamespace(action_id=model.action_id)


class M4BrowserState:
    """Playwright-backed BrowserState limited to one already-open localhost page."""

    def __init__(
        self,
        playwright: Playwright,
        browser: Browser,
        browser_context: BrowserContext,
        page: Page,
    ) -> None:
        self.pw = playwright
        self.browser = browser
        self.browser_context = browser_context
        self._page = page
        self.browser_artifacts = SimpleNamespace()
        self.browser_cleanup = None

    async def get_working_page(self) -> Page:
        return self._page

    async def must_get_working_page(self) -> Page:
        return self._page

    async def get_or_create_page(self, **_kwargs: Any) -> Page:
        return self._page

    async def list_valid_pages(self, **_kwargs: Any) -> list[Page]:
        return [page for page in self.browser_context.pages if not page.is_closed()]

    async def stop_page_loading(self) -> None:
        await self._page.evaluate("window.stop()")

    async def take_fullpage_screenshot(self, file_path: str | None = None) -> bytes:
        return await self._page.screenshot(path=file_path, full_page=True)

    async def take_post_action_screenshot(self, scrolling_number: int, file_path: str | None = None) -> bytes:
        del scrolling_number
        return await self._page.screenshot(path=file_path)

    async def scrape_website(self, **kwargs: Any) -> ScrapedPage:
        return await skyvern_scraper.scrape_website(browser_state=self, **kwargs)

    async def close(self) -> None:
        await self.browser_context.close()
        await self.browser.close()
        await self.pw.stop()


def repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def postgres_executable(name: str) -> str:
    return f"{name}.exe" if os.name == "nt" else name


def find_installed_chromium() -> Path:
    override = os.environ.get("FINRPA_CHROMIUM_EXECUTABLE")
    candidates: list[Path] = []
    if override:
        candidates.append(Path(override))
    browser_root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if browser_root and browser_root != "0":
        playwright_roots = [Path(browser_root)]
    elif os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        playwright_roots = [Path(local_app_data) / "ms-playwright"] if local_app_data else []
    else:
        playwright_roots = [Path.home() / ".cache" / "ms-playwright"]
    patterns = (
        "chromium-*/chrome-win/chrome.exe",
        "chromium-*/chrome-linux/chrome",
        "chromium_headless_shell-*/chrome-linux/headless_shell",
        "chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium",
    )
    for root in playwright_roots:
        for pattern in patterns:
            candidates.extend(sorted(root.glob(pattern), reverse=True))
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        if executable := shutil.which(name):
            candidates.append(Path(executable))
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise RuntimeError(
        "M4 requires a real Chromium binary; run `python -m playwright install chromium` "
        "or set FINRPA_CHROMIUM_EXECUTABLE"
    )


def find_postgres_bin() -> Path:
    override = os.environ.get("FINRPA_POSTGRES_BIN")
    path_initdb = shutil.which(postgres_executable("initdb"))
    candidates: list[Path | None] = [Path(override) if override else None]
    if path_initdb:
        candidates.append(Path(path_initdb).resolve().parent)
    if os.name == "nt":
        candidates.append(Path("E:/tmp/postgresql-14.23-portable/pgsql/bin"))
    required = tuple(postgres_executable(name) for name in ("initdb", "pg_ctl", "createdb", "pg_isready"))
    for candidate in candidates:
        if candidate and all((candidate / executable).is_file() for executable in required):
            return candidate.resolve()
    raise RuntimeError(
        "M4 requires PostgreSQL client/server binaries on PATH or FINRPA_POSTGRES_BIN "
        f"containing {', '.join(required)}"
    )


def assert_loopback_url(url: str) -> str:
    parsed = urlparse(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("M4 accepts only an unauthenticated 127.0.0.1 HTTP target")
    return url


def http_json(url: str, *, payload: dict[str, Any] | None = None, timeout: float = 10.0) -> Any:
    assert_loopback_url(url)
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(
        url,
        data=body,
        method="POST" if payload is not None else "GET",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            final_url = response.geturl()
            assert_loopback_url(final_url)
            if _origin(final_url) != _origin(url):
                raise ValueError("M4 rejected a redirected localhost target")
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Synthetic console request failed: {exc.code} {detail}") from exc


def is_loopback_port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex(("127.0.0.1", port)) == 0


@contextmanager
def isolated_m4_environment() -> Iterator[IsolatedM4Environment]:
    repository = repository_root()
    root = Path(tempfile.mkdtemp(prefix="finrpa-m4-"))
    cleanup = CleanupEvidence(temp_root=root)
    postgres_bin = find_postgres_bin()
    postgres_data = root / "postgres-data"
    postgres_log = root / "postgres.log"
    postgres_socket = root / "postgres-socket"
    console_log_path = root / "console.log"
    postgres_port = _reserve_loopback_port()
    console_port = _reserve_loopback_port()
    cleanup.postgres_port = postgres_port
    cleanup.console_port = console_port
    console_process: subprocess.Popen[bytes] | None = None
    console_log = None

    try:
        _run(
            [
                postgres_bin / postgres_executable("initdb"),
                "-D",
                postgres_data,
                "--username=postgres",
                "--auth=trust",
                "--encoding=UTF8",
                "--no-locale",
            ],
            cwd=repository,
        )
        postgres_options = f"-p {postgres_port} -h 127.0.0.1"
        if os.name != "nt":
            # Debian/Ubuntu PostgreSQL defaults to /var/run/postgresql, which
            # is not writable by an unprivileged GitHub Actions runner.
            postgres_socket.mkdir()
            postgres_options += f" -k {postgres_socket}"
        try:
            _run(
                [
                    postgres_bin / postgres_executable("pg_ctl"),
                    "-D",
                    postgres_data,
                    "-l",
                    postgres_log,
                    "-o",
                    postgres_options,
                    "-w",
                    "-t",
                    "30",
                    "start",
                ],
                cwd=repository,
                capture_output=False,
            )
        except RuntimeError as exc:
            server_log = postgres_log.read_text(encoding="utf-8", errors="replace") if postgres_log.is_file() else ""
            raise RuntimeError(f"{exc}\npostgres log:\n{server_log}") from exc
        cleanup.postgres_pid = _postgres_pid(postgres_data)
        _wait_for_port(postgres_port, expected_open=True)
        _run(
            [
                postgres_bin / postgres_executable("createdb"),
                "--host=127.0.0.1",
                f"--port={postgres_port}",
                "--username=postgres",
                "skyvern",
            ],
            cwd=repository,
        )
        database_url = f"postgresql+asyncpg://postgres@127.0.0.1:{postgres_port}/skyvern"
        migration_env = os.environ.copy()
        migration_env.update(
            {
                "DATABASE_STRING": database_url,
                "DATABASE_REPLICA_STRING": database_url,
                "GOVERNANCE_MODE": "off",
            }
        )
        _run([Path(sys.executable), "-m", "alembic", "upgrade", "heads"], cwd=repository, env=migration_env, timeout=300)

        console_log = console_log_path.open("wb")
        console_env = os.environ.copy()
        console_env["GOVERNANCE_MODE"] = "off"
        console_process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "enterprise.domains.synthetic_payment.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(console_port),
            ],
            cwd=repository,
            env=console_env,
            stdout=console_log,
            stderr=subprocess.STDOUT,
        )
        cleanup.console_pid = console_process.pid
        console_url = assert_loopback_url(f"http://127.0.0.1:{console_port}/")
        _wait_for_health(console_url, console_process)

        yield IsolatedM4Environment(
            repository=repository,
            root=root,
            database_url=database_url,
            console_url=console_url,
            console_process=console_process,
            postgres_bin=postgres_bin,
            postgres_data=postgres_data,
            cleanup=cleanup,
        )
    finally:
        if console_process is not None:
            try:
                if console_process.poll() is None:
                    console_process.terminate()
                    console_process.wait(timeout=10)
                cleanup.console_stopped = console_process.poll() is not None
            except Exception as exc:  # pragma: no cover - retained for forensic failure output
                cleanup.retained_errors.append(f"console cleanup failed: {exc}")
                try:
                    console_process.kill()
                    console_process.wait(timeout=5)
                    cleanup.console_stopped = True
                except Exception as kill_exc:
                    cleanup.retained_errors.append(f"console forced cleanup failed: {kill_exc}")
        if console_log is not None:
            console_log.close()
        try:
            if postgres_data.exists():
                _run(
                    [
                        postgres_bin / postgres_executable("pg_ctl"),
                        "-D",
                        postgres_data,
                        "-w",
                        "-t",
                        "30",
                        "stop",
                        "-m",
                        "fast",
                    ],
                    cwd=repository,
                    timeout=40,
                    check=False,
                )
            cleanup.postgres_stopped = not is_loopback_port_open(postgres_port)
        except Exception as exc:  # pragma: no cover - retained for forensic failure output
            cleanup.retained_errors.append(f"postgres cleanup failed: {exc}")
        cleanup.console_stopped = cleanup.console_stopped and not is_loopback_port_open(console_port)
        try:
            _safe_remove_temp_root(root)
            cleanup.temp_root_removed = not root.exists()
        except Exception as exc:  # pragma: no cover - retained for forensic failure output
            cleanup.retained_errors.append(f"temporary state retained at {root}: {exc}")
        if cleanup.retained_errors:
            raise AssertionError("; ".join(cleanup.retained_errors))


@asynccontextmanager
async def real_chromium(console_url: str, cleanup: CleanupEvidence) -> AsyncIterator[BrowserRuntime]:
    assert_loopback_url(console_url)
    executable = find_installed_chromium()
    playwright = await async_playwright().start()
    browser: Browser | None = None
    state: M4BrowserState | None = None
    try:
        browser = await playwright.chromium.launch(headless=True, executable_path=str(executable))
        context = await browser.new_context()
        page = await context.new_page()
        response = await page.goto(console_url, wait_until="domcontentloaded", timeout=15_000)
        if response is None or not response.ok:
            raise RuntimeError("Synthetic console navigation failed")
        await page.wait_for_load_state("networkidle", timeout=15_000)
        assert_loopback_url(page.url)
        if _canonical_page_url(page.url) != _canonical_page_url(console_url):
            raise ValueError("M4 rejected a redirected browser target")
        state = M4BrowserState(playwright, browser, context, page)
        yield BrowserRuntime(page=page, state=state, browser=browser)
    finally:
        if state is not None:
            await state.close()
        else:
            if browser is not None:
                await browser.close()
            await playwright.stop()
        cleanup.browser_closed = browser is None or not browser.is_connected()


@contextmanager
def configured_forge_boundary(database: M4Database, browser_state: M4BrowserState) -> Iterator[None]:
    previous = object.__getattribute__(forge_app, "_inst")
    previous_secret = settings.GOVERNANCE_AUDIT_HMAC_SECRET
    previous_context = skyvern_context.current()
    set_force_app_instance(
        SimpleNamespace(
            DATABASE=database,
            REPLICA_DATABASE=database,
            BROWSER_MANAGER=_SingleBrowserManager(browser_state),
            NORMAL_SELECT_AGENT_LLM_API_HANDLER=_deterministic_normal_select,
        )
    )
    settings.GOVERNANCE_AUDIT_HMAC_SECRET = HMAC_SECRET
    skyvern_context.set(
        skyvern_context.SkyvernContext(
            organization_id=ORGANIZATION_ID,
            task_id=TASK_ID,
            step_id=STEP_ID,
            run_id=RUN_ID,
        )
    )
    try:
        yield
    finally:
        settings.GOVERNANCE_AUDIT_HMAC_SECRET = previous_secret
        object.__setattr__(forge_app, "_inst", previous)
        if previous_context is None:
            skyvern_context.reset()
        else:
            skyvern_context.set(previous_context)


async def seed_governance_context(database: M4Database, console_url: str) -> SeededGovernanceContext:
    now = datetime.now(timezone.utc)
    async with database.Session() as session:
        session.add(OrganizationModel(organization_id=ORGANIZATION_ID, organization_name="FinRPA M4 Synthetic"))
        await session.flush()
        session.add(
            TaskModel(
                task_id=TASK_ID,
                organization_id=ORGANIZATION_ID,
                status=TaskStatus.running.value,
                title="M4 synthetic governed browser proof",
                url=console_url,
                navigation_goal="Submit exactly one approved synthetic payment",
                errors=[],
            )
        )
        await session.flush()
        session.add(
            StepModel(
                step_id=STEP_ID,
                organization_id=ORGANIZATION_ID,
                task_id=TASK_ID,
                status=StepStatus.running.value,
                order=0,
                is_last=True,
            )
        )
        await session.flush()
        session.add(
            TaskContractModel(
                contract_id=CONTRACT_ID,
                task_id=TASK_ID,
                organization_id=ORGANIZATION_ID,
                goal="Submit one approved synthetic payment through the governed Skyvern boundary",
                allowed_operations=["synthetic.payment.submit"],
                data_scope={"hosts": ["127.0.0.1"], "domain_pack": "synthetic.payment"},
                authorization_snapshot={"run_id": RUN_ID, "synthetic_only": True},
                policy_profile="finrpa-m4-synthetic",
                policy_version="phase2-m4-v2",
                success_criteria=["independent result probe confirms one submission"],
                mode="audit",
                expires_at=now + timedelta(minutes=10),
            )
        )
        await session.commit()

    task = Task(
        task_id=TASK_ID,
        organization_id=ORGANIZATION_ID,
        status=TaskStatus.running,
        url=console_url,
        title="M4 synthetic governed browser proof",
        navigation_goal="Submit exactly one approved synthetic payment",
        created_at=now,
        modified_at=now,
    )
    step = Step(
        task_id=TASK_ID,
        step_id=STEP_ID,
        status=StepStatus.running,
        order=0,
        is_last=True,
        organization_id=ORGANIZATION_ID,
        created_at=now,
        modified_at=now,
    )
    return SeededGovernanceContext(task=task, step=step, contract_id=CONTRACT_ID)


async def scrape_current_page(browser: BrowserRuntime) -> ScrapedPage:
    async def identity_cleanup(_page: Any, _url: str, tree: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return tree

    return await browser.state.scrape_website(
        url=browser.page.url,
        cleanup_element_tree=identity_cleanup,
        take_screenshots=False,
        draw_boxes=False,
        scroll=False,
        max_screenshot_number=1,
        must_included_tags=["button", "select"],
    )


def element_id_by_aria_label(scraped_page: ScrapedPage, aria_label: str) -> str:
    for element_id, element in scraped_page.id_to_element_dict.items():
        if (element.get("attributes") or {}).get("aria-label") == aria_label:
            return element_id
    observed = sorted(
        str((element.get("attributes") or {}).get("aria-label"))
        for element in scraped_page.elements
        if (element.get("attributes") or {}).get("aria-label")
    )
    raise AssertionError(f"Skyvern observation did not contain {aria_label!r}; observed={observed}")


def click_action(*, element_id: str, order: int, description: str) -> ClickAction:
    return ClickAction(
        element_id=element_id,
        organization_id=ORGANIZATION_ID,
        task_id=TASK_ID,
        step_id=STEP_ID,
        step_order=0,
        action_order=order,
        description=description,
        reasoning="M4 deterministic synthetic browser proof",
        intention=description,
    )


def select_action(*, element_id: str, order: int, value: str) -> SelectOptionAction:
    return SelectOptionAction(
        element_id=element_id,
        option=SelectOption(value=value),
        input_or_select_context=InputOrSelectContext(
            intention="Select the approved synthetic ambiguity fault",
            field="Synthetic execution fault mode",
            is_required=True,
        ),
        organization_id=ORGANIZATION_ID,
        task_id=TASK_ID,
        step_id=STEP_ID,
        step_order=0,
        action_order=order,
        description=f"Select synthetic fault mode {value}",
        reasoning="M4 deterministic fault injection through Skyvern",
        intention="Select the approved synthetic ambiguity fault",
    )


async def run_handler_action(
    *,
    browser: BrowserRuntime,
    governance: SeededGovernanceContext,
    scraped_page: ScrapedPage,
    action: ClickAction | SelectOptionAction,
    authorization: ExecutionAuthorization | None = None,
    profile: ExecutionProfile | None = None,
) -> list[Any]:
    results = await ActionHandler.handle_action(
        scraped_page=scraped_page,
        task=governance.task,
        step=governance.step,
        page=browser.page,
        action=action,
        execution_authorization=authorization,
        execution_profile=profile,
    )
    if not results or not isinstance(results[-1], ActionSuccess):
        raise AssertionError(f"Skyvern ActionHandler did not complete the action: {results!r}")
    return results


async def issue_exact_permit(
    *,
    database: M4Database,
    action: ClickAction,
    scraped_page: ScrapedPage,
    idempotency_key: str,
) -> tuple[ExecutionAuthorization, ExecutionProfile]:
    from enterprise.governance.audit import observation_hash
    from enterprise.governance.classification import action_fingerprint

    observed_hash = observation_hash(url=scraped_page.url, html=scraped_page.html, secret=HMAC_SECRET)
    fingerprint = action_fingerprint(
        task_id=TASK_ID,
        step_id=STEP_ID,
        action_payload=action.model_dump(mode="json", exclude_none=True),
        observation_hash=observed_hash,
        secret=HMAC_SECRET,
    )
    profile = ExecutionProfile(
        mechanism=ExecutionMechanism.LOCATOR,
        fallback_rank=0,
        evidence_refs=[f"skyvern://scrape/{RUN_ID}/{action.element_id}"],
    )
    decision = PolicyDecision(
        decision_id="decision-m4-synthetic-governed-browser",
        intent_id="intent-m4-synthetic-governed-browser",
        outcome=DecisionOutcome.ALLOW,
        risk_level="critical",
        reasons=["Owner-approved localhost-only synthetic M4 proof"],
        matched_rules=["finrpa-m4-v2"],
        policy_version="phase2-m4-v2",
    )
    async with database.Session() as session:
        session.add(
            PendingActionModel(
                pending_action_id="pending-m4-synthetic-governed-browser",
                task_id=TASK_ID,
                step_id=STEP_ID,
                contract_id=CONTRACT_ID,
                organization_id=ORGANIZATION_ID,
                action_fingerprint=fingerprint,
                observation_hash=observed_hash,
                action_payload=action.model_dump(mode="json", exclude_none=True),
                intent_payload={
                    "intent_id": decision.intent_id,
                    "operation": "synthetic.payment.submit",
                    "effect": ExecutionEffect.EXTERNAL_WRITE.value,
                },
                decision_payload=decision.model_dump(mode="json"),
                approval_id="approval-m4-synthetic-governed-browser",
                status="approved",
                row_version=1,
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
        )
        permit = await issue_permit(
            db_session=session,
            task_id=TASK_ID,
            step_id=STEP_ID,
            contract_id=CONTRACT_ID,
            action_fingerprint=fingerprint,
            observation_hash=observed_hash,
            decision=decision,
            effect=ExecutionEffect.EXTERNAL_WRITE,
            execution_profile=profile,
            ttl_seconds=300,
        )
        await session.commit()
    return (
        ExecutionAuthorization(
            permit_id=permit.permit_id,
            action_fingerprint=fingerprint,
            observation_hash=observed_hash,
            idempotency_key=idempotency_key,
            effect=ExecutionEffect.EXTERNAL_WRITE,
        ),
        profile,
    )


async def install_execute_order_probe(
    *,
    page: Page,
    database: M4Database,
    idempotency_key: str,
    observed_statuses: list[str | None],
    observed_urls: list[str],
) -> None:
    async def capture(route: Route) -> None:
        request_url = route.request.url
        assert_loopback_url(request_url)
        observed_urls.append(request_url)
        async with database.Session() as session:
            attempt = (
                await session.scalars(
                    select(ExecutionAttemptModel).where(
                        ExecutionAttemptModel.task_id == TASK_ID,
                        ExecutionAttemptModel.idempotency_key == idempotency_key,
                    )
                )
            ).first()
            observed_statuses.append(attempt.status if attempt is not None else None)
        await route.continue_()

    await page.route("**/api/challenges/*/execute", capture)


async def execution_attempt(database: M4Database, idempotency_key: str) -> ExecutionAttemptModel:
    async with database.Session() as session:
        attempt = (
            await session.scalars(
                select(ExecutionAttemptModel).where(
                    ExecutionAttemptModel.task_id == TASK_ID,
                    ExecutionAttemptModel.idempotency_key == idempotency_key,
                )
            )
        ).first()
        if attempt is None:
            raise AssertionError("Expected durable M4 execution attempt was not found")
        session.expunge(attempt)
        return attempt


async def resolve_attempt_from_probe(
    *, database: M4Database, attempt_id: str, result_probe: dict[str, Any]
) -> ExecutionAttemptStatus:
    if result_probe.get("status") != "confirmed":
        raise ValueError("Only independently confirmed synthetic probe evidence may resolve the attempt")
    async with database.Session() as session:
        resolved = await resolve_unknown_execution_attempt(
            db_session=session,
            attempt_id=attempt_id,
            confirmed=True,
            result_probe=result_probe,
        )
        await session.commit()
    return resolved.status


class _SingleBrowserManager:
    def __init__(self, browser_state: M4BrowserState) -> None:
        self._browser_state = browser_state

    def get_for_task(self, _task_id: str, workflow_run_id: str | None = None) -> M4BrowserState:
        del workflow_run_id
        return self._browser_state


async def _deterministic_normal_select(**_kwargs: Any) -> dict[str, Any]:
    return {"index": None, "value": "commit_then_inconclusive"}


def _reserve_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _run(
    command: list[Path | str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: int = 60,
    check: bool = True,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [str(item) for item in command],
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE if capture_output else subprocess.DEVNULL,
        stderr=subprocess.PIPE if capture_output else subprocess.DEVNULL,
        timeout=timeout,
        check=False,
    )
    if check and completed.returncode != 0:
        raise RuntimeError(
            f"Command failed ({completed.returncode}): {' '.join(str(item) for item in command)}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed


def _wait_for_port(port: int, *, expected_open: bool, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_loopback_port_open(port) is expected_open:
            return
        time.sleep(0.1)
    raise RuntimeError(f"Timed out waiting for 127.0.0.1:{port} open={expected_open}")


def _wait_for_health(console_url: str, process: subprocess.Popen[bytes]) -> None:
    health_url = console_url.rstrip("/") + "/health"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Synthetic console exited early with {process.returncode}")
        try:
            health = http_json(health_url, timeout=1.0)
            if health == {"status": "ready", "domain_pack": "synthetic.payment", "production_eligible": False}:
                return
        except (OSError, RuntimeError, ValueError):
            pass
        time.sleep(0.2)
    raise RuntimeError("Synthetic console did not become ready on 127.0.0.1")


def _postgres_pid(data_dir: Path) -> int | None:
    pid_file = data_dir / "postmaster.pid"
    if not pid_file.is_file():
        return None
    return int(pid_file.read_text(encoding="utf-8").splitlines()[0])


def _safe_remove_temp_root(root: Path) -> None:
    resolved = root.resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    if resolved.parent != temp_root or not resolved.name.startswith("finrpa-m4-"):
        raise ValueError(f"Refusing to remove unexpected M4 temporary root: {resolved}")
    last_error: OSError | None = None
    for _ in range(20):
        try:
            shutil.rmtree(resolved)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            last_error = exc
            time.sleep(0.1)
    if last_error is not None:
        raise last_error


def _origin(url: str) -> tuple[str, str | None, int | None]:
    parsed = urlparse(url)
    return parsed.scheme, parsed.hostname, parsed.port


def _canonical_page_url(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.hostname}:{parsed.port or 80}{parsed.path or '/'}"
