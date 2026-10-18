"""Check a built documentation site for external resources and missing local files.

The site must work on an internal network, and a page that loads a script, a style, a
font, an image or a frame from another host breaks there. Zensical builds such a page
without complaint, and so it builds a page whose image file does not exist. This
script reads the built HTML and CSS and reports both.

An element is *loading* when it is a ``script``, ``img``, ``iframe``, ``embed``,
``source``, ``video``, ``audio`` or ``object``, or a ``link`` whose ``rel`` fetches
something (a stylesheet, an icon, a preload and so on). Its URL is *external* when it
starts with ``http://``, ``https://`` or ``//``. A plain ``<a href>`` is a link the
reader may follow, not a load, and is fine. A CSS file or ``<style>`` element is
external when an ``@import`` or a ``url()`` (a ``data:`` URL is not) names another host.

The site is served under the path of ``site_url`` in ``mkdocs.yml`` (``/lakana/`` on GitHub
Pages), so a ``href`` or ``src`` that starts with a single ``/`` and not with that path would
point outside the site; it is reported, ``<a>`` links included.

Usage: ``uv run python scripts/check_built_site.py site-build``
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Sequence
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

_SITE_URL = re.compile(r"^site_url:\s*['\"]?(?P<url>[^'\"\s#]+)", re.MULTILINE)

_LOADING_ATTRIBUTES = {
    "script": ("src",),
    "img": ("src", "srcset"),
    "iframe": ("src",),
    "embed": ("src",),
    "source": ("src", "srcset"),
    "video": ("src", "poster"),
    "audio": ("src",),
    "object": ("data",),
    "link": ("href",),
}
_NOT_LOADED_RELS = {
    "canonical",
    "alternate",
    "next",
    "prev",
    "previous",
    "author",
    "help",
    "license",
    "search",
    "first",
    "last",
    "up",
    "index",
}
_CSS_URL = re.compile(r"""url\(\s*(['"]?)(?P<url>.*?)\1\s*\)""", re.IGNORECASE | re.DOTALL)
_CSS_IMPORT = re.compile(r"""@import\s+(?:url\(\s*)?['"]?(?P<url>[^'")\s;]+)""", re.IGNORECASE)


def is_external(url: str) -> bool:
    """Say whether a URL names another host.

    Parameters
    ----------
    url
        An attribute value or a CSS URL.

    Returns
    -------
    bool
        ``True`` for an absolute ``http`` or ``https`` URL and for a scheme-relative one.
    """
    return url.strip().lower().startswith(("http://", "https://", "//"))


def _urls(attribute: str, value: str) -> list[str]:
    """Split an attribute into the URLs it holds.

    Parameters
    ----------
    attribute
        The attribute's name; ``srcset`` holds a comma-separated list with descriptors.
    value
        The attribute's value.

    Returns
    -------
    list of str
        The URLs, without the width and density descriptors of a ``srcset``.
    """
    if attribute != "srcset":
        return [value.strip()]
    return [part.split()[0] for part in value.split(",") if part.strip()]


def css_problems(text: str) -> list[str]:
    """Find the external URLs of a stylesheet.

    Parameters
    ----------
    text
        CSS.

    Returns
    -------
    list of str
        One external URL per ``@import`` or ``url()`` that names another host.
    """
    found = [m["url"].strip() for m in _CSS_IMPORT.finditer(text)]
    found += [m["url"].strip() for m in _CSS_URL.finditer(text)]
    return sorted({url for url in found if is_external(url)})


class _Loads(HTMLParser):
    """Collect what a page loads and what its style elements import.

    Parameters
    ----------
    convert_charrefs
        Passed to the base class so that entities in attribute values are decoded.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.loads: list[tuple[str, str]] = []
        self.anchors: list[str] = []
        self.style = ""
        self._in_style = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Record the loading attributes of an element.

        Parameters
        ----------
        tag
            The element's name.
        attrs
            Its attributes.
        """
        values = {name: value or "" for name, value in attrs}
        if tag == "style":
            self._in_style = True
        if tag == "a" and values.get("href", "").strip():
            self.anchors.append(values["href"].strip())
        for attribute in _LOADING_ATTRIBUTES.get(tag, ()):
            if attribute not in values:
                continue
            if tag == "link" and not self._link_loads(values.get("rel", "")):
                continue
            self.loads.extend((tag, url) for url in _urls(attribute, values[attribute]) if url)

    @staticmethod
    def _link_loads(rel: str) -> bool:
        """Say whether a ``link`` element makes the browser fetch its target.

        Parameters
        ----------
        rel
            The element's ``rel`` value.

        Returns
        -------
        bool
            ``False`` for the relations that only name another page.
        """
        return not set(rel.lower().split()) <= _NOT_LOADED_RELS

    def handle_endtag(self, tag: str) -> None:
        """Leave a style element.

        Parameters
        ----------
        tag
            The element's name.
        """
        if tag == "style":
            self._in_style = False

    def handle_data(self, data: str) -> None:
        """Keep the text of a style element.

        Parameters
        ----------
        data
            Text inside an element.
        """
        if self._in_style:
            self.style += data


def site_base_path(config: Path) -> str:
    """Read the path the site is served under from the ``site_url`` of its configuration.

    Parameters
    ----------
    config
        The ``mkdocs.yml`` file; it may be missing.

    Returns
    -------
    str
        The path with a leading and a trailing slash, ``/`` for a site at the root of its
        host, for a configuration without ``site_url`` and for a missing file.
    """
    try:
        text = config.read_text(encoding="utf-8")
    except OSError:
        return "/"
    match = _SITE_URL.search(text)
    path = urlsplit(match["url"]).path.strip("/") if match else ""
    return f"/{path}/" if path else "/"


def _exists(site: Path, page: Path, url: str, base_path: str = "/") -> bool:
    """Say whether a local URL points at a file of the built site.

    Parameters
    ----------
    site
        The built site's directory.
    page
        The HTML file that holds the URL.
    url
        A relative or root-relative URL, possibly with a query or a fragment.
    base_path
        The path the site is served under; a root-relative URL starts with it.

    Returns
    -------
    bool
        Whether the file, or the directory's ``index.html``, exists inside `site`.
    """
    path = unquote(urlsplit(url).path)
    if path.startswith("/"):
        path = path.removeprefix(base_path.rstrip("/"))
    base = site if path.startswith("/") else page.parent
    target = (base / path.lstrip("/")).resolve()
    if target.is_dir():
        target = target / "index.html"
    return target.is_file() and target.is_relative_to(site.resolve())


def _outside_the_site(url: str, base_path: str) -> bool:
    """Say whether a URL starts at the host root, outside the path the site is served under.

    Parameters
    ----------
    url
        An attribute value.
    base_path
        The path the site is served under.

    Returns
    -------
    bool
        ``True`` for a URL with a single leading ``/`` that does not start with `base_path`.
    """
    parts = urlsplit(url)
    if parts.scheme or parts.netloc:
        return False
    return parts.path.startswith("/") and not parts.path.startswith(base_path)


def check_site(site: Path, base_path: str = "/") -> list[str]:
    """Check every HTML and CSS file of a built site.

    Parameters
    ----------
    site
        The built site's directory.
    base_path
        The path the site is served under (``/lakana/`` on GitHub Pages); see
        :func:`site_base_path`.

    Returns
    -------
    list of str
        One line per problem, naming the file and the URL and what to do.
    """
    problems: list[str] = []
    for css in sorted(site.rglob("*.css")):
        shown = css.relative_to(site)
        problems += [
            f"{shown}: the stylesheet loads {url} from another host; remove the "
            "feature that adds it, so the site works on an internal network."
            for url in css_problems(css.read_text(encoding="utf-8"))
        ]
    for page in sorted(site.rglob("*.html")):
        shown = page.relative_to(site)
        parser = _Loads()
        parser.feed(page.read_text(encoding="utf-8"))
        problems += [
            f"{shown}: a style element loads {url} from another host; remove the "
            "feature that adds it, so the site works on an internal network."
            for url in css_problems(parser.style)
        ]
        for tag, url in [*parser.loads, *(("a", href) for href in parser.anchors)]:
            if _outside_the_site(url, base_path):
                problems.append(
                    f"{shown}: <{tag}> points at {url}, which starts at the root of the host "
                    f"and so leaves the site under {base_path}; use a relative URL, or set "
                    "`site_url` in mkdocs.yml."
                )
            elif tag == "a":
                continue
            elif is_external(url):
                problems.append(
                    f"{shown}: <{tag}> loads {url} from another host; use a file under "
                    "docs/site/assets or remove it, so the site works on an internal network."
                )
            elif (
                urlsplit(url).scheme == ""
                and urlsplit(url).path
                and not _exists(site, page, url, base_path)
            ):
                problems.append(
                    f"{shown}: <{tag}> points at {url}, which is not in the built site; "
                    "add the file under docs/site or fix the path."
                )
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    """Check a built site and print what is wrong.

    Parameters
    ----------
    argv
        The command line without the program name; ``sys.argv[1:]`` if ``None``.

    Returns
    -------
    int
        ``1`` if there is a problem or the directory has no page, else ``0``.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("site", type=Path, help="the built site, usually site-build")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("mkdocs.yml"),
        help="the configuration whose site_url gives the path the site is served under",
    )
    args = parser.parse_args(argv)
    if not any(args.site.rglob("*.html")):
        print(
            f"{args.site} holds no HTML page; build the site first with "
            "`uv run --group docs zensical build --strict`."
        )
        return 1
    problems = check_site(args.site, site_base_path(args.config))
    for problem in problems:
        print(problem)
    pages = len(list(args.site.rglob("*.html")))
    count = "problem" if len(problems) == 1 else "problems"
    print(f"{pages} pages checked; {len(problems)} {count}.")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
