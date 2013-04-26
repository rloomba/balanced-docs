"""
Generates rST by via external scripts.

Use the `dcode-default` directive to set default options:

.. dcode-default: [key]
    :cache: true
    :record: /tmp/dcode.record
    :script: some-script
    :{script-option-1}:
    ...
    :{script-option-n}:

And the `dcode` directive to capture generated rST:

.. dcode: [{key}] [{script-arg-1}] .. [{script-arg-n}]
    :{script-option-1}: value(s)
    ...
    :{script-option-n}: value(s)

    {content}

Note that default options can be overridden by `dcode`.
"""
from collections import defaultdict
import functools
import hashlib
import logging
import os
import pipes
import re
import shlex
import subprocess
import sys

from docutils import nodes
from docutils.parsers.rst import directives, Directive
from docutils.statemachine import ViewList
from timeit import itertools
from sphinx.domains import std

logger = logging.getLogger(__name__)


class DCodeDefaultDirective(Directive):

    class Registry(dict):

        def __init__(self, parent, **kwargs):
            self.parent = parent
            super(type(self), self).__init__(**kwargs)

        def __getitem__(self, key):
            try:
                return super(type(self), self).__getitem__(key)
            except KeyError:
                return self.parent[key]

    default_registry = Registry(
        None,
        script=None,
        cache=False,
        record=None,
        ignore=False,
        section_include=None,
        section_chars='~^',
    )

    registry = defaultdict(functools.partial(Registry, default_registry))
    registry[None] = default_registry

    @classmethod
    def expand(cls, args, options):
        if args:
            key = args[0]
        else:
            key = None
        if 'cache' in options:
            cls.registry[key]['cache'] = True
        if 'script' in options:
            cls.registry[key]['script'] = options['script']
        if 'record' in options:
            cls.registry[key]['record'] = os.path.expanduser(options['record'])
        if 'ignore' in options:
            cls.registry[key]['ignore'] = True
        if 'section-chars' in options:
            cls.registry[key]['section_chars'] = options['section-chars']
        if 'section-include' in options:
            cls.registry[key]['section_include'] = options['section-include'].split()
        return []

    # Directive

    name = 'dcode-default'

    required_arguments = 0

    optional_arguments = 1

    option_spec = {
        'script': directives.unchanged,
        'cache': directives.flag,
        'ignore': directives.flag,
        'record': directives.unchanged,
        'ignore': directives.unchanged,
        'section-chars': '~^',
        'section-include': directives.unchanged,
    }

    has_content = False

    def run(self):
        self.expand(self.arguments, self.options)
        node = nodes.section()
        node.document = self.state.document
        return node.children


class DCodeDirective(Directive):

    @classmethod
    def expand(cls, arguments, options, content):
        # key, args
        if arguments and arguments[0] in DCodeDefaultDirective.registry:
            key = arguments[0]
            args = arguments[1:]
        else:
            key = None
            args = arguments[:]

        # cache
        if 'cache' in options:
            cache = True
        else:
            cache = DCodeDefaultDirective.registry[key]['cache']

        # ignore
        if 'ignore' in options:
            ignore = True
        else:
            ignore = DCodeDefaultDirective.registry[key]['ignore']

        # script
        if 'script' in options:
            script = options['script']
        else:
            script = DCodeDefaultDirective.registry[key]['script']

        # record
        if 'record' in options:
            record = os.path.expanduser(options['record'])
        else:
            record = DCodeDefaultDirective.registry[key]['record']

        # section-*
        if 'section-chars' in options:
            section_chars = options['section-chars']
        else:
            section_chars = DCodeDefaultDirective.registry[key]['section_chars']
        if 'section-include' in options:
            section_include = options['section-include'].split()
        else:
            section_include = DCodeDefaultDirective.registry[key]['section_include']

        # kwargs
        kwargs = dict(
            (k, v.split())
            for k, v in options.iteritems()
            if k not in cls.option_spec
        )

        # generate
        if not script:
            raise ValueError('No scripts for key "{0}"'.format(key))
        view = ViewList()

        def write(l):
            view.append(l if l.strip() else '', '<dcode>')

        if section_include:
            write = _SectionFilter(
                section_chars,
                section_include,
                write,
            )

        if not ignore:
            if isinstance(content, list):
                content = '\n'.join(content)
            _generate(
                cache=cache,
                record=record,
                write=write,
                script=script,
                args=args,
                kwargs=kwargs,
                content=content,
            )

        if section_include:
            write.done()

        return view

    # Directive

    name = 'dcode'

    required_arguments = 0

    optional_arguments = 100

    option_spec = {
        'cache': directives.flag,
        'script': directives.unchanged,
        'record': directives.unchanged,
        'section-include': directives.unchanged,
        'section-chars': directives.unchanged,
    }

    has_content = True

    def run(self):
        view = self.expand(self.arguments, self.options, self.content)
        node = nodes.section()
        node.document = self.state.document
        self.state.nested_parse(view, 0, node, match_titles=1)
        return node.children


# internals

class _SectionFilter(object):

    INCLUDE_SEPARATOR = '.'

    def __init__(self, chars, include, write):
        self.chars = chars
        self.write = write
        self.filtered = False
        self.include = [
            map(str.lower, i.split(self.INCLUDE_SEPARATOR))
            for i in include
        ]
        self._depth = 0
        self._chars = None
        self._h = None

    def __call__(self, l):
        if self._h:
            if self._is_section(self._h, l):
                self._on_section(self._h, l)
            else:
                self._write(self._h)
                self._write(l)
            self._h = None
            return
        if l and not l[0].isspace():
            self._h = l
            return
        self._write(l)

    def done(self):
        if self._h:
            self._write(self._h)
            self._h = None

    def _write(self, l):
        if self.filtered:
            self.write(l)

    def _is_section(self, heading, adornment):
        h = heading.rstrip()
        a = adornment.rstrip()
        return (
            a and
            len(a) == len(h) and
            not a[0].isalnum() and
            len(set(a)) == 1
        )

    def _on_section(self, heading, adorment):
        if self.filtered:
            if adorment[0] in self._chars:
                self._write(heading)
                self._write(adorment)
            else:
                logger.debug('filtering off for "%s", "%s"', heading, adorment)
                self.filtered = False
                self._depth = 0
        else:
            if self.chars[self._depth] != adorment[0]:
                self._depth = 0
            else:
                for i in self.include:
                    if len(i) <= self._depth:
                        continue
                    if i[self._depth] == heading.lower():
                        self._depth += 1
                        if len(i) == self._depth:
                            logger.debug('filtering on for "%s", "%s"', heading, adorment)
                            self._chars = self.chars[self._depth:]
                            self.filtered = True


class _Writer(object):

    def __init__(self):
        self._indent = []
        self._fragment = False

    def __enter__(self):
        self.indent()
        return self

    def __exit__(self, type, value, traceback):
        self.outdent()

    def indent(self, l=4):
        self._indent.append(' ' * l)

    def outdent(self):
        self._indent.pop()

    @property
    def _indentation(self):
        if self._fragment:
            i = ''
        else:
            i = ''.join(self._indent)
        return i

    def line(self, *args):
        raise NotImplementedError()

    def fragment(self, *args):
        raise NotImplementedError()


def _execute(script, args, kwargs, content, record=None):
    cmd = (
        shlex.split(script.encode('utf-8')) +
        args +
        ['--{}={}'.format(k, v) for k, vs in kwargs.iteritems() for v in vs]
    )
    sh_cmd = ' '.join(pipes.quote(p) for p in cmd)
    logger.debug('executing "%s"', sh_cmd)
    if record:
        with open(record, 'a') as fo:
            fo.write(sh_cmd + '\n')
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = proc.communicate(content or '')
    if proc.returncode != 0:
        print >>sys.stderr, sh_cmd, '- failed with exit code', proc.returncode
        print >>sys.stderr, 'stderr:'
        print >>sys.stderr, stderr
        print >>sys.stderr, 'stdout:'
        print >>sys.stderr, stdout
        raise Exception('{} - failed with exit code {}'.format(sh_cmd, proc.returncode))
    return stdout


_CACHE = {
}


def _cache_key(script, args, kwargs, content):
    m = hashlib.md5()
    m.update(script)
    for arg in sorted(args):
        m.update(arg)
    for k, v in sorted(kwargs.items()):
        m.update(k)
        for vv in v:
            m.update(vv)
    for l in content:
        m.update(l)
    return m.hexdigest()


def _generate(
        cache,
        record,
        script,
        args,
        kwargs,
        content,
        write
    ):
    if cache:
        key = _cache_key(script, args, kwargs, content)
        if key in _CACHE:
            logger.debug('cache hit "%s"', key)
            result = _CACHE[key]
        else:
            result = _execute(script, args, kwargs, content, record)
            logger.debug('cache store "%s"', key)
            _CACHE[key] = result
    else:
        result = _execute(script, args, kwargs, content, record)
    for line in result.splitlines():
        write(line)