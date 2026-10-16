"""Pure validation of repository URLs, refs and local paths."""

from __future__ import annotations

from pathlib import Path

import pytest

from lakana_core.errors import ValidationError
from lakana_core.models import RepositoryKind
from lakana_core.projects.git_input import (
    is_full_sha,
    resolve_local_path,
    validate_ref,
    validate_repo_url,
)


@pytest.mark.parametrize(
    "ref", ["main", "release/1.2", "v1.0.0", "HEAD", "refs/heads/main", "feature/a-b_c"]
)
def test_valid_refs_pass(ref: str) -> None:
    """Ordinary branch, tag and full ref names are accepted unchanged."""
    assert validate_ref(ref) == ref


def test_full_sha_is_lower_cased() -> None:
    """A 40-hex SHA in any case becomes lower case."""
    assert validate_ref("A" * 40) == "a" * 40
    assert is_full_sha("a" * 40)
    assert not is_full_sha("a" * 39)
    assert not is_full_sha("g" * 40)


@pytest.mark.parametrize(
    "ref",
    [
        "",
        "--upload-pack=touch /tmp/pwned",
        "-x",
        "a..b",
        "a b",
        "a\x00b",
        "a\nb",
        "a~1",
        "a^",
        "a:b",
        "a?",
        "a*",
        "a[",
        "a\\b",
        "a@{1}",
        "@",
        "a/",
        "a.",
        "/a",
        "a//b",
        "a/.hidden",
        "a.lock",
        "x" * 256,
    ],
)
def test_invalid_refs_are_refused(ref: str) -> None:
    """`check-ref-format` rules, plus no leading `-` (argument injection)."""
    with pytest.raises(ValidationError):
        validate_ref(ref)


@pytest.mark.parametrize(
    "ref",
    [
        "refs/pull/1/head",
        "refs/changes/12/1234/1",
        "refs/merge-requests/3/head",
        "refs/remotes/origin/main",
        "refs/notes/commits",
        "refs/heads",
        "refs/tags",
        "refs/foo",
    ],
)
def test_refs_outside_branches_and_tags_are_refused(ref: str) -> None:
    """Only branches, tags and HEAD deploy: pull/review namespaces never do."""
    with pytest.raises(ValidationError, match="branch, a tag or HEAD"):
        validate_ref(ref)


@pytest.mark.parametrize(
    "ref",
    [
        "refs/tags/v1",
        "refs/heads/feature/x",
        "feature/x",
        "release/1",
        "environments/prod",
        "pipelines/x",
        "remotes/x",
        "pull/123/head",
    ],
)
def test_branch_and_tag_namespaces_are_accepted(ref: str) -> None:
    """Qualified heads and tags and any short name pass; the worker resolves short names exactly."""
    assert validate_ref(ref) == ref


@pytest.mark.parametrize(
    ("kind", "url"),
    [
        (RepositoryKind.HTTPS, "https://github.com/acme/analytics.git"),
        (RepositoryKind.HTTPS, "https://gitlab.internal:8443/data/analytics"),
        (RepositoryKind.HTTPS, "https://10.0.0.5/repo.git"),
        (RepositoryKind.SSH, "ssh://git@github.com/acme/analytics.git"),
        (RepositoryKind.SSH, "ssh://git@gitlab.internal:2222/data/analytics.git"),
        (RepositoryKind.SSH, "git@github.com:acme/analytics.git"),
        (RepositoryKind.SSH, "gitlab.internal:data/analytics.git"),
        (RepositoryKind.LOCAL, "/srv/repos/analytics"),
    ],
)
def test_valid_urls_pass(kind: RepositoryKind, url: str) -> None:
    """Allowed schemes, private addresses included (the admin grant is the boundary)."""
    assert validate_repo_url(kind, url) == url


@pytest.mark.parametrize(
    ("kind", "url", "message"),
    [
        (RepositoryKind.HTTPS, "https://user:token@github.com/a/b", "credentials"),
        (RepositoryKind.HTTPS, "https://token@github.com/a/b", "credentials"),
        (RepositoryKind.HTTPS, "http://github.com/a/b", "https://"),
        (RepositoryKind.HTTPS, "file:///etc", "https://"),
        (RepositoryKind.HTTPS, "https:///nohost", "host"),
        (RepositoryKind.HTTPS, "https://github.com/a b", "whitespace"),
        (RepositoryKind.SSH, "ssh://git:pw@github.com/a/b", "password"),
        (RepositoryKind.SSH, "ssh://-oProxyCommand=x/a", "host"),
        (RepositoryKind.SSH, "-oProxyCommand=x:a", "host"),
        (RepositoryKind.SSH, "git@github.com:-upload", "path"),
        (RepositoryKind.SSH, "https://github.com/a/b", "ssh://"),
        (RepositoryKind.SSH, "ext::sh -c touch% /tmp/pwned", "whitespace"),
        (RepositoryKind.SSH, "ext::sh", "ssh://"),
        (RepositoryKind.LOCAL, "relative/path", "absolute"),
        (RepositoryKind.LOCAL, "file:///srv/repo", "absolute"),
        (RepositoryKind.LOCAL, "/srv/a\nb", "control characters"),
        (RepositoryKind.LOCAL, "/srv/a\u202eb", "control characters"),
        (RepositoryKind.LOCAL, "/srv/a\x85b", "control characters"),
        (RepositoryKind.SSH, "ssh://-oProxyCommand=sh@host/a", "user name"),
        (RepositoryKind.SSH, "ssh://%2DoProxyCommand=x/a", "percent-encoded"),
        (RepositoryKind.SSH, "ssh://host:-oX/a", "malformed"),
        (RepositoryKind.HTTPS, "https://[::1/x", "malformed"),
        (RepositoryKind.HTTPS, "https://[evil]/x", "malformed"),
        (RepositoryKind.HTTPS, "https://evil.com\uff20github.com/x", "malformed"),
        (RepositoryKind.SSH, "ssh://h\uff0fx/a", "malformed"),
    ],
)
def test_invalid_urls_are_refused(kind: RepositoryKind, url: str, message: str) -> None:
    """Userinfo, other schemes, option-shaped hosts and paths, and relative paths are refused."""
    with pytest.raises(ValidationError, match=message):
        validate_repo_url(kind, url)


def test_local_path_must_be_under_a_root(tmp_path: Path) -> None:
    """The resolved path must lie under one of the configured roots."""
    root = tmp_path / "repos"
    (root / "analytics").mkdir(parents=True)
    assert resolve_local_path(str(root / "analytics"), [str(root)]) == str(
        (root / "analytics").resolve()
    )
    with pytest.raises(ValidationError, match="not under any of LAKANA_REPO_ROOTS"):
        resolve_local_path(str(tmp_path), [str(root)])


def test_outside_a_root_names_the_next_step_without_revealing_the_roots(tmp_path: Path) -> None:
    """The message tells a local user and an operator what to do, and never prints a root."""
    root = tmp_path / "secret-root-name"
    root.mkdir()
    target = tmp_path / "hidden-target"
    target.mkdir()
    (tmp_path / "alias").symlink_to(target)
    for candidate in (tmp_path, tmp_path / "alias"):
        with pytest.raises(ValidationError) as caught:
            resolve_local_path(str(candidate), [str(root)])
        message = str(caught.value)
        assert "lakana server start --local" in message
        assert "home directory" in message
        assert "operator" in message
        assert "LAKANA_REPO_ROOTS" in message
        assert "secret-root-name" not in message
        assert "hidden-target" not in message


def test_outside_a_root_the_answer_does_not_depend_on_whether_the_path_exists(
    tmp_path: Path,
) -> None:
    """An existing and a missing path outside the roots get the same message (no probing)."""
    root = tmp_path / "repos"
    root.mkdir()
    existing = tmp_path / "exists"
    existing.mkdir()
    (tmp_path / "dangling").symlink_to(tmp_path / "nowhere")
    messages = set()
    for name in ("exists", "missing", "dangling", "missing/deeper"):
        with pytest.raises(ValidationError) as caught:
            resolve_local_path(str(tmp_path / name), [str(root)])
        messages.add(str(caught.value).replace(str(tmp_path / name), "<path>"))
    assert len(messages) == 1
    assert "not under any of LAKANA_REPO_ROOTS" in messages.pop()


def test_sibling_refusals_name_the_next_step(tmp_path: Path) -> None:
    """Disabled roots and a missing directory under a root say what to do."""
    with pytest.raises(ValidationError) as disabled:
        resolve_local_path(str(tmp_path), [])
    text = str(disabled.value)
    assert "the operator sets LAKANA_REPO_ROOTS" in text
    assert "lakana server start --local" in text
    assert "home directory" in text
    with pytest.raises(ValidationError) as missing:
        resolve_local_path(str(tmp_path / "gone"), [str(tmp_path)])
    assert "Check the path" in str(missing.value)


def test_local_path_symlink_escaping_a_root_is_refused(tmp_path: Path) -> None:
    """Symlinks are resolved before the check."""
    root = tmp_path / "repos"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside)
    with pytest.raises(ValidationError, match="not under any of LAKANA_REPO_ROOTS"):
        resolve_local_path(str(root / "link"), [str(root)])


def test_root_prefix_is_not_a_root(tmp_path: Path) -> None:
    """`/repos-evil` is not under `/repos`."""
    (tmp_path / "repos").mkdir()
    (tmp_path / "repos-evil").mkdir()
    with pytest.raises(ValidationError, match="not under any"):
        resolve_local_path(str(tmp_path / "repos-evil"), [str(tmp_path / "repos")])


def test_local_repositories_disabled_without_roots(tmp_path: Path) -> None:
    """An empty `LAKANA_REPO_ROOTS` refuses every local repository."""
    with pytest.raises(ValidationError, match="LAKANA_REPO_ROOTS is empty"):
        resolve_local_path(str(tmp_path), [])


def test_local_path_must_be_an_existing_directory(tmp_path: Path) -> None:
    """Resolving needs the path to exist."""
    with pytest.raises(ValidationError, match="is not a directory"):
        resolve_local_path(str(tmp_path / "missing"), [str(tmp_path)])


@pytest.mark.parametrize(
    ("kind", "url", "message"),
    [
        (RepositoryKind.HTTPS, "https://github.com/" + "a" * 2048, "longer than"),
        (RepositoryKind.LOCAL, "/srv/repos/a\x00b", "control characters"),
        (RepositoryKind.LOCAL, "/srv/" + "a" * 2048, "longer than"),
        (RepositoryKind.SSH, "ssh://git@github.com/-upload", "path"),
        (RepositoryKind.SSH, "git@-host:a", "host must not start"),
    ],
)
def test_more_invalid_urls_are_refused(kind: RepositoryKind, url: str, message: str) -> None:
    """Over-long URLs, NUL in a local path, option-shaped ssh path and user-prefixed host."""
    with pytest.raises(ValidationError, match=message):
        validate_repo_url(kind, url)


def test_local_path_with_spaces_is_accepted(tmp_path: Path) -> None:
    """Ordinary spaces and printable non-ASCII are fine in a local path."""
    repo = tmp_path / "My Projects" / "répo"
    repo.mkdir(parents=True)
    assert validate_repo_url(RepositoryKind.LOCAL, str(repo)) == str(repo)
    assert resolve_local_path(str(repo), [str(tmp_path)]) == str(repo.resolve())


@pytest.mark.parametrize(
    ("kind", "url"),
    [
        (RepositoryKind.SSH, "ssh://good.example?@evil.example/repo"),
        (RepositoryKind.SSH, "ssh://h#@-oX/a"),
        (RepositoryKind.SSH, "ssh://git@github.com/acme/repo.git?x"),
        (RepositoryKind.SSH, "ssh://git@github.com/acme/repo.git#frag"),
        (RepositoryKind.SSH, "git@github.com:acme/repo.git?x"),
        (RepositoryKind.SSH, "git@github.com:acme/repo#x"),
        (RepositoryKind.HTTPS, "https://good.example?@evil.example/repo"),
        (RepositoryKind.HTTPS, "https://good.example#@evil.example/repo"),
        (RepositoryKind.HTTPS, "https://github.com/acme/repo.git?x=1"),
    ],
)
def test_query_and_fragment_characters_are_refused(kind: RepositoryKind, url: str) -> None:
    """`urlsplit` ends the host at `?`/`#` and git does not: `ssh://a?@b/r` reaches `b`."""
    with pytest.raises(ValidationError, match=r"must not contain '\?' or '#'"):
        validate_repo_url(kind, url)


def test_local_path_named_star_is_refused(tmp_path: Path) -> None:
    """A last component `*` would make the worker's `safe.directory` entry a prefix glob."""
    star = tmp_path / "repos" / "*"
    star.mkdir(parents=True)
    with pytest.raises(ValidationError, match=r"last component must not be '\*'"):
        resolve_local_path(str(star), [str(tmp_path / "repos")])
    link = tmp_path / "repos" / "innocent"
    link.symlink_to(star)
    with pytest.raises(ValidationError, match=r"last component must not be '\*'"):
        resolve_local_path(str(link), [str(tmp_path / "repos")])
    assert resolve_local_path(str(tmp_path / "repos"), [str(tmp_path / "repos")])
