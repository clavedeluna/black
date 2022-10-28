# Copyright 2004-2005 Elemental Security, Inc. All Rights Reserved.
# Licensed to PSF under a Contributor Agreement.

# Modifications:
# Copyright 2006 Google, Inc. All Rights Reserved.
# Licensed to PSF under a Contributor Agreement.

"""Parser driver.

This provides a high-level interface to parse a file into a syntax tree.

"""

__author__ = "Guido van Rossum <guido@python.org>"

__all__ = ["Driver", "load_grammar"]

# Python imports
import io
import logging
import os
import pkgutil
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from logging import Logger
from typing import (
    IO,
    Any,
    Generic,
    Iterable,
    Iterator,
    List,
    Optional,
    Text,
    Tuple,
    TypeVar,
    Union,
    cast,
)

from blib2to3.pgen2.grammar import Grammar
from blib2to3.pgen2.tokenize import GoodTokenInfo
from blib2to3.pytree import NL

# Pgen imports
from . import grammar, parse, pgen, token, tokenize

Path = Union[str, "os.PathLike[str]"]


@dataclass
class ReleaseRange:
    start: int
    end: Optional[int] = None
    tokens: List[Any] = field(default_factory=list)

    def lock(self) -> None:
        total_eaten = len(self.tokens)
        self.end = self.start + total_eaten


class TokenProxy:
    def __init__(self, generator: Any) -> None:
        self._tokens = generator
        self._counter = 0
        self._release_ranges: List[ReleaseRange] = []

    @contextmanager
    def release(self) -> Iterator["TokenProxy"]:
        release_range = ReleaseRange(self._counter)
        self._release_ranges.append(release_range)
        try:
            yield self
        finally:
            # Lock the last release range to the final position that
            # has been eaten.
            release_range.lock()

    def eat(self, point: int) -> Any:
        eaten_tokens = self._release_ranges[-1].tokens
        if point < len(eaten_tokens):
            return eaten_tokens[point]
        else:
            while point >= len(eaten_tokens):
                token = next(self._tokens)
                eaten_tokens.append(token)
            return token

    def __iter__(self) -> "TokenProxy":
        return self

    def __next__(self) -> Any:
        # If the current position is already compromised (looked up)
        # return the eaten token, if not just go further on the given
        # token producer.
        for release_range in self._release_ranges:
            assert release_range.end is not None

            start, end = release_range.start, release_range.end
            if start <= self._counter < end:
                token = release_range.tokens[self._counter - start]
                break
        else:
            token = next(self._tokens)
        self._counter += 1
        return token

    def can_advance(self, to: int) -> bool:
        # Try to eat, fail if it can't. The eat operation is cached
        # so there wont be any additional cost of eating here
        try:
            self.eat(to)
        except StopIteration:
            return False
        else:
            return True


class Driver(object):
    def __init__(self, grammar: Grammar, logger: Optional[Logger] = None) -> None:
        self.grammar = grammar
        if logger is None:
            logger = logging.getLogger(__name__)
        self.logger = logger

    def parse_tokens(self, tokens: Iterable[GoodTokenInfo], debug: bool = False) -> NL:
        """Parse a series of tokens and return the syntax tree."""
        # XXX Move the prefix computation into a wrapper around tokenize.
        proxy = TokenProxy(tokens)

        p = parse.Parser(self.grammar)
        p.setup(proxy=proxy)

        lineno = 1
        column = 0
        indent_columns: List[int] = []
        type = value = start = end = line_text = None
        prefix = ""

        for quintuple in proxy:
            type, value, start, end, line_text = quintuple
            if start != (lineno, column):
                assert (lineno, column) <= start, ((lineno, column), start)
                s_lineno, s_column = start
                if lineno < s_lineno:
                    prefix += "\n" * (s_lineno - lineno)
                    lineno = s_lineno
                    column = 0
                if column < s_column:
                    prefix += line_text[column:s_column]
                    column = s_column
            if type in (tokenize.COMMENT, tokenize.NL):
                prefix += value
                lineno, column = end
                if value.endswith("\n"):
                    lineno += 1
                    column = 0
                continue
            if type == token.OP:
                type = grammar.opmap[value]
            if debug:
                assert type is not None
                self.logger.debug(
                    "%s %r (prefix=%r)", token.tok_name[type], value, prefix
                )
            if type == token.INDENT:
                indent_columns.append(len(value))
                _prefix = prefix + value
                prefix = ""
                value = ""
            elif type == token.DEDENT:
                _indent_col = indent_columns.pop()
                prefix, _prefix = self._partially_consume_prefix(prefix, _indent_col)
            if p.addtoken(cast(int, type), value, (prefix, start)):
                if debug:
                    self.logger.debug("Stop.")
                break
            prefix = ""
            if type in {token.INDENT, token.DEDENT}:
                prefix = _prefix
            lineno, column = end
            if value.endswith("\n"):
                lineno += 1
                column = 0
        else:
            # We never broke out -- EOF is too soon (how can this happen???)
            assert start is not None
            raise parse.ParseError("incomplete input", type, value, (prefix, start))
        assert p.rootnode is not None
        return p.rootnode

    def parse_stream_raw(self, stream: IO[Text], debug: bool = False) -> NL:
        """Parse a stream and return the syntax tree."""
        tokens = tokenize.generate_tokens(stream.readline, grammar=self.grammar)
        return self.parse_tokens(tokens, debug)

    def parse_stream(self, stream: IO[Text], debug: bool = False) -> NL:
        """Parse a stream and return the syntax tree."""
        return self.parse_stream_raw(stream, debug)

    def parse_file(
        self, filename: Path, encoding: Optional[Text] = None, debug: bool = False
    ) -> NL:
        """Parse a file and return the syntax tree."""
        with io.open(filename, "r", encoding=encoding) as stream:
            return self.parse_stream(stream, debug)

    def parse_string(self, text: Text, debug: bool = False) -> NL:
        """Parse a string and return the syntax tree."""
        tokens = tokenize.generate_tokens(
            io.StringIO(text).readline, grammar=self.grammar
        )
        return self.parse_tokens(tokens, debug)

    def _partially_consume_prefix(self, prefix: Text, column: int) -> Tuple[Text, Text]:
        lines: List[str] = []
        current_line = ""
        current_column = 0
        wait_for_nl = False
        for char in prefix:
            current_line += char
            if wait_for_nl:
                if char == "\n":
                    if current_line.strip() and current_column < column:
                        res = "".join(lines)
                        return res, prefix[len(res) :]

                    lines.append(current_line)
                    current_line = ""
                    current_column = 0
                    wait_for_nl = False
            elif char in " \t":
                current_column += 1
            elif char == "\n":
                # unexpected empty line
                current_column = 0
            else:
                # indent is finished
                wait_for_nl = True
        return "".join(lines), current_line


def _generate_pickle_name(gt: Path, cache_dir: Optional[Path] = None) -> Text:
    head, tail = os.path.splitext(gt)
    if tail == ".txt":
        tail = ""
    name = head + tail + ".".join(map(str, sys.version_info)) + ".pickle"
    if cache_dir:
        return os.path.join(cache_dir, os.path.basename(name))
    else:
        return name


def load_grammar(
    gt: Text = "Grammar.txt",
    gp: Optional[Text] = None,
    save: bool = True,
    force: bool = False,
    logger: Optional[Logger] = None,
) -> Grammar:
    """Load the grammar (maybe from a pickle)."""
    if logger is None:
        logger = logging.getLogger(__name__)
    gp = _generate_pickle_name(gt) if gp is None else gp
    if force or not _newer(gp, gt):
        g: grammar.Grammar = pgen.generate_grammar(gt)
        if save:
            try:
                g.dump(gp)
            except OSError:
                # Ignore error, caching is not vital.
                pass
    else:
        g = grammar.Grammar()
        g.load(gp)
    return g


def _newer(a: Text, b: Text) -> bool:
    """Inquire whether file a was written since file b."""
    if not os.path.exists(a):
        return False
    if not os.path.exists(b):
        return True
    return os.path.getmtime(a) >= os.path.getmtime(b)


def load_packaged_grammar(
    package: str, grammar_source: Text, cache_dir: Optional[Path] = None
) -> grammar.Grammar:
    """Normally, loads a pickled grammar by doing
        pkgutil.get_data(package, pickled_grammar)
    where *pickled_grammar* is computed from *grammar_source* by adding the
    Python version and using a ``.pickle`` extension.

    However, if *grammar_source* is an extant file, load_grammar(grammar_source)
    is called instead. This facilitates using a packaged grammar file when needed
    but preserves load_grammar's automatic regeneration behavior when possible.

    """
    if os.path.isfile(grammar_source):
        gp = _generate_pickle_name(grammar_source, cache_dir) if cache_dir else None
        return load_grammar(grammar_source, gp=gp)
    pickled_name = _generate_pickle_name(os.path.basename(grammar_source), cache_dir)
    data = pkgutil.get_data(package, pickled_name)
    assert data is not None
    g = grammar.Grammar()
    g.loads(data)
    return g


def main(*args: Text) -> bool:
    """Main program, when run as a script: produce grammar pickle files.

    Calls load_grammar for each argument, a path to a grammar text file.
    """
    if not args:
        args = tuple(sys.argv[1:])
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(message)s")
    for gt in args:
        load_grammar(gt, save=True, force=True)
    return True


if __name__ == "__main__":
    sys.exit(int(not main()))
