"""Generic mechanism for marking and selecting python functions."""
import dataclasses
from typing import AbstractSet
from typing import Collection
from typing import List
from typing import Optional
from typing import TYPE_CHECKING
from typing import Union

from .expression import Expression
from .expression import ParseError
from .structures import EMPTY_PARAMETERSET_OPTION
from .structures import get_empty_parameterset_mark
from .structures import Mark
from .structures import MARK_GEN
from .structures import MarkDecorator
from .structures import MarkGenerator
from .structures import ParameterSet
from _pytest.config import Config
from _pytest.config import ExitCode
from _pytest.config import hookimpl
from _pytest.config import UsageError
from _pytest.config.argparsing import Parser
from _pytest.stash import StashKey

if TYPE_CHECKING:
    from _pytest.nodes import Item


__all__ = [
    "MARK_GEN",
    "Mark",
    "MarkDecorator",
    "MarkGenerator",
    "ParameterSet",
    "get_empty_parameterset_mark",
]


old_mark_config_key = StashKey[Optional[Config]]()


def param(
    *values: object,
    marks: Union[MarkDecorator, Collection[Union[MarkDecorator, Mark]]] = (),
    id: Optional[str] = None,
) -> ParameterSet:
    """Specify a parameter in `pytest.mark.parametrize`_ calls or
    :ref:`parametrized fixtures <fixture-parametrize-marks>`.

    .. code-block:: python

        @pytest.mark.parametrize(
            "test_input,expected",
            [
                ("3+5", 8),
                pytest.param("6*9", 42, marks=pytest.mark.xfail),
            ],
        )
        def test_eval(test_input, expected):
            assert eval(test_input) == expected

    :param values: Variable args of the values of the parameter set, in order.
    :param marks: A single mark or a list of marks to be applied to this parameter set.
    :param id: The id to attribute to this parameter set.
    """
    return ParameterSet.param(*values, marks=marks, id=id)


def pytest_addoption(parser: Parser) -> None:
    group = parser.getgroup("general")
    group._addoption(
        "-k",
        action="store",
        dest="keyword",
        default="",
        metavar="EXPRESSION",
        help="Only run tests which match the given substring expression. "
        "An expression is a Python evaluatable expression "
        "where all names are substring-matched against test names "
        "and their parent classes. Example: -k 'test_method or test_"
        "other' matches all test functions and classes whose name "
        "contains 'test_method' or 'test_other', while -k 'not test_method' "
        "matches those that don't contain 'test_method' in their names. "
        "-k 'not test_method and not test_other' will eliminate the matches. "
        "Additionally keywords are matched to classes and functions "
        "containing extra names in their 'extra_keyword_matches' set, "
        "as well as functions which have names assigned directly to them. "
        "The matching is case-insensitive.",
    )

    group._addoption(
        "-m",
        action="store",
        dest="markexpr",
        default="",
        metavar="MARKEXPR",
        help="Only run tests matching given mark expression. "
        "For example: -m 'mark1 and not mark2'.",
    )

    group.addoption(
        "--markers",
        action="store_true",
        help="show markers (builtin, plugin and per-project ones).",
    )

    parser.addini("markers", "Markers for test functions", "linelist")
    parser.addini(EMPTY_PARAMETERSET_OPTION, "Default marker for empty parametersets")


@hookimpl(tryfirst=True)
def pytest_cmdline_main(config: Config) -> Optional[Union[int, ExitCode]]:
    import _pytest.config

    if config.option.markers:
        config._do_configure()
        tw = _pytest.config.create_terminal_writer(config)
        for line in config.getini("markers"):
            parts = line.split(":", 1)
            name = parts[0]
            rest = parts[1] if len(parts) == 2 else ""
            tw.write(f"@pytest.mark.{name}:", bold=True)
            tw.line(rest)
            tw.line()
        config._ensure_unconfigure()
        return 0

    return None


@dataclasses.dataclass
class KeywordMatcher:
    """A matcher for keywords.

    Given a list of names, matches any substring of one of these names. The
    string inclusion check is case-insensitive.

    Will match on the name of colitem, including the names of its parents.
    Only matches names of items which are either a :class:`Class` or a
    :class:`Function`.

    Additionally, matches on names in the 'extra_keyword_matches' set of
    any item, as well as names directly assigned to test functions.
    """

    __slots__ = ("_names",)

    _names: AbstractSet[str]

    @classmethod
    def from_item(cls, item: "Item") -> "KeywordMatcher":
        # Add the names of the current item and any parent items.
        import pytest

        mapped_names = {
            node.name
            for node in item.listchain()
            if not isinstance(node, pytest.Session)
        }
        # Add the names added as extra keywords to current or parent items.
        mapped_names.update(item.listextrakeywords())

        if function_obj := getattr(item, "function", None):
            mapped_names.update(function_obj.__dict__)

        # Add the markers to the keywords as we no longer handle them correctly.
        mapped_names.update(mark.name for mark in item.iter_markers())

        return cls(mapped_names)

    def __call__(self, subname: str) -> bool:
        subname = subname.lower()
        names = (name.lower() for name in self._names)

        return any(subname in name for name in names)


def deselect_by_keyword(items: "List[Item]", config: Config) -> None:
    keywordexpr = config.option.keyword.lstrip()
    if not keywordexpr:
        return

    expr = _parse_expression(keywordexpr, "Wrong expression passed to '-k'")

    remaining = []
    deselected = []
    for colitem in items:
        if not expr.evaluate(KeywordMatcher.from_item(colitem)):
            deselected.append(colitem)
        else:
            remaining.append(colitem)

    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = remaining


@dataclasses.dataclass
class MarkMatcher:
    """A matcher for markers which are present.

    Tries to match on any marker names, attached to the given colitem.
    """

    __slots__ = ("own_mark_names",)

    own_mark_names: AbstractSet[str]

    @classmethod
    def from_item(cls, item: "Item") -> "MarkMatcher":
        mark_names = {mark.name for mark in item.iter_markers()}
        return cls(mark_names)

    def __call__(self, name: str) -> bool:
        return name in self.own_mark_names


def deselect_by_mark(items: "List[Item]", config: Config) -> None:
    matchexpr = config.option.markexpr
    if not matchexpr:
        return

    expr = _parse_expression(matchexpr, "Wrong expression passed to '-m'")
    remaining: List[Item] = []
    deselected: List[Item] = []
    for item in items:
        if expr.evaluate(MarkMatcher.from_item(item)):
            remaining.append(item)
        else:
            deselected.append(item)
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = remaining


def _parse_expression(expr: str, exc_message: str) -> Expression:
    try:
        return Expression.compile(expr)
    except ParseError as e:
        raise UsageError(f"{exc_message}: {expr}: {e}") from None


def pytest_collection_modifyitems(items: "List[Item]", config: Config) -> None:
    deselect_by_keyword(items, config)
    deselect_by_mark(items, config)


def pytest_configure(config: Config) -> None:
    config.stash[old_mark_config_key] = MARK_GEN._config
    MARK_GEN._config = config

    empty_parameterset = config.getini(EMPTY_PARAMETERSET_OPTION)

    if empty_parameterset not in ("skip", "xfail", "fail_at_collect", None, ""):
        raise UsageError(
            "{!s} must be one of skip, xfail or fail_at_collect"
            " but it is {!r}".format(EMPTY_PARAMETERSET_OPTION, empty_parameterset)
        )


def pytest_unconfigure(config: Config) -> None:
    MARK_GEN._config = config.stash.get(old_mark_config_key, None)
