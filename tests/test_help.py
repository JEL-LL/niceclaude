"""`niceclaude help <command>`, and the pages it prints.

argparse gives every subcommand a --help for free, but a page that lists bare
flag names with no explanation is not help, and nothing in argparse insists on
more -- `niceclaude on --help` shipped exactly that for a long time. These
tests insist: every command has a description, every argument says what it is
for, and `help <command>` prints the same page as `<command> --help`, so the
two ways in cannot drift apart.
"""

import pytest

from niceclaude import cli


def run(capsys, *argv):
    """Run the CLI and return (exit code, stdout, stderr).

    argparse's own --help exits via SystemExit rather than returning, so both
    shapes are normalized to a code here.
    """
    try:
        code = cli.main(list(argv))
    except SystemExit as exc:
        code = exc.code
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def command_names():
    _ap, sub = cli.build_parser()
    return list(sub.choices)


def pages():
    _ap, sub = cli.build_parser()
    return sorted(sub.choices.items())


# --- the help command --------------------------------------------------------

def test_help_for_one_command_prints_its_page(capsys):
    code, out, err = run(capsys, "help", "on")
    assert (code, err) == (0, "")
    assert out.startswith("usage: niceclaude on ")
    assert "--max-delay" in out
    # The description, not just the flag list.
    assert "longest" in out and "matching rule wins" in out
    assert "examples:" in out


@pytest.mark.parametrize("name", command_names())
def test_help_command_and_dash_dash_help_print_the_same_page(capsys, name):
    via_help = run(capsys, "help", name)
    via_flag = run(capsys, name, "--help")
    assert via_flag[0] == 0
    assert via_help == (0, via_flag[1], "")


def test_bare_help_prints_the_overview(capsys):
    code, out, err = run(capsys, "help")
    assert (code, err) == (0, "")
    assert out.startswith("usage: niceclaude ")
    for name in command_names():
        assert f"  {name} " in out, f"{name} missing from the overview"


def test_bare_help_matches_top_level_dash_dash_help(capsys):
    assert run(capsys, "help")[1] == run(capsys, "--help")[1]


def test_help_for_an_unknown_command_is_an_error(capsys):
    code, out, err = run(capsys, "help", "bogus")
    assert code == 2
    assert out == ""
    assert "bogus" in err
    assert "install" in err      # the valid names, so the user can retry


def test_help_lists_itself():
    """`niceclaude help help` is a real page, and the overview mentions the
    command the overview is telling people to use."""
    assert "help" in command_names()


# --- the pages have content --------------------------------------------------

def test_every_command_is_described_exactly_once():
    """COMMAND_HELP and the parser agree on the set of commands. An entry with
    no parser is dead text; a parser with no entry cannot be built."""
    assert set(cli.COMMAND_HELP) == set(command_names())


@pytest.mark.parametrize("name,parser", pages())
def test_every_command_has_a_description(name, parser):
    text = (parser.description or "").strip()
    assert text, f"{name} has no description"
    # One line is the overview's summary repeated, not a page.
    assert len(text.splitlines()) >= 3, f"{name}: description is a one-liner"


@pytest.mark.parametrize("name,parser", pages())
def test_every_argument_says_what_it_is_for(name, parser):
    for action in parser._actions:
        assert action.help, f"{name}: {action.dest} has no help text"


@pytest.mark.parametrize("name,parser", pages())
def test_pages_fit_a_terminal(name, parser):
    """Descriptions and examples are printed verbatim, not re-wrapped, so a
    long line here is a long line on screen."""
    for text in (parser.description, parser.epilog):
        for line in (text or "").splitlines():
            assert len(line) <= 79, f"{name}: {line!r}"
