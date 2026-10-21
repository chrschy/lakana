"""Tests for the documentation code-block checks."""

import os
import runpy
from pathlib import Path

import pytest

from scripts import docs_blocks
from scripts.docs_blocks import (
    BlockError,
    Marker,
    check,
    find_blocks,
    fix,
    main,
    read_source,
    syntax_problem,
)


def _page(tmp_path: Path, text: str) -> Path:
    """Write a Markdown page.

    Parameters
    ----------
    tmp_path
        The test's temporary directory.
    text
        The page's content.

    Returns
    -------
    Path
        The page's path.
    """
    page = tmp_path / "page.md"
    page.write_bytes(text.encode("utf-8"))
    return page


def test_a_fenced_block_is_found_with_its_language_line_and_body(tmp_path: Path) -> None:
    """The basic case."""
    page = _page(tmp_path, "Text\n\n```python\nx = 1\ny = 2\n```\n\nMore\n")
    (block,) = find_blocks(page)
    assert (block.lang, block.line, block.body, block.marker) == (
        "python",
        3,
        "x = 1\ny = 2\n",
        None,
    )
    assert (block.start, block.end, block.fence, block.indent) == (3, 5, "```", "")


def test_a_marker_on_the_line_before_belongs_to_the_block(tmp_path: Path) -> None:
    """The three markers, each directly above its fence."""
    page = _page(
        tmp_path,
        "<!-- source: docs/examples/a/project/x.sql#first -->\n```sql\nSELECT 1\n```\n"
        "<!-- not tested: needs a cloud account -->\n```console\naws s3 ls\n```\n"
        "<!-- output -->\n```text\n1 row\n```\n",
    )
    assert [b.marker for b in find_blocks(page)] == [
        Marker("source", "docs/examples/a/project/x.sql#first"),
        Marker("not-tested", "needs a cloud account"),
        Marker("output", ""),
    ]


def test_a_marker_separated_by_a_blank_line_is_refused(tmp_path: Path) -> None:
    """Only the line directly above the fence is a marker; a gap is an error, not a free pass."""
    page = _page(tmp_path, "<!-- output -->\n\n```text\nx\n```\n")
    with pytest.raises(BlockError, match="directly above"):
        find_blocks(page)


def test_a_marker_with_leading_indent_in_a_list_counts(tmp_path: Path) -> None:
    """A marker indented like its fence inside a list item is still a marker."""
    page = _page(tmp_path, "1. Step\n\n    <!-- output -->\n    ```text\n    x\n    ```\n")
    assert find_blocks(page)[0].marker == Marker("output", "")


def test_a_block_inside_a_list_item_is_found_and_dedented(tmp_path: Path) -> None:
    """An indented fence is still a code block; its body loses the indent."""
    page = _page(tmp_path, "1. Step\n\n    ```toml\n    a = 1\n    ```\n")
    (block,) = find_blocks(page)
    assert (block.lang, block.body, block.indent) == ("toml", "a = 1\n", "    ")


def test_a_blank_line_and_a_shallow_line_in_an_indented_body_are_kept(tmp_path: Path) -> None:
    """A blank line has no indent to lose; a line indented less than the fence loses what it has."""
    page = _page(tmp_path, "1. Step\n\n    ```text\n    a\n\n  b\n    ```\n")
    assert find_blocks(page)[0].body == "a\n\nb\n"


def test_tabs_as_indent_are_removed_from_the_body(tmp_path: Path) -> None:
    """A tab-indented fence dedents by the same tab."""
    page = _page(tmp_path, "-\t Step\n\n\t```text\n\tx\n\t```\n")
    (block,) = find_blocks(page)
    assert (block.indent, block.body) == ("\t", "x\n")


def test_a_longer_fence_may_contain_a_shorter_one(tmp_path: Path) -> None:
    """Four backticks wrap three; the inner fence is body, not a block."""
    page = _page(tmp_path, "````markdown\n```python\nx\n```\n````\n")
    (block,) = find_blocks(page)
    assert (block.lang, block.body) == ("markdown", "```python\nx\n```\n")


def test_a_fence_of_the_other_character_does_not_close_a_block(tmp_path: Path) -> None:
    """Tildes do not close a backtick fence."""
    page = _page(tmp_path, "```text\n~~~\n```\n")
    assert find_blocks(page)[0].body == "~~~\n"


def test_a_closing_fence_may_be_longer_than_the_opening_one(tmp_path: Path) -> None:
    """Three backticks open, five close."""
    page = _page(tmp_path, "```text\nx\n`````\nafter\n")
    (block,) = find_blocks(page)
    assert (block.body, block.end) == ("x\n", 2)


def test_a_closing_fence_may_be_followed_by_spaces(tmp_path: Path) -> None:
    """Trailing whitespace on the closing fence is allowed."""
    page = _page(tmp_path, "```text\nx\n```   \n")
    assert find_blocks(page)[0].body == "x\n"


def test_a_fence_line_with_text_after_it_does_not_close_a_block(tmp_path: Path) -> None:
    """A closing fence carries no info string."""
    page = _page(tmp_path, "```text\n```python\nx\n```\n")
    assert find_blocks(page)[0].body == "```python\nx\n"


def test_tilde_fences_are_blocks_too(tmp_path: Path) -> None:
    """`~~~` opens and closes a block."""
    page = _page(tmp_path, "~~~json\n{}\n~~~\n")
    assert find_blocks(page)[0].lang == "json"


def test_a_tilde_info_string_may_contain_a_backtick(tmp_path: Path) -> None:
    """Only a backtick fence forbids backticks in its info string."""
    page = _page(tmp_path, "~~~text `a`\nx\n~~~\n")
    assert find_blocks(page)[0].lang == "text"


def test_inline_code_with_three_backticks_is_not_a_fence(tmp_path: Path) -> None:
    """A line like ```code``` is inline code, not an opening fence."""
    assert find_blocks(_page(tmp_path, "```code``` is inline\n\ntext\n")) == []


def test_a_block_without_a_language_has_an_empty_language(tmp_path: Path) -> None:
    """No info string."""
    assert find_blocks(_page(tmp_path, "```\nx\n```\n"))[0].lang == ""


def test_attributes_after_the_language_are_ignored(tmp_path: Path) -> None:
    """The language is the first word of the info string, lower-cased."""
    page = _page(tmp_path, '```Python title="a.py" hl_lines="1"\nx = 1\n```\n')
    assert find_blocks(page)[0].lang == "python"


def test_an_empty_block_has_an_empty_body(tmp_path: Path) -> None:
    """Nothing between the fences."""
    (block,) = find_blocks(_page(tmp_path, "```text\n```\n"))
    assert (block.body, block.start, block.end) == ("", 1, 1)


def test_windows_line_endings_give_the_same_blocks(tmp_path: Path) -> None:
    """CRLF pages parse as LF pages do."""
    page = _page(tmp_path, "<!-- output -->\r\n```text\r\nx\r\n```\r\n")
    (block,) = find_blocks(page)
    assert (block.body, block.marker) == ("x\n", Marker("output", ""))


def test_a_page_without_blocks_has_none(tmp_path: Path) -> None:
    """Plain text, including an empty page."""
    assert find_blocks(_page(tmp_path, "")) == []
    assert find_blocks(_page(tmp_path, "just text\n")) == []


def test_an_unclosed_fence_is_an_error_naming_the_line(tmp_path: Path) -> None:
    """A page that ends inside a block is broken."""
    with pytest.raises(BlockError, match=r"page\.md:2: .*never closed"):
        find_blocks(_page(tmp_path, "Text\n```python\nx = 1\n"))


def test_a_shorter_closing_fence_leaves_the_block_unclosed(tmp_path: Path) -> None:
    """Three backticks do not close four."""
    with pytest.raises(BlockError, match=r"page\.md:1: .*never closed"):
        find_blocks(_page(tmp_path, "````text\nx\n```\n"))


@pytest.mark.parametrize(
    "marker",
    ["<!-- not tested: -->", "<!-- not tested -->", "<!-- source: -->", "<!-- source:  #r -->"],
)
def test_a_marker_without_its_value_is_an_error(tmp_path: Path, marker: str) -> None:
    """A reason and a path are required."""
    with pytest.raises(BlockError, match=r"page\.md:1: "):
        find_blocks(_page(tmp_path, f"{marker}\n```text\nx\n```\n"))


@pytest.mark.parametrize(
    ("lang", "body"),
    [
        ("python", "x = 1\n"),
        ("py", "def f():\n    return 1\n"),
        ("toml", '[project]\nname = "a"\n'),
        ("yaml", "a:\n  - 1\n"),
        ("yaml", "a: 1\n---\nb: 2\n"),
        ("yml", "a: 1\n"),
        ("json", '{"a": [1, 2]}\n'),
    ],
)
def test_valid_code_has_no_syntax_problem(lang: str, body: str) -> None:
    """Each parsed language accepts valid input."""
    assert syntax_problem(lang, body) is None


@pytest.mark.parametrize(
    ("lang", "body"),
    [
        ("python", "def f(:\n"),
        ("toml", "a = \n"),
        ("yaml", "a: [1\n"),
        ("json", "{a: 1}\n"),
    ],
)
def test_invalid_code_is_reported(lang: str, body: str) -> None:
    """Each parsed language rejects invalid input with a message."""
    assert syntax_problem(lang, body)


def test_a_language_the_script_cannot_parse_is_not_judged() -> None:
    """Console, SQL and text are never syntax-checked."""
    for lang in ("console", "sql", "text", "bash", ""):
        with pytest.raises(ValueError, match="cannot parse"):
            syntax_problem(lang, "anything")


def _tree(tmp_path: Path, pages: dict[str, str], examples: dict[str, str]) -> Path:
    """Build a repository root with a site and examples.

    Parameters
    ----------
    tmp_path
        The test's temporary directory.
    pages
        Page paths relative to ``docs/site`` and their content.
    examples
        File paths relative to ``docs/examples`` and their content.

    Returns
    -------
    Path
        The root.
    """
    for base, files in (("docs/site", pages), ("docs/examples", examples)):
        (tmp_path / base).mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            target = tmp_path / base / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(text.encode("utf-8"))
    return tmp_path


SQL = "SELECT 1 AS id\n"
SYNCED = "<!-- source: docs/examples/a/x.sql -->\n```sql\nSELECT 1 AS id\n```\n"


def test_a_block_equal_to_its_source_file_passes(tmp_path: Path) -> None:
    """The synced case."""
    root = _tree(tmp_path, {"p.md": SYNCED}, {"a/x.sql": SQL})
    assert check(root, root / "docs/site") == ([], 0)


@pytest.mark.parametrize(
    "body", ["SELECT 2 AS id\n", "SELECT 1 AS id \n", "SELECT 1 AS id\n\n", ""]
)
def test_any_difference_from_the_source_is_a_problem(tmp_path: Path, body: str) -> None:
    """Different text, trailing space, an extra line, an empty block."""
    page = f"<!-- source: docs/examples/a/x.sql -->\n```sql\n{body}```\n"
    root = _tree(tmp_path, {"p.md": page}, {"a/x.sql": SQL})
    (problem,), _ = check(root, root / "docs/site")
    assert "docs/site/p.md:2" in problem
    assert "differs from docs/examples/a/x.sql" in problem
    assert "--fix" in problem


def test_the_message_shows_the_first_differing_line(tmp_path: Path) -> None:
    """A trailing space is visible in the message."""
    page = "<!-- source: docs/examples/a/x.sql -->\n```sql\nSELECT 1 AS id \n```\n"
    root = _tree(tmp_path, {"p.md": page}, {"a/x.sql": SQL})
    (problem,), _ = check(root, root / "docs/site")
    assert "line 1" in problem
    assert "'SELECT 1 AS id \\n'" in problem
    assert "'SELECT 1 AS id\\n'" in problem


def test_a_missing_line_is_named_in_the_message(tmp_path: Path) -> None:
    """The shorter side shows as nothing."""
    page = "<!-- source: docs/examples/a/x.sql -->\n```sql\nSELECT 1 AS id\nextra\n```\n"
    root = _tree(tmp_path, {"p.md": page}, {"a/x.sql": SQL})
    (problem,), _ = check(root, root / "docs/site")
    assert "line 2" in problem
    assert "<nothing>" in problem


def test_a_source_file_without_a_final_newline_still_matches(tmp_path: Path) -> None:
    """A block always ends in a newline, so the file is compared as if it did."""
    root = _tree(tmp_path, {"p.md": SYNCED}, {"a/x.sql": "SELECT 1 AS id"})
    assert check(root, root / "docs/site") == ([], 0)


def test_a_source_file_with_windows_line_endings_matches(tmp_path: Path) -> None:
    """Line endings of the file are not content."""
    root = _tree(tmp_path, {"p.md": SYNCED}, {"a/x.sql": "SELECT 1 AS id\r\n"})
    assert check(root, root / "docs/site") == ([], 0)


def test_a_region_is_the_lines_between_its_delimiters(tmp_path: Path) -> None:
    """Delimiter lines are not shown; other regions do not leak in."""
    source = (
        "set -e\n# docs:start init\nlakana init demo\n# docs:end init\ntest -d demo\n"
        "# docs:start run\nlakana run\n# docs:end run\n"
    )
    root = _tree(tmp_path, {}, {"a/run.sh": source})
    assert read_source(root, "docs/examples/a/run.sh#init") == "lakana init demo\n"
    assert read_source(root, "docs/examples/a/run.sh#run") == "lakana run\n"


def test_a_region_works_with_any_comment_syntax(tmp_path: Path) -> None:
    """SQL, HTML and C comments carry the same delimiters."""
    source = "-- docs:start q\nSELECT 1\n-- docs:end q\n"
    source += "<!-- docs:start h -->\n<p>\n<!-- docs:end h -->\n"
    source += "/* docs:start c */\nint x;\n/* docs:end c */\n"
    root = _tree(tmp_path, {}, {"a/x.sql": source})
    assert read_source(root, "docs/examples/a/x.sql#q") == "SELECT 1\n"
    assert read_source(root, "docs/examples/a/x.sql#h") == "<p>\n"
    assert read_source(root, "docs/examples/a/x.sql#c") == "int x;\n"


def test_a_region_of_a_file_with_windows_line_endings(tmp_path: Path) -> None:
    """Delimiters are found with CRLF endings too."""
    source = "# docs:start a\r\nx\r\n# docs:end a\r\n"
    root = _tree(tmp_path, {}, {"a/x.sh": source})
    assert read_source(root, "docs/examples/a/x.sh#a") == "x\n"


def test_a_region_nested_in_another_hides_only_the_inner_delimiters(tmp_path: Path) -> None:
    """Different names may nest or overlap; delimiter lines are never shown."""
    source = "# docs:start outer\na\n# docs:start inner\nb\n# docs:end inner\nc\n# docs:end outer\n"
    root = _tree(tmp_path, {}, {"a/x.sh": source})
    assert read_source(root, "docs/examples/a/x.sh#outer") == "a\nb\nc\n"
    assert read_source(root, "docs/examples/a/x.sh#inner") == "b\n"


@pytest.mark.parametrize(
    ("source", "text"),
    [
        ("# docs:start a\nx\n# docs:start a\ny\n# docs:end a\n", "starts twice"),
        ("# docs:start a\nx\n# docs:end a\n# docs:start a\ny\n# docs:end a\n", "starts twice"),
        ("x\n# docs:end a\n", "ends before it starts"),
        ("# docs:start a\nx\n", "never ends"),
        ("# docs:start a\nx\n# docs:end a\n# docs:end a\n", "ends twice"),
    ],
)
def test_a_region_with_unbalanced_delimiters_is_an_error(
    tmp_path: Path, source: str, text: str
) -> None:
    """Opened twice, ended first, never ended, ended twice."""
    root = _tree(tmp_path, {}, {"a/x.sh": source})
    with pytest.raises(BlockError, match=text) as excinfo:
        read_source(root, "docs/examples/a/x.sh#a")
    assert "docs/examples/a/x.sh" in str(excinfo.value)


def test_an_empty_region_name_is_an_error(tmp_path: Path) -> None:
    """A trailing `#` names nothing."""
    root = _tree(tmp_path, {}, {"a/x.sql": SQL})
    with pytest.raises(BlockError, match="names no region"):
        read_source(root, "docs/examples/a/x.sql#")


@pytest.mark.parametrize(
    ("value", "text"),
    [
        ("docs/examples/a/missing.sql", "does not exist"),
        ("docs/examples/a", "is a directory"),
        ("docs/examples/a/x.sql#nope", "has no region"),
        ("docs/examples/../../etc/passwd", "must be under docs/examples"),
        ("docs/examples/a/../x.sql", "must be under docs/examples"),
        ("/etc/passwd", "must be under docs/examples"),
        ("docs/site/p.md", "must be under docs/examples"),
        ("docs/examples", "must be under docs/examples"),
        ("docs/examplesx/a.sql", "must be under docs/examples"),
        ("", "must be under docs/examples"),
    ],
)
def test_a_source_that_cannot_be_read_is_an_error(tmp_path: Path, value: str, text: str) -> None:
    """Missing file, missing region, and every path outside the examples."""
    root = _tree(tmp_path, {"p.md": "x\n"}, {"a/x.sql": SQL})
    with pytest.raises(BlockError, match=text):
        read_source(root, value)


def test_a_source_reached_through_a_symbolic_link_is_refused(tmp_path: Path) -> None:
    """A link under the examples must not pull in a file from elsewhere."""
    root = _tree(tmp_path, {}, {"a/x.sql": SQL})
    (root / "secret.txt").write_text("s\n", encoding="utf-8")
    (root / "docs/examples/a/link.sql").symlink_to(root / "secret.txt")
    with pytest.raises(BlockError, match="symbolic link"):
        read_source(root, "docs/examples/a/link.sql")


def test_a_source_in_a_directory_reached_through_a_link_is_refused(tmp_path: Path) -> None:
    """Every directory on the way counts, not just the file."""
    root = _tree(tmp_path, {}, {})
    (root / "outside").mkdir()
    (root / "outside/x.sql").write_text(SQL, encoding="utf-8")
    (root / "docs/examples/b").symlink_to(root / "outside")
    with pytest.raises(BlockError, match="symbolic link"):
        read_source(root, "docs/examples/b/x.sql")


def test_an_examples_directory_that_is_a_link_is_refused(tmp_path: Path) -> None:
    """The examples directory itself may not be a link."""
    root = _tree(tmp_path, {}, {})
    (root / "docs/examples").rmdir()
    (root / "outside").mkdir()
    (root / "outside/x.sql").write_text(SQL, encoding="utf-8")
    (root / "docs/examples").symlink_to(root / "outside")
    with pytest.raises(BlockError, match="symbolic link"):
        read_source(root, "docs/examples/x.sql")


def test_an_unmarked_block_in_a_parsed_language_is_syntax_checked(tmp_path: Path) -> None:
    """Valid passes; invalid is a problem naming the parser's message."""
    root = _tree(tmp_path, {"ok.md": "```toml\na = 1\n```\n", "bad.md": "```toml\na = \n```\n"}, {})
    problems, _ = check(root, root / "docs/site")
    assert len(problems) == 1
    assert "docs/site/bad.md:1" in problems[0]
    assert "toml" in problems[0]


@pytest.mark.parametrize("lang", ["console", "sql", "bash", "text", ""])
def test_an_unmarked_block_in_any_other_language_is_a_problem(tmp_path: Path, lang: str) -> None:
    """The guard: no fourth kind of block."""
    root = _tree(tmp_path, {"p.md": f"```{lang}\nx\n```\n"}, {})
    (problem,), _ = check(root, root / "docs/site")
    assert "docs/site/p.md:1" in problem
    for way in ("<!-- source:", "<!-- not tested:", "<!-- output -->"):
        assert way in problem


def test_marked_blocks_pass_and_not_tested_ones_are_counted(tmp_path: Path) -> None:
    """Output and not-tested blocks are accepted; the second kind is counted."""
    page = (
        "<!-- output -->\n```text\n1 row\n```\n"
        "<!-- not tested: needs a cloud account -->\n```console\naws s3 ls\n```\n"
        "<!-- not tested: moved as is -->\n```sql\nSELECT\n```\n"
    )
    root = _tree(tmp_path, {"p.md": page}, {})
    assert check(root, root / "docs/site") == ([], 2)


def test_a_source_error_is_reported_with_the_page_and_line(tmp_path: Path) -> None:
    """A synced block whose file is missing."""
    page = "<!-- source: docs/examples/a/nope.sql -->\n```sql\nx\n```\n"
    root = _tree(tmp_path, {"p.md": page}, {})
    (problem,), _ = check(root, root / "docs/site")
    assert problem.startswith("docs/site/p.md:2: ")
    assert "does not exist" in problem


def test_pages_in_subdirectories_are_checked(tmp_path: Path) -> None:
    """The walk is recursive."""
    root = _tree(tmp_path, {"a/b/p.md": "```console\nx\n```\n"}, {})
    (problem,), _ = check(root, root / "docs/site")
    assert "docs/site/a/b/p.md:1" in problem


def test_a_page_that_is_a_symbolic_link_is_a_problem_and_never_written(
    tmp_path: Path,
) -> None:
    """The target outside the site is neither read nor changed."""
    root = _tree(tmp_path, {}, {"a/x.sql": SQL})
    outside = root / "outside.md"
    text = "<!-- source: docs/examples/a/x.sql -->\n```sql\nold\n```\n"
    outside.write_text(text, encoding="utf-8")
    (root / "docs/site/link.md").symlink_to(outside)
    (problem,), _ = check(root, root / "docs/site")
    assert "docs/site/link.md" in problem
    assert "symbolic link" in problem
    assert fix(root, root / "docs/site") == []
    assert outside.read_text(encoding="utf-8") == text


def test_a_directory_in_the_site_that_is_a_link_is_a_problem_and_never_written(
    tmp_path: Path,
) -> None:
    """Pages behind a linked directory are neither followed nor changed."""
    root = _tree(tmp_path, {}, {"a/x.sql": SQL})
    (root / "outside").mkdir()
    text = "<!-- source: docs/examples/a/x.sql -->\n```sql\nold\n```\n"
    (root / "outside/p.md").write_text(text, encoding="utf-8")
    (root / "docs/site/linked").symlink_to(root / "outside")
    (problem,), _ = check(root, root / "docs/site")
    assert "docs/site/linked" in problem
    assert "symbolic link" in problem
    assert fix(root, root / "docs/site") == []
    assert (root / "outside/p.md").read_text(encoding="utf-8") == text


def test_a_site_directory_that_is_a_link_is_refused_by_check_and_fix(tmp_path: Path) -> None:
    """`docs/site` itself may not be a link out of the repository."""
    root = _tree(tmp_path, {}, {"a/x.sql": SQL})
    (root / "docs/site").rmdir()
    (root / "outside").mkdir()
    text = "<!-- source: docs/examples/a/x.sql -->\n```sql\nold\n```\n"
    (root / "outside/p.md").write_text(text, encoding="utf-8")
    (root / "docs/site").symlink_to(root / "outside")
    (problem,), _ = check(root, root / "docs/site")
    assert "docs/site" in problem
    assert "symbolic link" in problem
    assert fix(root, root / "docs/site") == []
    assert (root / "outside/p.md").read_text(encoding="utf-8") == text


def test_a_site_outside_the_root_is_refused(tmp_path: Path) -> None:
    """`fix` writes only below the root's site."""
    root = _tree(tmp_path / "repo", {}, {})
    other = tmp_path / "other"
    other.mkdir()
    text = "<!-- source: docs/examples/a/x.sql -->\n```sql\nold\n```\n"
    (other / "p.md").write_text(text, encoding="utf-8")
    assert fix(root, other) == []
    assert (other / "p.md").read_text(encoding="utf-8") == text
    (problem,), _ = check(root, other)
    assert "not inside" in problem


def test_a_root_without_a_site_is_a_problem_not_a_pass(tmp_path: Path) -> None:
    """A mistyped `--root` must not look like a clean run."""
    (problem,), _ = check(tmp_path, tmp_path / "docs/site")
    assert "docs/site" in problem
    assert "--root" in problem
    assert fix(tmp_path, tmp_path / "docs/site") == []


def test_fix_rewrites_a_differing_block_and_nothing_else(tmp_path: Path) -> None:
    """The block takes the file's content; the rest of the page is untouched."""
    page = (
        "Intro\n\n<!-- source: docs/examples/a/x.sql -->\n```sql\nSELECT 2\n```\n\n"
        "Outro\n```toml\na = 1\n```\n"
    )
    root = _tree(tmp_path, {"p.md": page}, {"a/x.sql": SQL})
    assert fix(root, root / "docs/site") == [root / "docs/site/p.md"]
    assert (root / "docs/site/p.md").read_text(encoding="utf-8") == page.replace("SELECT 2\n", SQL)
    assert fix(root, root / "docs/site") == []


def test_fix_with_two_synced_blocks_on_a_page_fixes_both(tmp_path: Path) -> None:
    """Fixing the first shifts the lines of the second."""
    block = "<!-- source: docs/examples/a/{}.sql -->\n```sql\n{}```\n"
    page = block.format("one", "old\n") + "\n" + block.format("two", "old\nold\nold\n")
    root = _tree(tmp_path, {"p.md": page}, {"a/one.sql": "a\nb\nc\nd\n", "a/two.sql": "z\n"})
    fix(root, root / "docs/site")
    assert (root / "docs/site/p.md").read_text(encoding="utf-8") == (
        block.format("one", "a\nb\nc\nd\n") + "\n" + block.format("two", "z\n")
    )
    assert check(root, root / "docs/site") == ([], 0)


def test_fix_keeps_windows_line_endings_of_the_page(tmp_path: Path) -> None:
    """A page with CRLF stays CRLF, including the lines it inserts."""
    page = "Intro\r\n<!-- source: docs/examples/a/x.sql -->\r\n```sql\r\nold\r\n```\r\nEnd\r\n"
    root = _tree(tmp_path, {"p.md": page}, {"a/x.sql": "a\nb\n"})
    fix(root, root / "docs/site")
    assert (root / "docs/site/p.md").read_bytes() == page.replace("old\r\n", "a\r\nb\r\n").encode()
    assert check(root, root / "docs/site") == ([], 0)


def test_fix_copies_a_source_file_without_a_final_newline(tmp_path: Path) -> None:
    """The block gets the missing newline, and then checks clean."""
    root = _tree(tmp_path, {"p.md": SYNCED.replace("SELECT 1 AS id", "old")}, {"a/x.sql": "q"})
    fix(root, root / "docs/site")
    assert (root / "docs/site/p.md").read_text(encoding="utf-8").endswith("```sql\nq\n```\n")
    assert check(root, root / "docs/site") == ([], 0)


def test_fix_empties_a_block_whose_source_is_empty(tmp_path: Path) -> None:
    """An empty file gives an empty block."""
    root = _tree(tmp_path, {"p.md": SYNCED}, {"a/x.sql": ""})
    fix(root, root / "docs/site")
    assert (root / "docs/site/p.md").read_text(encoding="utf-8") == (
        "<!-- source: docs/examples/a/x.sql -->\n```sql\n```\n"
    )
    assert check(root, root / "docs/site") == ([], 0)


def test_fix_leaves_a_block_with_an_unreadable_source_and_a_broken_page(
    tmp_path: Path,
) -> None:
    """`fix` skips what it cannot repair; `check` reports it."""
    bad_source = "<!-- source: docs/examples/a/nope.sql -->\n```sql\nold\n```\n"
    broken = "```python\nx\n"
    root = _tree(tmp_path, {"a.md": bad_source, "b.md": broken}, {})
    assert fix(root, root / "docs/site") == []
    assert (root / "docs/site/a.md").read_text(encoding="utf-8") == bad_source
    assert (root / "docs/site/b.md").read_text(encoding="utf-8") == broken


def test_fix_leaves_blocks_with_other_markers_alone(tmp_path: Path) -> None:
    """Only synced blocks are rewritten."""
    page = "<!-- output -->\n```text\nx\n```\n```toml\na = 1\n```\n"
    root = _tree(tmp_path, {"p.md": page}, {})
    assert fix(root, root / "docs/site") == []


def test_fix_keeps_the_indent_of_a_block_in_a_list(tmp_path: Path) -> None:
    """A synced block inside a list item stays inside it."""
    page = "1. Do\n\n    <!-- source: docs/examples/a/x.sql -->\n    ```sql\n    old\n    ```\n"
    root = _tree(tmp_path, {"p.md": page}, {"a/x.sql": "SELECT 1\nFROM t\n"})
    fix(root, root / "docs/site")
    assert (root / "docs/site/p.md").read_text(encoding="utf-8") == page.replace(
        "    old\n", "    SELECT 1\n    FROM t\n"
    )


def test_a_block_in_a_list_whose_source_has_blank_lines_round_trips(tmp_path: Path) -> None:
    """An empty line stays empty; a line of spaces keeps them; check passes after."""
    page = "1. Do\n\n    <!-- source: docs/examples/a/x.sql -->\n    ```sql\n    old\n    ```\n"
    source = "SELECT 1\n\n  \n    FROM t\n"
    root = _tree(tmp_path, {"p.md": page}, {"a/x.sql": source})
    fix(root, root / "docs/site")
    text = (root / "docs/site/p.md").read_text(encoding="utf-8")
    assert "    SELECT 1\n\n      \n        FROM t\n" in text
    assert check(root, root / "docs/site") == ([], 0)


def test_main_reports_problems_and_the_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit 1 with the problems; exit 0 with the count of not-tested blocks."""
    root = _tree(tmp_path, {"p.md": "```console\nx\n```\n"}, {})
    assert main(["--root", str(root)]) == 1
    assert "docs/site/p.md:1" in capsys.readouterr().out
    (root / "docs/site/p.md").write_text(
        "<!-- not tested: because -->\n```console\nx\n```\n", encoding="utf-8"
    )
    assert main(["--root", str(root)]) == 0
    assert (
        "1 block marked as not tested, 0 blocks marked as output; 0 problems"
        in capsys.readouterr().out
    )


def test_main_counts_in_the_plural(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Zero and two blocks read as plurals."""
    root = _tree(tmp_path, {}, {})
    assert main(["--root", str(root)]) == 0
    assert (
        "0 blocks marked as not tested, 0 blocks marked as output; 0 problems"
        in capsys.readouterr().out
    )


def test_main_says_how_many_problems_there_are(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """One problem is singular."""
    root = _tree(tmp_path, {"p.md": "```console\nx\n```\n"}, {})
    main(["--root", str(root)])
    assert "1 problem." in capsys.readouterr().out


def test_main_fix_writes_and_then_checks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--fix` repairs a synced block and reports the page."""
    page = "<!-- source: docs/examples/a/x.sql -->\n```sql\nold\n```\n"
    root = _tree(tmp_path, {"p.md": page}, {"a/x.sql": SQL})
    assert main(["--root", str(root), "--fix"]) == 0
    assert "updated docs/site/p.md" in capsys.readouterr().out
    assert main(["--root", str(root)]) == 0


def test_main_with_a_root_that_has_no_site_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A wrong `--root` is exit 1 with the way out."""
    assert main(["--root", str(tmp_path), "--fix"]) == 1
    assert "--root" in capsys.readouterr().out


def test_a_broken_page_is_reported_not_raised(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unclosed fence is one more problem in the list."""
    root = _tree(tmp_path, {"p.md": "```python\nx\n"}, {})
    assert main(["--root", str(root)]) == 1
    out = capsys.readouterr().out
    assert "never closed" in out
    assert "docs/site/p.md:1" in out


def test_a_source_that_is_not_text_is_an_error(tmp_path: Path) -> None:
    """A binary file is named, not a traceback."""
    root = _tree(tmp_path, {}, {})
    (root / "docs/examples/b.bin").write_bytes(b"\xff\xfe\x00")
    with pytest.raises(BlockError, match="UTF-8"):
        read_source(root, "docs/examples/b.bin")


def test_a_page_that_is_not_text_is_a_problem_and_left_alone(tmp_path: Path) -> None:
    """A page that is not UTF-8 is reported by `check` and skipped by `fix`."""
    root = _tree(tmp_path, {}, {})
    (root / "docs/site/p.md").write_bytes(b"\xff\xfe\x00")
    (problem,), _ = check(root, root / "docs/site")
    assert "docs/site/p.md" in problem
    assert "UTF-8" in problem
    assert fix(root, root / "docs/site") == []


def test_files_that_are_not_pages_are_ignored(tmp_path: Path) -> None:
    """Images and other assets in the site are not read."""
    root = _tree(tmp_path, {"img/logo.svg": "```console\nx\n```\n"}, {})
    assert check(root, root / "docs/site") == ([], 0)


def test_the_script_runs_as_a_program(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Run as `python scripts/docs_blocks.py`, the exit code is main's."""
    root = _tree(tmp_path, {"p.md": "```console\nx\n```\n"}, {})
    monkeypatch.setattr("sys.argv", ["docs_blocks.py", "--root", str(root)])
    with pytest.raises(SystemExit) as excinfo:
        runpy.run_path(str(Path(docs_blocks.__file__)), run_name="__main__")
    assert excinfo.value.code == 1
    assert "docs/site/p.md:1" in capsys.readouterr().out


NOT_ROOT = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores file permissions"
)


def _marker_page(marker: str, body: str = "zzz = 1\n") -> str:
    """Build a page with one python block under a marker line.

    Parameters
    ----------
    marker
        The line above the fence.
    body
        The block's content.

    Returns
    -------
    str
        The page.
    """
    return f"{marker}\n```python\n{body}```\n"


@pytest.mark.parametrize(
    "marker",
    [
        "<!-- SOURCE: docs/examples/e/a.py -->",
        "<!-- sourse: docs/examples/e/a.py -->",
        "<!-- Not tested: x -->",
        "<!-- not-tested: x -->",
        "<!-- Output -->",
        "<!--   NOT   TESTED   -->",
        "<!-- sorce: docs/examples/e/a.py -->",
        "<!-- outpt -->",
    ],
)
def test_a_misspelled_marker_is_an_error_not_an_unmarked_block(tmp_path: Path, marker: str) -> None:
    """The block must not slip through as an unmarked, parsed one."""
    root = _tree(tmp_path, {"p.md": _marker_page(marker)}, {"e/a.py": "a = 1\n"})
    (problem,), _ = check(root, root / "docs/site")
    assert problem.startswith("docs/site/p.md:1: ")
    assert "looks like a marker" in problem
    for exact in (
        "<!-- source: docs/examples/<file> -->",
        "<!-- not tested: <reason> -->",
        "<!-- output -->",
    ):
        assert exact in problem
    assert "not checked" in problem


@pytest.mark.parametrize(
    "comment",
    [
        "<!-- TODO: explain this -->",
        "<!-- the output is below -->",
        "<!-- -->",
        "<!-- Sources: see the changelog -->",
        "<!-- sources -->",
        "<!-- outputs: differ on Windows -->",
        "<!-- sure -->",
        "<!-- sauce -->",
    ],
)
def test_an_ordinary_comment_above_a_block_is_left_alone(tmp_path: Path, comment: str) -> None:
    """Only a near miss of a marker is an error."""
    root = _tree(tmp_path, {"p.md": _marker_page(comment, "x = 1\n")}, {})
    assert check(root, root / "docs/site") == ([], 0)


def test_two_markers_above_one_block_are_an_error(tmp_path: Path) -> None:
    """A synced block must not lose its marker to a second one."""
    page = (
        "<!-- source: docs/examples/e/a.py -->\n<!-- not tested: why -->\n```python\nzzz = 1\n```\n"
    )
    root = _tree(tmp_path, {"p.md": page}, {"e/a.py": "a = 1\n"})
    (problem,), _ = check(root, root / "docs/site")
    assert problem.startswith("docs/site/p.md:2: ")
    assert "two markers" in problem


@pytest.mark.parametrize("char", ["\x0c", "\u2028", "\u2029", "\x85", "\x1c"])
def test_fix_converges_on_a_source_with_a_line_separator_character(
    tmp_path: Path, char: str
) -> None:
    """Only a line feed ends a line; the other separators are content."""
    outside = f"Text{char}more\n\n"
    page = outside + "<!-- source: docs/examples/a/x.txt -->\n```text\nold\n```\n"
    source = f"one{char}two\nthree\n"
    root = _tree(tmp_path, {"p.md": page}, {"a/x.txt": source})
    assert fix(root, root / "docs/site") == [root / "docs/site/p.md"]
    expected = outside + f"<!-- source: docs/examples/a/x.txt -->\n```text\n{source}```\n"
    assert (root / "docs/site/p.md").read_bytes() == expected.encode()
    assert check(root, root / "docs/site") == ([], 0)
    assert fix(root, root / "docs/site") == []


def test_a_page_with_separator_characters_outside_blocks_is_untouched(tmp_path: Path) -> None:
    """`fix` rewrites nothing when no block differs."""
    page = "a\x0cb\u2028c\n" + SYNCED
    root = _tree(tmp_path, {"p.md": page}, {"a/x.sql": SQL})
    assert fix(root, root / "docs/site") == []
    assert (root / "docs/site/p.md").read_bytes() == page.encode()


def test_the_message_for_a_separator_in_a_line_shows_the_whole_line(tmp_path: Path) -> None:
    """The first differing line is a line feed line."""
    page = "<!-- source: docs/examples/a/x.txt -->\n```text\na\x0cb\n```\n"
    root = _tree(tmp_path, {"p.md": page}, {"a/x.txt": "a\x0cc\n"})
    (problem,), _ = check(root, root / "docs/site")
    assert "line 1" in problem


@NOT_ROOT
def test_an_unreadable_page_is_a_problem_not_a_traceback(tmp_path: Path) -> None:
    """Permissions on a page."""
    root = _tree(tmp_path, {"p.md": "x\n"}, {})
    (root / "docs/site/p.md").chmod(0)
    try:
        (problem,), _ = check(root, root / "docs/site")
        assert fix(root, root / "docs/site") == []
    finally:
        (root / "docs/site/p.md").chmod(0o644)
    assert problem.startswith("docs/site/p.md: cannot read it")
    assert "permissions" in problem


@NOT_ROOT
def test_an_unreadable_source_is_a_problem_not_a_traceback(tmp_path: Path) -> None:
    """Permissions on a source file."""
    root = _tree(tmp_path, {"p.md": SYNCED}, {"a/x.sql": SQL})
    (root / "docs/examples/a/x.sql").chmod(0)
    try:
        (problem,), _ = check(root, root / "docs/site")
        assert fix(root, root / "docs/site") == []
    finally:
        (root / "docs/examples/a/x.sql").chmod(0o644)
    assert "docs/site/p.md:2" in problem
    assert "cannot be read" in problem


@NOT_ROOT
def test_fix_reports_a_page_it_cannot_write(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A read-only page is named, and the exit code is 1."""
    page = "<!-- source: docs/examples/a/x.sql -->\n```sql\nold\n```\n"
    root = _tree(tmp_path, {"p.md": page}, {"a/x.sql": SQL})
    (root / "docs/site/p.md").chmod(0o444)
    try:
        assert fix(root, root / "docs/site") == []
        assert main(["--root", str(root), "--fix"]) == 1
    finally:
        (root / "docs/site/p.md").chmod(0o644)
    out = capsys.readouterr().out
    assert "docs/site/p.md: cannot write it" in out
    assert "writable" in out
    assert (root / "docs/site/p.md").read_text(encoding="utf-8") == page


def test_a_source_name_that_is_too_long_is_a_problem(tmp_path: Path) -> None:
    """The file system's own limit does not leak out as an exception."""
    page = f"<!-- source: docs/examples/{'a' * 300}.sql -->\n```sql\nx\n```\n"
    root = _tree(tmp_path, {"p.md": page}, {})
    (problem,), _ = check(root, root / "docs/site")
    assert "docs/site/p.md:2" in problem
    assert "longer than 255 bytes" in problem


def test_a_byte_order_mark_does_not_hide_the_first_fence(tmp_path: Path) -> None:
    """A page saved with a BOM starts with a fence."""
    page = _page(tmp_path, "\ufeff```toml\na = 1\n```\n")
    (block,) = find_blocks(page)
    assert (block.line, block.lang) == (1, "toml")


def test_fix_keeps_the_byte_order_mark_of_a_page_and_reads_one_in_a_source(
    tmp_path: Path,
) -> None:
    """Both ends may carry a BOM."""
    page = "\ufeff<!-- source: docs/examples/a/x.sql -->\n```sql\nold\n```\n"
    root = _tree(tmp_path, {"p.md": page}, {"a/x.sql": "\ufeff" + SQL})
    fix(root, root / "docs/site")
    assert (root / "docs/site/p.md").read_bytes() == ("\ufeff" + SYNCED).encode()
    assert check(root, root / "docs/site") == ([], 0)


def test_a_fence_in_a_blockquote_is_reported(tmp_path: Path) -> None:
    """The reader sees a code block the check cannot judge."""
    root = _tree(tmp_path, {"p.md": "Text\n\n> ```console\n> x\n> ```\n"}, {})
    (problem,), _ = check(root, root / "docs/site")
    assert problem.startswith("docs/site/p.md:3: ")
    assert "start of a line" in problem


@pytest.mark.parametrize("tag", ["<pre>", "<pre class='x'>", "  <PRE>"])
def test_a_pre_element_is_reported(tmp_path: Path, tag: str) -> None:
    """HTML code blocks cannot be judged either."""
    root = _tree(tmp_path, {"p.md": f"{tag}\nx\n</pre>\n"}, {})
    (problem,), _ = check(root, root / "docs/site")
    assert problem.startswith("docs/site/p.md:1: ")
    assert "start of a line" in problem


def test_delimiter_lines_are_never_shown_for_the_whole_file(tmp_path: Path) -> None:
    """Whole file and region follow one rule."""
    source = "a\n# docs:start r\nb\n# docs:end r\nc\n"
    root = _tree(tmp_path, {}, {"a/x.sh": source})
    assert read_source(root, "docs/examples/a/x.sh") == "a\nb\nc\n"


def test_a_page_error_says_later_blocks_were_not_checked(tmp_path: Path) -> None:
    """An unclosed fence stops the page; the message says so."""
    root = _tree(tmp_path, {"p.md": "```python\nx\n"}, {})
    (problem,), _ = check(root, root / "docs/site")
    assert "not checked" in problem


def test_a_page_with_windows_line_endings_equals_a_source_with_unix_ones(
    tmp_path: Path,
) -> None:
    """Line endings are not part of the comparison."""
    root = _tree(tmp_path, {"p.md": SYNCED.replace("\n", "\r\n")}, {"a/x.sql": SQL})
    assert check(root, root / "docs/site") == ([], 0)


def test_fix_keeps_a_page_without_a_final_newline(tmp_path: Path) -> None:
    """The last line stays as it was."""
    page = "<!-- source: docs/examples/a/x.sql -->\n```sql\nold\n```"
    root = _tree(tmp_path, {"p.md": page}, {"a/x.sql": SQL})
    fix(root, root / "docs/site")
    assert (root / "docs/site/p.md").read_text(encoding="utf-8") == SYNCED.rstrip("\n")


@pytest.mark.parametrize("source", [b"a = 1\r", b"x\r\r\ny\n", b"a\rb\r", b"a = 1\r\nb = 2\r\n"])
def test_a_source_with_carriage_returns_at_line_ends_converges(
    tmp_path: Path, source: bytes
) -> None:
    """A carriage return before a line end is not compared, in a source as in a page."""
    page = "<!-- source: docs/examples/e/a.txt -->\n```text\nold\n```\n"
    root = _tree(tmp_path, {"p.md": page}, {})
    (root / "docs/examples/e").mkdir(parents=True)
    (root / "docs/examples/e/a.txt").write_bytes(source)
    assert fix(root, root / "docs/site") == [root / "docs/site/p.md"]
    assert check(root, root / "docs/site") == ([], 0)
    assert fix(root, root / "docs/site") == []


def test_an_output_marker_is_for_text_blocks_only(tmp_path: Path) -> None:
    """A command in a `bash` block is not output; the marker may not hide it."""
    page = "<!-- output -->\n```bash\nrm -rf x\n```\n"
    root = _tree(tmp_path, {"p.md": page}, {})
    (problem,), _ = check(root, root / "docs/site")
    assert "docs/site/p.md:2" in problem
    assert "`bash`" in problem
    assert "`text`" in problem


def test_output_blocks_are_counted_and_printed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Text, console and unlabelled blocks are output; the summary line counts them."""
    page = "".join(f"<!-- output -->\n```{lang}\nx\n```\n" for lang in ("text", "console", ""))
    root = _tree(tmp_path, {"p.md": page}, {})
    counts: dict[str, int] = {}
    assert check(root, root / "docs/site", counts) == ([], 0)
    assert counts == {"output": 3, "unchecked": 3, "compared": 0, "skipped": 0}
    assert main(["--root", str(root)]) == 0
    assert "0 blocks marked as not tested, 3 blocks marked as output; 0 problems" in (
        capsys.readouterr().out
    )


def test_main_counts_one_output_block_in_the_singular(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """One block reads as singular."""
    root = _tree(tmp_path, {"p.md": "<!-- output -->\n```text\na\n```\n"}, {})
    assert main(["--root", str(root)]) == 0
    assert "1 block marked as output" in capsys.readouterr().out


def test_a_refused_site_still_reports_an_output_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing site directory is a problem, not a crash."""
    assert main(["--root", str(tmp_path)]) == 1
    assert "0 blocks marked as output" in capsys.readouterr().out


@pytest.mark.parametrize("gap", ["\n\n", "\n\n\n"])
def test_a_marker_separated_from_its_block_by_blank_lines_is_an_error(
    tmp_path: Path, gap: str
) -> None:
    """The marker only counts directly above the fence; a gap would silently skip the sync."""
    page = f"<!-- source: docs/examples/a/x.py -->{gap}```python\nx = 1\n```\n"
    root = _tree(tmp_path, {"p.md": page}, {"a/x.py": "x = 1\n"})
    (problem,), _ = check(root, root / "docs/site")
    assert "docs/site/p.md:1" in problem
    assert "directly above" in problem
