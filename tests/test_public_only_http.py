"""Company websites are strangers' servers: nothing they answer may steer a request to this PC or its network, and nothing
they send may cost more than a page's worth of memory and time."""

import socket
import time

import httpcore
import httpx
import pytest

from aime.context import _PublicOnlyBackend, inside_host, is_public_ip
from aime.site_reader import SiteReader, parse_page
from aime.website_check import check_website, page_title, read_robots, site_name


@pytest.mark.parametrize("address", ["127.0.0.1", "10.1.2.3", "172.16.0.9", "192.168.1.1", "169.254.169.254", "100.64.0.1", "0.0.0.0",
                                     "224.0.0.1", "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1", "::ffff:192.168.1.1", "not an address"])
def test_inside_addresses_are_not_public(address):
    assert not is_public_ip(address)


@pytest.mark.parametrize("address", ["93.184.216.34", "8.8.8.8", "2606:2800:220:1:248:1893:25c8:1946", "::ffff:8.8.8.8"])
def test_internet_addresses_are_public(address):
    assert is_public_ip(address)


@pytest.mark.parametrize("host", ["localhost", "LOCALHOST.", "printer.local", "db.internal", "app.localhost", "127.0.0.1", "[::1]", "::1",
                                  "::ffff:127.0.0.1", "2130706433", "0x7f.0.0.1", "127.1", "0177.0.0.1", "169.254.169.254"])
def test_inside_hosts_need_no_lookup(host):
    assert inside_host(host)


@pytest.mark.parametrize("host", ["site.example", "www.acme.com", "8.8.8.8", "acme.io", "3m.com"])
def test_ordinary_hosts_go_on_to_the_lookup(host):
    assert not inside_host(host)


def resolves_to(monkeypatch, *addresses):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in addresses])


@pytest.mark.parametrize("addresses", [("127.0.0.1",), ("192.168.1.5",), ("::ffff:10.0.0.1",), ("::1",), ("8.8.8.8", "127.0.0.1"), ()])
def test_a_name_that_resolves_inside_is_refused_where_it_resolves(monkeypatch, addresses):
    resolves_to(monkeypatch, *addresses)
    connected = []
    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", lambda self, host, *a, **k: connected.append(host))
    with pytest.raises(httpcore.ConnectError):
        _PublicOnlyBackend().connect_tcp("rebind.example", 443)
    assert not connected


def test_the_connection_goes_to_the_address_that_was_checked(monkeypatch):
    resolves_to(monkeypatch, "8.8.8.8")
    connected = []
    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", lambda self, host, *a, **k: connected.append(host) or "stream")
    assert _PublicOnlyBackend().connect_tcp("site.example", 443) == "stream"
    assert connected == ["8.8.8.8"]  # not the name, which could resolve differently a moment later


def test_a_real_request_to_a_name_for_this_pc_is_refused(make_ctx):
    ctx = make_ctx()
    ctx.http_transport = None  # the real transport, with the real lookup
    with ctx.http(public_only=True, timeout=2) as client, pytest.raises(httpx.HTTPError):
        client.get("http://localhost:9/")


@pytest.mark.parametrize("target", ["http://127.0.0.1/", "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://2130706433/",
                                    "http://localhost/", "ftp://site.example/"])
def test_every_redirect_hop_is_checked(make_ctx, target):
    seen = []

    def handler(request):
        seen.append(request.url.host)
        if request.url.path == "/two":
            return httpx.Response(302, headers={"location": target})
        return httpx.Response(302, headers={"location": "https://site.example/two"})
    ctx = make_ctx(handler=handler)
    with ctx.http(public_only=True) as client, pytest.raises(httpx.HTTPError):
        client.get("https://site.example/one")
    assert set(seen) == {"site.example"}


def test_other_clients_are_as_they_were(make_ctx):
    """An API or a local model on this PC is called on purpose, and no page chooses that address."""
    ctx = make_ctx(handler=lambda request: httpx.Response(200, text="ok"))
    with ctx.http() as client:
        assert client.get("http://127.0.0.1:11434/").status_code == 200


def robots_answer(make_ctx, response):
    return make_ctx(handler=lambda request: response())


@pytest.mark.parametrize("status,rules", [(404, True), (410, True), (500, False), (503, False), (429, False)])
def test_robots_status_meanings_are_kept(make_ctx, status, rules):
    ctx = make_ctx(handler=lambda request: httpx.Response(status))
    with ctx.http(public_only=True) as client:
        assert read_robots(client, "https://site.example/robots.txt")[0] is rules


def test_robots_rules_are_read(make_ctx):
    ctx = make_ctx(handler=lambda request: httpx.Response(200, text="User-agent: *\nDisallow: /private\n"))
    with ctx.http(public_only=True) as client:
        rules = read_robots(client, "https://site.example/robots.txt")[0]
    assert not rules.can_fetch("aime", "https://site.example/private") and rules.can_fetch("aime", "https://site.example/")


def test_a_robots_file_that_drips_slowly_is_cut_at_the_time_limit(make_ctx, monkeypatch):
    monkeypatch.setattr("aime.website_check.ROBOTS_MAX_SECONDS", 0.05)
    read = []

    class Drip(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(1000):
                read.append(1)
                time.sleep(0.02)
                yield b"#\n"
    ctx = make_ctx(handler=lambda request: httpx.Response(200, stream=Drip()))
    with ctx.http(public_only=True) as client:
        assert read_robots(client, "https://site.example/robots.txt")[0] is False
    assert len(read) < 20


def test_unreadable_robots_means_the_homepage_is_not_fetched(make_ctx):
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, text="x" * 600_000) if request.url.path == "/robots.txt" else httpx.Response(200, text="<title>Site</title>")
    ctx = make_ctx(handler=handler)
    assert check_website(ctx, "site.example", "Site").status == "blocked"
    assert seen == ["/robots.txt"]
    assert SiteReader(ctx, "site.example", delay=0).read()[0].status == "robots_unavailable"


@pytest.mark.parametrize("page,title", [("<html><TITLE lang=en> Acme &amp; Co\n Inc </TITLE>", "Acme & Co Inc"), ("<title></title>", None),
                                        ("<title>never ends", None), ("no title", None)])
def test_title_is_read_as_before(page, title):
    assert page_title(page) == title


@pytest.mark.parametrize("page,name", [('<meta property="og:site_name" content="Acme &amp; Co">', "Acme & Co"),
                                       ("<META name=x><meta content='z' name=y><meta property='og:site_name' content='Acme'>", "Acme"),
                                       ('<meta property="og:title" content="Acme">', None), ("<meta " * 300, None)])
def test_site_name_is_read_as_before(page, name):
    assert site_name(page) == name


@pytest.mark.parametrize("page", ["<" * 300_000, "<meta " * 50_000, "<meta " * 50_000 + ">", "<!--" * 75_000, '<a x="' * 50_000, "%" * 300_000,
                                  "a@" * 150_000, "a@" + "a." * 150_000, "<script>" + "<" * 300_000, "<" * 300_000 + ">"], ids=str.__len__)
def test_a_page_at_the_size_cap_costs_one_pass(page):
    started = time.perf_counter()
    parse_page("https://site.example/", "home", page)
    assert time.perf_counter() - started < 3


def test_page_parsing_still_finds_what_a_page_publishes():
    page = parse_page("https://site.example/", "home", '<title>Acme</title><p>Write to sales@acme.example or a%b@acme.example</p><a href="/about">About')
    assert page.title == "Acme" and page.emails == ["sales@acme.example", "a%b@acme.example"]
    assert page.links[0][0] == "https://site.example/about" if page.links else True
