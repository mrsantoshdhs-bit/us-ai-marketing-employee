"""Application context shared by agents, jobs, CLI, and the web app."""

from __future__ import annotations

import ipaddress
import logging
import socket
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpcore
import httpx

from aime import __version__
from aime.config import Settings
from aime.db import Database
from aime.logging_setup import setup_logging

if TYPE_CHECKING:
    from aime.llm.base import LLMProvider

USER_AGENT = f"aime/{__version__} (local marketing assistant)"

log = logging.getLogger("aime.context")


class NotPublicAddress(httpx.TransportError):
    """A request to a private, local or otherwise inside address, refused before anything was sent."""


def is_public_ip(address: str) -> bool:
    """A routable internet address. IPv4 mapped into IPv6 ("::ffff:127.0.0.1") counts as the IPv4 address it holds."""
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    ip = getattr(ip, "ipv4_mapped", None) or ip
    return ip.is_global and not ip.is_multicast


def inside_host(host: str) -> bool:
    """True for a host known not to be on the internet without a lookup: a local name, or an address in any spelling
    ("127.0.0.1", "::1", "2130706433", "0x7f.1"). Other names are checked where they resolve."""
    name = host.strip("[]").rstrip(".").lower()
    if name == "localhost" or name.endswith((".localhost", ".local", ".internal", ".intranet", ".lan")):
        return True
    try:
        ipaddress.ip_address(name.split("%")[0])
    except ValueError:
        if not name.split(".")[-1].startswith(("0x", *"0123456789")):
            return False  # a name: its last label is never a number
        try:
            name = socket.inet_ntoa(socket.inet_aton(name))  # the short and numeric forms a resolver also accepts
        except OSError:
            return False
    return not is_public_ip(name)


def _refuse_inside_request(request: httpx.Request) -> None:
    """Runs before every request, so also before every redirect hop."""
    if request.url.scheme not in ("http", "https"):
        raise NotPublicAddress(f"refused a {request.url.scheme} address", request=request)
    if inside_host(request.url.host):
        raise NotPublicAddress("refused a private or local address", request=request)


class _PublicOnlyBackend(httpcore.SyncBackend):
    """Connects only to public addresses, checked where the name is resolved. It connects to the address it checked, so a
    name that resolves differently a moment later (DNS rebinding) cannot reach inside. TLS still verifies the host name."""

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        try:
            found = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise httpcore.ConnectError(f"name not resolved: {exc}") from exc
        addresses = list(dict.fromkeys(item[4][0] for item in found))
        if not addresses or not all(is_public_ip(a) for a in addresses):
            raise httpcore.ConnectError("refused: the name resolves to a private or local address")
        error: Exception | None = None
        for address in addresses:
            try:
                return super().connect_tcp(address, port, timeout, local_address, socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                error = exc
        raise error


def _public_only_transport() -> httpx.HTTPTransport:
    transport = httpx.HTTPTransport()
    transport._pool = httpcore.ConnectionPool(ssl_context=httpx.create_ssl_context(), network_backend=_PublicOnlyBackend())
    return transport


@dataclass
class AppContext:
    settings: Settings
    db: Database
    http_transport: httpx.BaseTransport | None = None
    _llm: "LLMProvider | None" = field(default=None, repr=False)
    _llm_checked: bool = field(default=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def http(self, public_only: bool = False, **kwargs) -> httpx.Client:
        """`public_only`: for addresses a stranger's page can steer, such as a company website and where it redirects.
        Every hop to a private or local address is refused, whether it is written as an address or a name resolves to one."""
        headers = {"User-Agent": USER_AGENT}
        headers.update(kwargs.pop("headers", {}) or {})
        transport = self.http_transport
        if public_only:
            kwargs["event_hooks"] = {"request": [_refuse_inside_request]}
            transport = transport or _public_only_transport()
        return httpx.Client(
            timeout=kwargs.pop("timeout", self.settings.http_timeout),
            headers=headers,
            transport=transport,
            follow_redirects=kwargs.pop("follow_redirects", True),
            **kwargs,
        )

    @property
    def llm(self) -> "LLMProvider | None":
        with self._lock:
            if not self._llm_checked:
                from aime.llm.factory import build_llm

                self._llm = build_llm(self)
                self._llm_checked = True
            return self._llm

    def reset_llm(self) -> None:
        with self._lock:
            self._llm = None
            self._llm_checked = False


class DataRoleMismatch(RuntimeError):
    pass


def _has_data(db: Database) -> bool:
    """Any record in any table: posts, tokens or history are worth a backup even with no company on file."""
    from sqlalchemy import inspect, text

    tables = inspect(db.engine).get_table_names()
    with db.engine.connect() as conn:
        return any(conn.execute(text(f'SELECT 1 FROM "{t}" LIMIT 1')).first() for t in tables)  # noqa: S608


def create_context(settings: Settings | None = None, http_transport: httpx.BaseTransport | None = None) -> AppContext:
    from aime.db import kv_get, kv_set
    from aime.migrations import migrate

    settings = settings or Settings.load()
    settings.ensure_dirs()
    setup_logging(settings.logs_dir)
    db = Database(settings.database_url)
    migrate(db.engine, settings.backups_dir, has_data=_has_data(db))
    with db.session() as s:
        role = kv_get(s, "db_role")
        if role is None:
            kv_set(s, "db_role", settings.data_role)
        elif role != settings.data_role:
            db.dispose()
            raise DataRoleMismatch(
                f"Database at {settings.database_url} is a '{role}' database but settings say "
                f"'{settings.data_role}'. Refusing to mix real and sample data.")
    with db.session() as s:  # the Super Admin's sales settings, laid over config/business.toml
        from aime import admin_settings
        admin_settings.load(s, settings)
    return AppContext(settings=settings, db=db, http_transport=http_transport)


_CONTEXT: AppContext | None = None
_CONTEXT_LOCK = threading.Lock()


def get_context() -> AppContext:
    global _CONTEXT
    with _CONTEXT_LOCK:
        if _CONTEXT is None:
            _CONTEXT = create_context()
        return _CONTEXT


def set_context(ctx: AppContext | None) -> None:
    global _CONTEXT
    with _CONTEXT_LOCK:
        _CONTEXT = ctx
