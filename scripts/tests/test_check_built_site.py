"""Tests for the built-site check."""

from pathlib import Path

import pytest

from scripts.check_built_site import (
    check_site,
    css_problems,
    is_external,
    main,
    site_base_path,
)


def _site(tmp_path: Path, files: dict[str, str]) -> Path:
    """Write a built site.

    Parameters
    ----------
    tmp_path
        A scratch directory.
    files
        File contents by path relative to the site.

    Returns
    -------
    Path
        The site directory.
    """
    site = tmp_path / "site"
    site.mkdir()
    for name, text in files.items():
        (site / name).parent.mkdir(parents=True, exist_ok=True)
        (site / name).write_text(text, encoding="utf-8")
    return site


def _page(body: str) -> str:
    """Wrap a body in a minimal page.

    Parameters
    ----------
    body
        The page's content.

    Returns
    -------
    str
        An HTML document.
    """
    return f"<!doctype html><html><head></head><body>{body}</body></html>"


@pytest.mark.parametrize(
    ("url", "external"),
    [
        ("https://a.example/x.js", True),
        ("HTTP://a.example/x.js", True),
        ("//cdn.example/x.js", True),
        (" https://a.example", True),
        ("assets/x.js", False),
        ("../x.js", False),
        ("/x.js", False),
        ("data:image/png;base64,AAAA", False),
        ("#top", False),
    ],
)
def test_is_external(url: str, external: bool) -> None:
    """Only absolute http(s) and scheme-relative URLs name another host."""
    assert is_external(url) is external


@pytest.mark.parametrize(
    "tag",
    [
        '<script src="https://cdn.example/x.js"></script>',
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css">',
        '<link rel="preload" href="//fonts.gstatic.com/x.woff2" as="font">',
        '<link rel="icon" href="https://example.com/i.png">',
        '<img src="https://example.com/pic.png">',
        '<img srcset="a.png 1x, https://example.com/b.png 2x" src="a.png">',
        '<iframe src="https://example.com/"></iframe>',
        '<video poster="https://example.com/p.png"></video>',
        '<audio src="https://example.com/a.mp3"></audio>',
        '<object data="https://example.com/o.swf"></object>',
        '<embed src="https://example.com/e">',
        '<video><source src="https://example.com/v.mp4"></video>',
    ],
)
def test_an_element_that_loads_from_another_host_is_reported(tmp_path: Path, tag: str) -> None:
    """Each loading element with an external URL is a problem naming file and URL."""
    site = _site(tmp_path, {"index.html": _page(tag), "a.png": "x"})
    (problem,) = check_site(site)
    assert problem.startswith("index.html: ")
    assert "://" in problem or "//" in problem
    assert "another host" in problem


def test_a_plain_link_to_another_site_is_fine(tmp_path: Path) -> None:
    """A reader may follow `<a href>`; nothing is loaded."""
    site = _site(
        tmp_path,
        {
            "index.html": _page(
                '<a href="https://zensical.org/">z</a>'
                '<link rel="canonical" href="https://example.com/">'
                '<link rel="next alternate" href="https://example.com/n">'
            )
        },
    )
    assert check_site(site) == []


def test_local_files_that_exist_pass(tmp_path: Path) -> None:
    """Relative, directory, query and fragment forms resolve."""
    site = _site(
        tmp_path,
        {
            "index.html": _page(
                '<img src="img/a%20b.png?v=1"><script src="js/x.js#h"></script>'
                '<link rel="icon" href="sub/"><img src="data:image/png;base64,AA">'
                '<link rel="stylesheet" href="#">'
            ),
            "img/a b.png": "x",
            "js/x.js": "x",
            "sub/index.html": _page(""),
        },
    )
    assert check_site(site) == []


@pytest.mark.parametrize(
    "tag",
    [
        '<img src="missing.png">',
        '<script src="missing.js"></script>',
        '<img src="../outside.png">',
    ],
)
def test_a_missing_local_file_is_reported(tmp_path: Path, tag: str) -> None:
    """A page may not name a file the build did not produce, nor one outside the site."""
    (tmp_path / "outside.png").write_text("x", encoding="utf-8")
    site = _site(tmp_path, {"index.html": _page(tag)})
    (problem,) = check_site(site)
    assert "not in the built site" in problem
    assert "index.html" in problem


def test_an_attribute_without_a_value_or_a_url_is_ignored(tmp_path: Path) -> None:
    """`<img>` without `src`, an empty `srcset` and a bare `src` attribute load nothing."""
    site = _site(tmp_path, {"index.html": _page('<img alt="x"><img srcset=" , "><img src>')})
    assert check_site(site) == []


def test_css_urls_and_imports_to_another_host_are_reported() -> None:
    """`url()` and `@import` to a host are found; `data:` URLs and local files are not."""
    css = (
        '@import "https://fonts.example/css";'
        "@import url(//other.example/a.css);"
        ".a{background:url('https://img.example/a.png')}"
        '.b{background:url(data:image/svg+xml;charset=utf-8,<svg xmlns="http://www.w3.org/2000/svg"/>)}'
        ".c{background:url('data:image/svg+xml,<svg xmlns=\"http://www.w3.org/2000/svg\"/>')}"
        ".d{background:url(local.png)}"
    )
    assert css_problems(css) == [
        "//other.example/a.css",
        "https://fonts.example/css",
        "https://img.example/a.png",
    ]


def test_a_css_file_and_a_style_element_are_checked(tmp_path: Path) -> None:
    """Both places a stylesheet can hide an external load."""
    site = _site(
        tmp_path,
        {
            "index.html": _page('<style>@import "https://s.example/x.css";</style><p>text</p>'),
            "assets/main.css": ".a{background:url(https://c.example/a.png)}",
        },
    )
    problems = check_site(site)
    assert len(problems) == 2
    assert any(p.startswith("assets/main.css: ") and "c.example" in p for p in problems)
    assert any(p.startswith("index.html: a style element") and "s.example" in p for p in problems)


def test_main_reports_and_counts(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Exit 1 with the problems; exit 0 when the site is clean."""
    site = _site(tmp_path, {"index.html": _page('<img src="https://e.example/a.png">')})
    assert main([str(site)]) == 1
    out = capsys.readouterr().out
    assert "e.example" in out
    assert "1 pages checked; 1 problem." in out
    (site / "index.html").write_text(_page("<p>x</p>"), encoding="utf-8")
    assert main([str(site)]) == 0
    assert "1 pages checked; 0 problems." in capsys.readouterr().out


def test_main_refuses_a_directory_without_pages(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing build must not pass as "nothing wrong"."""
    assert main([str(tmp_path / "nope")]) == 1
    assert "zensical build" in capsys.readouterr().out


@pytest.mark.parametrize(
    "tag",
    [
        '<link rel="stylesheet" href="/assets/main.css">',
        '<script src="/js/x.js"></script>',
        '<img src="/img/a.png">',
        '<a href="/how-to/">x</a>',
        '<a href="/">x</a>',
    ],
)
def test_a_root_absolute_url_is_reported(tmp_path: Path, tag: str) -> None:
    """The site is served under `/lakana/`, so a URL that starts at the host root breaks there."""
    site = _site(
        tmp_path,
        {"index.html": _page(tag), "assets/main.css": "x", "js/x.js": "x", "img/a.png": "x"}
        | {"how-to/index.html": _page("")},
    )
    (problem,) = check_site(site, base_path="/lakana/")
    assert "index.html" in problem
    assert "starts at the root of the host" in problem
    assert "site_url" in problem


def test_a_url_under_the_site_s_own_base_path_resolves_inside_the_site(tmp_path: Path) -> None:
    """Zensical writes `/lakana/...` into 404.html; that is right under the deployed address."""
    files = {
        "404.html": _page('<link rel="icon" href="/lakana/img/a.png"><a href="/lakana/">x</a>'),
        "index.html": _page(""),
        "img/a.png": "x",
    }
    assert check_site(_site(tmp_path, files), base_path="/lakana/") == []
    files["404.html"] = _page('<img src="/lakana/img/gone.png">')
    (tmp_path / "again").mkdir()
    (problem,) = check_site(_site(tmp_path / "again", files), base_path="/lakana/")
    assert "not in the built site" in problem


def test_a_relative_anchor_and_an_external_anchor_are_fine(tmp_path: Path) -> None:
    """Only a URL that starts with a single slash is refused."""
    site = _site(
        tmp_path,
        {"index.html": _page('<a href="./x/">x</a><a href="https://e.example/">e</a><a href="#t">')}
        | {"x/index.html": _page("")},
    )
    assert check_site(site, base_path="/lakana/") == []


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ("site_url: https://lakana-labs.github.io/lakana/\n", "/lakana/"),
        ('a: 1\nsite_url: "https://h.example/docs"\n', "/docs/"),
        ("site_url: https://h.example/\n", "/"),
        ("site_name: x\n", "/"),
    ],
)
def test_the_base_path_comes_from_the_site_url(tmp_path: Path, config: str, expected: str) -> None:
    """A config without `site_url` means a site at the root of its host."""
    path = tmp_path / "mkdocs.yml"
    path.write_text(config, encoding="utf-8")
    assert site_base_path(path) == expected
    assert site_base_path(tmp_path / "missing.yml") == "/"


def test_main_reads_the_base_path_from_the_config(tmp_path: Path) -> None:
    """`--config` names the file whose `site_url` decides which absolute URLs are legal."""
    site = _site(tmp_path, {"index.html": _page('<img src="/lakana/a.png">'), "a.png": "x"})
    config = tmp_path / "mkdocs.yml"
    config.write_text("site_url: https://h.example/lakana/\n", encoding="utf-8")
    assert main([str(site), "--config", str(config)]) == 0
    config.write_text("site_name: x\n", encoding="utf-8")
    assert main([str(site), "--config", str(config)]) == 1


def test_main_refuses_a_missing_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A missing config would silently turn the root-absolute check off, so it is an error.

    Parameters
    ----------
    tmp_path
        Holds the built site.
    capsys
        Captures what the checker prints.
    """
    site = _site(tmp_path, {"index.html": _page('<img src="/assets/a.png">')})
    assert main([str(site), "--config", str(tmp_path / "missing.yml")]) == 1
    assert "missing.yml" in capsys.readouterr().out
