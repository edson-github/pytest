"""Version info, help messages, tracing configuration."""
import os
import sys
from argparse import Action
from typing import List
from typing import Optional
from typing import Union

import pytest
from _pytest.config import Config
from _pytest.config import ExitCode
from _pytest.config import PrintHelp
from _pytest.config.argparsing import Parser


class HelpAction(Action):
    """An argparse Action that will raise an exception in order to skip the
    rest of the argument parsing when --help is passed.

    This prevents argparse from quitting due to missing required arguments
    when any are defined, for example by ``pytest_addoption``.
    This is similar to the way that the builtin argparse --help option is
    implemented by raising SystemExit.
    """

    def __init__(self, option_strings, dest=None, default=False, help=None):
        super().__init__(
            option_strings=option_strings,
            dest=dest,
            const=True,
            default=default,
            nargs=0,
            help=help,
        )

    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, self.const)

        # We should only skip the rest of the parsing after preparse is done.
        if getattr(parser._parser, "after_preparse", False):
            raise PrintHelp


def pytest_addoption(parser: Parser) -> None:
    group = parser.getgroup("debugconfig")
    group.addoption(
        "--version",
        "-V",
        action="count",
        default=0,
        dest="version",
        help="Display pytest version and information about plugins. "
        "When given twice, also display information about plugins.",
    )
    group._addoption(
        "-h",
        "--help",
        action=HelpAction,
        dest="help",
        help="Show help message and configuration info",
    )
    group._addoption(
        "-p",
        action="append",
        dest="plugins",
        default=[],
        metavar="name",
        help="Early-load given plugin module name or entry point (multi-allowed). "
        "To avoid loading of plugins, use the `no:` prefix, e.g. "
        "`no:doctest`.",
    )
    group.addoption(
        "--traceconfig",
        "--trace-config",
        action="store_true",
        default=False,
        help="Trace considerations of conftest.py files",
    )
    group.addoption(
        "--debug",
        action="store",
        nargs="?",
        const="pytestdebug.log",
        dest="debug",
        metavar="DEBUG_FILE_NAME",
        help="Store internal tracing debug information in this log file. "
        "This file is opened with 'w' and truncated as a result, care advised. "
        "Default: pytestdebug.log.",
    )
    group._addoption(
        "-o",
        "--override-ini",
        dest="override_ini",
        action="append",
        help='Override ini option with "option=value" style, '
        "e.g. `-o xfail_strict=True -o cache_dir=cache`.",
    )


@pytest.hookimpl(hookwrapper=True)
def pytest_cmdline_parse():
    outcome = yield
    config: Config = outcome.get_result()

    if config.option.debug:
        # --debug | --debug <file.log> was provided.
        path = config.option.debug
        debugfile = open(path, "w")
        debugfile.write(
            "versions pytest-%s, "
            "python-%s\ncwd=%s\nargs=%s\n\n"
            % (
                pytest.__version__,
                ".".join(map(str, sys.version_info)),
                os.getcwd(),
                config.invocation_params.args,
            )
        )
        config.trace.root.setwriter(debugfile.write)
        undo_tracing = config.pluginmanager.enable_tracing()
        sys.stderr.write("writing pytest debug information to %s\n" % path)

        def unset_tracing() -> None:
            debugfile.close()
            sys.stderr.write("wrote pytest debug information to %s\n" % debugfile.name)
            config.trace.root.setwriter(None)
            undo_tracing()

        config.add_cleanup(unset_tracing)


def showversion(config: Config) -> None:
    if config.option.version > 1:
        sys.stdout.write(
            f"This is pytest version {pytest.__version__}, imported from {pytest.__file__}\n"
        )
        if plugininfo := getpluginversioninfo(config):
            for line in plugininfo:
                sys.stdout.write(line + "\n")
    else:
        sys.stdout.write(f"pytest {pytest.__version__}\n")


def pytest_cmdline_main(config: Config) -> Optional[Union[int, ExitCode]]:
    if config.option.version > 0:
        showversion(config)
        return 0
    elif config.option.help:
        config._do_configure()
        showhelp(config)
        config._ensure_unconfigure()
        return 0
    return None


def showhelp(config: Config) -> None:
    import textwrap

    reporter = config.pluginmanager.get_plugin("terminalreporter")
    tw = reporter._tw
    tw.write(config._parser.optparser.format_help())
    tw.line()
    tw.line(
        "[pytest] ini-options in the first "
        "pytest.ini|tox.ini|setup.cfg|pyproject.toml file found:"
    )
    tw.line()

    columns = tw.fullwidth  # costly call
    indent_len = 24  # based on argparse's max_help_position=24
    indent = " " * indent_len
    for name in config._parser._ininames:
        help, type, default = config._parser._inidict[name]
        if type is None:
            type = "string"
        if help is None:
            raise TypeError(f"help argument cannot be None for {name}")
        spec = f"{name} ({type}):"
        tw.write(f"  {spec}")
        spec_len = len(spec)
        if spec_len > (indent_len - 3):
            # Display help starting at a new line.
            tw.line()
            helplines = textwrap.wrap(
                help,
                columns,
                initial_indent=indent,
                subsequent_indent=indent,
                break_on_hyphens=False,
            )

            for line in helplines:
                tw.line(line)
        else:
            # Display help starting after the spec, following lines indented.
            tw.write(" " * (indent_len - spec_len - 2))
            if wrapped := textwrap.wrap(
                help, columns - indent_len, break_on_hyphens=False
            ):
                tw.line(wrapped[0])
                for line in wrapped[1:]:
                    tw.line(indent + line)

    tw.line()
    tw.line("Environment variables:")
    vars = [
        ("PYTEST_ADDOPTS", "Extra command line options"),
        ("PYTEST_PLUGINS", "Comma-separated plugins to load during startup"),
        ("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "Set to disable plugin auto-loading"),
        ("PYTEST_DEBUG", "Set to enable debug tracing of pytest's internals"),
    ]
    for name, help in vars:
        tw.line(f"  {name:<24} {help}")
    tw.line()
    tw.line()

    tw.line("to see available markers type: pytest --markers")
    tw.line("to see available fixtures type: pytest --fixtures")
    tw.line(
        "(shown according to specified file_or_dir or current dir "
        "if not specified; fixtures with leading '_' are only shown "
        "with the '-v' option"
    )

    for warningreport in reporter.stats.get("warnings", []):
        tw.line(f"warning : {warningreport.message}", red=True)
    return


conftest_options = [("pytest_plugins", "list of plugin names to load")]


def getpluginversioninfo(config: Config) -> List[str]:
    lines = []
    if plugininfo := config.pluginmanager.list_plugin_distinfo():
        lines.append("setuptools registered plugins:")
        for plugin, dist in plugininfo:
            loc = getattr(plugin, "__file__", repr(plugin))
            content = f"{dist.project_name}-{dist.version} at {loc}"
            lines.append(f"  {content}")
    return lines


def pytest_report_header(config: Config) -> List[str]:
    lines = []
    if config.option.debug or config.option.traceconfig:
        lines.append(f"using: pytest-{pytest.__version__}")

        if verinfo := getpluginversioninfo(config):
            lines.extend(verinfo)

    if config.option.traceconfig:
        lines.append("active plugins:")
        items = config.pluginmanager.list_name_plugin()
        for name, plugin in items:
            r = plugin.__file__ if hasattr(plugin, "__file__") else repr(plugin)
            lines.append(f"    {name:<20}: {r}")
    return lines
