import pytest

from mcp_gateway.origin import OriginPolicy, normalize_origin


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://App.Example.COM", "https://app.example.com"),
        ("HTTPS://app.example.com:443", "https://app.example.com"),
        ("http://app.example.com:80", "http://app.example.com"),
        ("https://app.example.com:80", "https://app.example.com:80"),
        ("http://app.example.com:443", "http://app.example.com:443"),
        ("http://localhost:8787", "http://localhost:8787"),
        ("http://[::1]:8080", "http://[::1]:8080"),
        ("vscode-webview://abc", "vscode-webview://abc"),
    ],
)
def test_normalisation(raw: str, expected: str) -> None:
    assert normalize_origin(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "app.example.com",
        "https://",
        "https://app.example.com/",
        "https://app.example.com/path",
        "https://app.example.com?x=1",
        "https://user@app.example.com",
        "https://app.example.com:notaport",
        "https://app.example.com:99999",
        "null",
    ],
)
def test_not_an_origin(raw: str) -> None:
    assert normalize_origin(raw) is None


def test_no_origin_header_passes() -> None:
    assert OriginPolicy([]).allows([])


def test_listed_origin_passes_in_any_spelling() -> None:
    policy = OriginPolicy(["https://app.example.com"])
    assert policy.allows(["HTTPS://APP.example.com:443"])
    assert policy.allows([" https://app.example.com "])


@pytest.mark.parametrize(
    "header", [["https://evil.example"], ["https://app.example.com.evil.example"], ["null"], ["x"]]
)
def test_unlisted_or_malformed_origin_is_refused(header: list[str]) -> None:
    assert not OriginPolicy(["https://app.example.com"]).allows(header)


def test_duplicate_origin_headers_are_refused() -> None:
    policy = OriginPolicy(["https://app.example.com"])
    assert not policy.allows(["https://app.example.com", "https://app.example.com"])


def test_the_opaque_origin_is_refused_like_any_unlisted_origin() -> None:
    assert not OriginPolicy(["https://app.example.com"]).allows(["null"])
    assert not OriginPolicy([]).allows(["null"])


def test_empty_allow_list_refuses_every_origin() -> None:
    assert not OriginPolicy([]).allows(["https://app.example.com"])
