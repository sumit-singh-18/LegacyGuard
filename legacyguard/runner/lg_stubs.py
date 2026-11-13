"""Stand-ins for databases and HTTP clients during the behavioral diff (roadmap C1, "stub and compare").

Runs in the target interpreter. Standard library only. Old and new code talk to the same fakes, which never leave
the process and always give the same answers (no rows, HTTP 200 with an empty JSON object). What is compared is the
calls each version makes: the SQL and its parameters, the HTTP method, URL and body, commits. A changed WHERE clause,
a dropped UPDATE or a changed request then shows as a difference, and functions that take a connection or cursor
can be checked at all.

Real recorded answers (record against the test database, replay to both versions) are a later step: recording
against a developer's real database or third-party APIs could write data or spend money, so it is not done here.
"""
from __future__ import annotations

import json
import re
import sys

MAX_EVENTS = 50
EVENTS: list = []          # the I/O calls of the current input, in order
_WS = re.compile(r"\s+")


def reset() -> None:
    EVENTS.clear()


def _record(event: dict) -> None:
    if len(EVENTS) < MAX_EVENTS:
        EVENTS.append(event)


def _sql(text) -> str:
    return _WS.sub(" ", str(text)).strip().rstrip(";")


def _plain(value):
    """Parameters as JSON-friendly values, so the comparison and the message stay readable."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    return repr(value)


class FakeCursor:
    """A DB-API cursor that records statements and returns no rows."""

    rowcount = 0
    lastrowid = 1
    description = None
    arraysize = 1

    def __init__(self, conn=None):
        self.connection = conn

    def execute(self, sql, params=None):
        _record({"io": "sql", "sql": _sql(sql), "params": _plain(params)})
        return self

    def executemany(self, sql, seq):
        _record({"io": "sql", "sql": _sql(sql), "params": _plain(list(seq)), "many": True})
        return self

    def executescript(self, script):
        _record({"io": "sql", "sql": _sql(script)})
        return self

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def fetchmany(self, size=None):
        return []

    def __iter__(self):
        return iter(())

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConnection:
    """A DB-API connection (sqlite3, psycopg, pymysql shapes) that records statements and transaction calls."""

    def __init__(self, *args, **kwargs):
        self.row_factory = None
        self.isolation_level = None
        self.autocommit = False

    def cursor(self, *args, **kwargs):
        return FakeCursor(self)

    def execute(self, sql, params=None):
        return FakeCursor(self).execute(sql, params)

    def executemany(self, sql, seq):
        return FakeCursor(self).executemany(sql, seq)

    def executescript(self, script):
        return FakeCursor(self).executescript(script)

    def commit(self):
        _record({"io": "sql", "sql": "COMMIT"})

    def rollback(self):
        _record({"io": "sql", "sql": "ROLLBACK"})

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *rest):
        _record({"io": "sql", "sql": "ROLLBACK" if exc_type else "COMMIT"})
        return False


def fake_connect(*args, **kwargs):
    return FakeConnection()


def _body(kwargs) -> object:
    if kwargs.get("json") is not None:
        return _plain(kwargs["json"])
    data = kwargs.get("data") or kwargs.get("content")
    if isinstance(data, bytes):
        data = data.decode("utf-8", "replace")
    return _plain(data)


def _http_event(method, url, kwargs) -> None:
    _record({"io": "http", "method": str(method).upper(), "url": str(url), "params": _plain(kwargs.get("params")), "body": _body(kwargs)})


def install(stub_http: bool = True) -> list[str]:
    """Replace database connects and HTTP clients with the fakes. Returns what was replaced, for the report.
    Drivers not yet imported are patched by patch_loaded() once the project has imported them."""
    import sqlite3
    sqlite3.connect = fake_connect
    sqlite3.dbapi2.connect = fake_connect
    done = ["sqlite3"]
    if stub_http:
        import urllib.request

        def urlopen(url, data=None, *args, **kwargs):
            target = getattr(url, "full_url", url)
            method = getattr(url, "get_method", lambda: "POST" if data else "GET")()
            _record({"io": "http", "method": method, "url": str(target), "params": None,
                     "body": _plain(data.decode("utf-8", "replace") if isinstance(data, bytes) else data)})
            import io as _io

            class _Resp(_io.BytesIO):
                status = 200
                headers = {}

                def getcode(self):
                    return 200
            return _Resp(b"{}")
        urllib.request.urlopen = urlopen
        done.append("urllib")
    return done


_PATCHED: set = set()


def patch_loaded(stub_http: bool = True) -> list[str]:
    """Patch database drivers and HTTP clients the project has imported by now."""
    done = []
    for name in ("psycopg2", "psycopg", "pymysql", "MySQLdb", "mysql.connector"):
        mod = sys.modules.get(name)
        if mod is not None and name not in _PATCHED and hasattr(mod, "connect"):
            mod.connect = fake_connect
            _PATCHED.add(name)
            done.append(name)
    if not stub_http:
        return done
    requests = sys.modules.get("requests")
    if requests is not None and "requests" not in _PATCHED:
        def request(self, method, url, **kwargs):
            _http_event(method, url, kwargs)
            resp = requests.models.Response()
            resp.status_code, resp._content, resp.url = 200, b"{}", str(url)
            resp.headers["Content-Type"] = "application/json"
            return resp
        requests.sessions.Session.request = request
        _PATCHED.add("requests")
        done.append("requests")
    httpx = sys.modules.get("httpx")
    if httpx is not None and "httpx" not in _PATCHED:
        real_send, real_asend = httpx.Client.send, httpx.AsyncClient.send

        def send(self, request, **kwargs):
            # Clients with their own transport (Starlette's TestClient, httpx.MockTransport) stay in process anyway.
            if not isinstance(getattr(self, "_transport", None), (httpx.HTTPTransport, type(None))):
                return real_send(self, request, **kwargs)
            return _fake(request)

        async def asend(self, request, **kwargs):
            if not isinstance(getattr(self, "_transport", None), (httpx.AsyncHTTPTransport, type(None))):
                return await real_asend(self, request, **kwargs)
            return _fake(request)

        def _fake(request):
            body = request.content.decode("utf-8", "replace") if request.content else None
            try:
                body = json.loads(body) if body else body
            except ValueError:
                pass
            _record({"io": "http", "method": request.method, "url": str(request.url), "params": None, "body": _plain(body)})
            return httpx.Response(200, json={}, request=request)
        httpx.Client.send = send
        httpx.AsyncClient.send = asend
        _PATCHED.add("httpx")
        done.append("httpx")
    return done
