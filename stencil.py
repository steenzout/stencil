from __future__ import unicode_literals

import importlib

import io
import os
import re

from collections import namedtuple
from io import StringIO

TOK_COMMENT = 'comment'
TOK_TEXT = 'text'
TOK_VAR = 'var'
TOK_BLOCK = 'block'

tag_re = re.compile(r'{%\s*(?P<block>.+?)\s*%}|{{\s*(?P<var>.+?)\s*}}|{#\s*(?P<comment>.+?)\s*#}', re.DOTALL)
nodename_re = re.compile(r'\w+')

Token = namedtuple('Token', 'type content')


def digattr(obj, attr, default=None):
    '''Perform template-style dotted lookup'''
    for step in attr.split('.'):
        try:    # dict lookup
            obj = obj[step]
        except (TypeError, AttributeError, KeyError):
            try:    # attribute lookup
                obj = getattr(obj, step)
            except (TypeError, AttributeError):
                try:    # list index lookup
                    obj = obj[int(step)]
                except (IndexError, ValueError, KeyError, TypeError):
                    return default
        if callable(obj):
            obj = obj()
    return obj


def tokenise(template):
    '''A generator which yields Token instances'''
    upto = 0

    for m in tag_re.finditer(template):
        start, end = m.span()
        if upto < start:
            yield Token(TOK_TEXT, template[upto:start])
        upto = end

        mode = m.lastgroup
        yield Token(mode, m.group(mode).strip())

    if upto < len(template):
        yield Token(TOK_TEXT, template[upto:])


class TemplateLoader(dict):
    def __init__(self, paths):
        self.paths = map(os.path.abspath, paths)

    def load(self, name):
        for path in self.paths:
            full_path = os.path.join(path, name)
            if os.path.isfile(full_path):
                with io.open(full_path, 'r') as fin:
                    return Template(fin.read(), loader=self)

    def __missing__(self, key):
        self[key] = tmpl = self.load(key)
        return tmpl


class Context(object):
    def __init__(self, data, filters=None):
        self._stack = []
        self.push(**data)
        self.filters = filters or {}

    def push(self, **kwargs):
        self._stack.insert(0, kwargs)

    def pop(self):
        self._stack.pop(0)

    def __getitem__(self, key):
        for layer in self._stack:
            if key in layer:
                return layer[key]
        raise KeyError(key)

    def __setitem__(self, key, value):
        self._stack[0][key] = value

    def resolve(self, expr):
        '''Resolve an expression which may also have filters'''
        # Parse expr
        parts = expr.split('|')
        # Resolve in context
        # XXX Make sure first level lookup is ALWAYS dict
        value = digattr(self, parts.pop(0))
        for filt in parts:
            try:
                func = self.filters[filt]
            except KeyError:
                raise SyntaxError("Unknown filter function %s : %s" % (filt, expr))
            else:
                value = func(value)
        return value


class Template(object):
    def __init__(self, src, loader=None):
        self.src = src
        self.loader = loader
        self.tokens = tokenise(src)
        self.nodes = list(self.parse())

    def parse(self):
        for token in self.tokens:
            if token.type == TOK_TEXT:
                yield TextTag(token.content)
            elif token.type == TOK_VAR:
                yield VarTag(token.content)
            elif token.type == TOK_BLOCK:
                m = nodename_re.match(token.content)
                if not m:
                    raise SyntaxError(token)
                yield BlockNode.__tags__[m.group(0)].parse(token.content[m.end(0):].strip(), self)
            else:  # TOK_COMMENT
                pass

    def render(self, context):
        if isinstance(context, dict):
            context = Context(context)
        context.output = output = StringIO()
        for node in self.nodes:
            output.write(node.render(context))
        return output.getvalue()


class Node(object):
    name = None

    def __init__(self, content):
        self.content = content

    def render(self, context):
        return ''


class TextTag(Node):

    def render(self, context):
        return self.content


class VarTag(Node):

    def render(self, context):
        return unicode(context.resolve(self.content))


class BlockMeta(type):
    def __new__(mcs, name, bases, attrs):
        cls = super(BlockMeta, mcs).__new__(mcs, name, bases, attrs)
        if 'name' in attrs:
            BlockNode.__tags__[attrs['name']] = cls
        return cls


class BlockNode(Node):
    __metaclass__ = BlockMeta
    __tags__ = {}

    @classmethod
    def parse(cls, content, parser):
        return cls(content)


class Nodelist(list):
    def __init__(self, parser, end):
        node = next(parser.parse())
        while node.name != end:
            self.append(node)
            node = next(parser.parse())

    def render(self, context):
        for node in self:
            context.output.write(node.render(context))


class ForTag(BlockNode):
    '''
    {% for value in iterable %}
    ...
    {% endfor %}
    '''
    name = 'for'

    def __init__(self, argname, iterable, nodelist):
        self.argname = argname
        self.iterable = iterable
        self.nodelist = nodelist

    @classmethod
    def parse(cls, content, parser):
        arg, iterable = content.split(' in ', 1)
        nodelist = Nodelist(parser, 'endfor')
        return cls(arg.strip(), iterable.strip(), nodelist)

    def render(self, context):
        iterable = context.resolve(self.iterable)
        context.push()
        for idx, item in enumerate(iterable):
            context['loopcounter'] = idx
            context[self.argname] = item
            self.nodelist.render(context)
        context.pop()
        return ''


class EndforTag(BlockNode):
    name = 'endfor'


class IfTag(BlockNode):
    name = 'if'

    def __init__(self, condition, nodelist):
        self.condition = condition
        self.nodelist = nodelist

    @classmethod
    def parse(cls, content, parser):
        nodelist = Nodelist(parser, 'endif')
        return cls(content, nodelist)

    def render(self, context):
        if self.test_condition(context):
            self.nodelist.render(context)
        return ''

    def test_condition(self, context):
        cond = self.condition
        if cond.split(' ', 1)[0].strip() == 'not':
            inv = True
            cond = cond.split(' ', 1)[1].strip()
        else:
            inv = False
        return inv ^ bool(context.resolve(cond))


class EndifTag(BlockNode):
    name = 'endif'


class IncludeTag(BlockNode):
    name = 'include'

    def __init__(self, template_name, loader):
        self.template_name = template_name
        self.loader = loader

    @classmethod
    def parse(cls, content, parser):
        assert parser.loader is not None, "Can't use {% include %} without a bound Loader"
        return cls(content, parser.loader)

    def render(self, context):
        tmpl = self.loader[self.template_name]
        context.push()
        output = tmpl.render(context)
        context.pop()
        return output


class LoadTag(BlockNode):
    name = 'load'

    def render(self, context):
        module = importlib.load_module(self.content)
        return getattr(module, 'init', lambda x: '')(context)
