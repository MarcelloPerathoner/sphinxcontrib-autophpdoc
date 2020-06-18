"""
    sphinxcontrib.autophpdoc
    ~~~~~~~~~~~~~~~~~~~~~~~~

    Automatically insert docstrings for PHP functions, classes or whole modules into the doctree.

    - use PHPDOC to build a structure.xml file of your whole project.

         phpdoc -d src -t doc_src/phpdoc --template="xml"

    - add to your conf.py

       .. code::

          extensions = [
             ...
             'sphinxcontrib.phpdomain',
             'sphinxcontrib.autophpdoc',
          ]

          autophpdoc_structure_xml = 'doc_src/phpdoc/structure.xml'
          autophpdoc_members = True
          autophpdoc_title = True

    - in your documentation:

       .. php:automodule:: ^modules/main.php ^modules/.*.php

    :copyright: Copyright 2019 by Marcello Perathoner <marcello@perathoner.de>
    :license: BSD, see LICENSE for details.
"""

import os
import re
from typing import Any, Callable, Dict, Iterator, List, Sequence, Set, Tuple, Union # noqa

import docutils
from docutils.parsers.rst import directives
from docutils.statemachine import StringList

import sphinx
from sphinx.util.docutils import SphinxDirective, switch_source_input
from sphinx.util.nodes import nested_parse_with_titles
from sphinx.errors import SphinxWarning, SphinxError, ExtensionError

from lxml import etree

import pbr.version

if False:
    # For type annotations
    from sphinx.application import Sphinx  # noqa

__version__ = pbr.version.VersionInfo ('autophpdoc').version_string ()


NAME = 'autophpdoc'

logger = sphinx.util.logging.getLogger (__name__)

RE_AUTOSTRIP = re.compile (r'^php:auto') # strip directive name to obtain objtype
RE_TRIM      = re.compile (r'(</?p>)')
RE_BRACES    = re.compile (r'(\s*\(.*\))')
RE_WS        = re.compile (r'(\s+)')

NS = {
    're' : 'http://exslt.org/regular-expressions'
}

def trim (text):
    """ Normalize spaces and remove other useless stuff PHPDoc put in. """
    text = RE_TRIM.sub ('', text)
    return RE_WS.sub (' ', text.strip ())

def strip_braces (text):
    """ Strip the braces from function signatures. """
    return RE_BRACES.sub ('', text)

def bs (link):
    """ Replace \\ with \\\\ because RST wants it that way. """
    # phpdomain does not grok leading backslashes
    link = link.lstrip ('\\')
    return link.replace ('\\', '\\\\')


def setup (app):
    # type: (Sphinx) -> Dict[unicode, Any]

    app.add_directive_to_domain ('php', 'automodule',   AutoDirective)
    app.add_directive_to_domain ('php', 'autoclass',    AutoDirective)
    app.add_directive_to_domain ('php', 'autofunction', AutoDirective)

    app.add_config_value (NAME + '_structure_xml', '', False)
    app.add_config_value (NAME + '_members', False, False)
    app.add_config_value (NAME + '_title', False, False)

    return {
        'version'            : __version__,
        'parallel_read_safe' : True,
    }


def members_option (arg: Any) -> Union[bool, List[str]]:
    """Used to convert the :members: option to auto directives."""
    if arg is None or arg is True:
        return True
    if arg is False:
        return False
    return [x.strip () for x in arg.split (',')]

def bool_option(arg: Any) -> bool:
    """Used to convert flag options to auto directives.  (Instead of
    directives.flag(), which returns None).
    """
    return True

class AutoPHPDocError (SphinxError):
    """ The autophpdoc exception. """
    category = NAME + ' error'


seen_namespaces = set () # the first time seen gets in the index, the others must have :noindex:


class Subject (object):
    """ A thing to document. """

    def __init__ (self, node, indent, directive):
        self.node = node
        self.indent = indent
        self.directive = directive
        self.options = self.directive.options

    def xpath (self, query):
        """ Perform an xpath search starting at this node. """
        return self.node.xpath (query, namespaces = NS)

    def xpath_str (self, query, default = None):
        """ Perform an xpath search returning a string starting at this node. """
        el = self.node.xpath (query, namespaces = NS, smart_strings = False)
        if not el:
            return default
        try:
            return etree.tostring (el[0], encoding = 'unicode', method = 'text').strip ()
        except TypeError:
            return str (el[0]).strip ()

    def splitlines (self, text):
        return [(' ' * self.indent) + s for s in text.splitlines ()]

    def append (self, text, content):
        sourceline = self.get_lineno ()
        if isinstance (text, str):
            text = self.splitlines (text)
        for lineno, line in enumerate (text):
            content.append (
                line,
                '%s:%d:<%s>' % (self.get_filename (), sourceline + lineno, NAME)
            )

    def append_ns (self, content):
        ns = self.get_namespace ()
        self.append (".. php:namespace:: %s" % ns, content)
        if ns in seen_namespaces:
            self.append ("   :noindex:", content)
        else:
            seen_namespaces.add (ns)
        self.nl (content)

    def append_desc (self, content):
        self.append (self.get_description (), content)
        self.nl (content)
        self.append (self.get_long_description (), content)
        self.nl (content)
        for node in self.xpath ("docblock/tag[@name='see']"):
            PHPSee (node, self.indent, self.directive).run (content)
            self.nl (content)

    def nl (self, content):
        content.append ('', '')

    def underscore (self, text, char, content):
        self.append (text, content)
        self.append (char * len (text), content)
        self.nl (content)

    def get_filename (self):
        return self.xpath_str ('ancestor-or-self::file/@path', 'filename unknown')

    def get_lineno (self):
        # N.B. phpdoc doesn't get the line nos. of the subtags right
        # not much we can do
        if 'line' in self.node.attrib:
            return int (self.node.get ('line'))
        return int (self.xpath_str ('docblock/@line', '0'))

    def get_description (self):
        return self.xpath_str ('docblock/description', '')

    def get_long_description (self):
        return self.xpath_str ('docblock/long-description', '')

    def get_name (self):
        return self.xpath_str ('name', '')

    def get_value (self):
        return self.xpath_str ('value', '')

    def get_full_name (self):
        return self.xpath_str ('full_name', '')

    def get_type (self):
        return self.xpath_str ('docblock/tag[@name="var"]/@type', '')

    def get_namespace (self):
        return self.xpath_str ("@namespace", '')

    def get_package (self):
        return self.xpath_str ("tag[@name='package']", '')

    def xref (self, link):
        if link:
            what = 'ref'
            if link in self.directive.classes:
                what = 'php:class'
            elif link in self.directive.functions:
                what = 'php:func'
            elif link in self.directive.methods:
                what = 'php:meth'
            elif link in self.directive.properties:
                what = 'php:attr'
            link = bs (link)
            return ":%s:`%s`" % (what, link) if what != 'ref' else link
        return ''


class PHPArgument (Subject):

    def run (self, content):
        name  = trim (self.node.get ('variable'))
        type_ = trim (self.node.get ('type'))
        desc  = trim (self.node.get ('description'))
        self.append (":param %s %s: %s" % (bs (type_), name, desc), content)


class PHPReturn (Subject):

    def run (self, content):
        type_ = trim (self.node.get ('type'))
        desc  = trim (self.node.get ('description'))
        if desc:
            self.append (":returns: %s" % desc, content)
        if type_:
            self.append (":rtype: %s" % self.xref (type_), content)


class PHPThrows (Subject):

    def run (self, content):
        type_ = trim (self.node.get ('type'))
        desc  = trim (self.node.get ('description') or '')

        self.append (":raises %s: %s" % (self.xref (type_), desc), content)


class PHPSee (Subject):

    def run (self, content):
        desc = trim (self.node.get ('description'))
        link = self.node.get ('link')
        if link.startswith ('http'):
            self.append ("See: %s %s" % (link, desc), content)
        else:
            self.append ("See: %s %s" % (self.xref (link), desc), content)
        self.nl (content)


class PHPVariable (Subject):

    def run (self, content):
        type_ = self.get_type ()
        if type_:
            self.append ("(%s)" % self.xref (type_), content)
        self.append_desc (content)


class PHPConstant (PHPVariable):

    def run (self, content):
        self.append_ns (content)
        self.append (".. php:const:: %s" % self.get_name (), content)
        self.nl (content)
        self.indent += 3

        self.append (self.get_value (), content)
        self.nl (content)

        super ().run (content)


class PHPProperty (PHPVariable):

    def run (self, content):
        self.append (".. php:attr:: %s" % self.get_name (), content)
        self.nl (content)
        self.indent += 3

        super ().run (content)


class PHPCallable (Subject):

    def get_signature (self):
        args = self.xpath ('argument/name/text ()')
        return "%s (%s)" % (self.get_name (), ', '.join (args))

    def run (self, content):
        self.indent += 3

        self.append_desc (content)

        for node in self.xpath ("docblock/tag[@name='param']"):
            PHPArgument (node, self.indent, self.directive).run (content)
        for node in self.xpath ("docblock/tag[@name='return']"):
            PHPReturn (node, self.indent, self.directive).run (content)
        for node in self.xpath ("docblock/tag[@name='throws']"):
            PHPThrows (node, self.indent, self.directive).run (content)
        self.nl (content)


class PHPFunction (PHPCallable):
    def run (self, content):
        self.append_ns (content)
        self.append (".. php:function:: %s" % self.get_signature (), content)
        self.nl (content)
        super ().run (content)


class PHPMethod (PHPCallable):
    def run (self, content):
        self.append (".. php:method:: %s" % self.get_signature (), content)
        self.nl (content)
        super ().run (content)


class PHPClass (Subject):
    def run (self, content):
        self.append_ns (content)
        self.append (".. php:class:: %s" % self.get_name (), content)
        self.nl (content)
        self.indent += 3

        self.append_desc (content)

        for node in self.xpath ("property"):
            PHPProperty (node, self.indent, self.directive).run (content)
        for node in self.xpath ("method"):
            PHPMethod (node, self.indent, self.directive).run (content)
        self.nl (content)


class PHPModule (Subject):

    def get_name (self):
        return self.xpath_str ('@path', '')

    def run (self, content):
        filename = self.get_name ()
        module = os.path.splitext (filename)[0].replace ('/', '.')
        self.append (".. module:: %s" % module, content)
        self.nl (content)
        if self.directive.get_opt ('title'):
            self.underscore (self.get_name (), '-', content)
            self.nl (content)
        self.append_desc (content)

        if self.directive.get_opt ('members') is True:
            for node in self.xpath ("constant"):
                PHPConstant (node, self.indent, self.directive).run (content)
            for node in self.xpath ("function"):
                PHPFunction (node, self.indent, self.directive).run (content)
            for node in self.xpath ("class"):
                PHPClass (node, self.indent, self.directive).run (content)
        self.nl (content)


class AutoDirective (SphinxDirective):
    """Directive to document a whole PHP file. """

    # file path regex (should match a file/@path as found inside structure.xml)
    required_arguments = 1

    # more file path regexes
    optional_arguments = 999

    has_content = False

    option_spec = {
        'structure_xml' : directives.unchanged,  # path of structure.xml file, overrides config
        'members'       : members_option,        # which members to include (default: all)
        'title'         : bool_option,           # should we output a section title
    }


    def get_opt (self, name, required = False):
        opt = self.options.get (name) or getattr (self.env.config, "%s_%s" % (NAME, name))
        if required and not opt:
            raise AutoPHPDocError (
                ':%s: option required in directive (or set %s_%s in conf.py).' % (name, NAME, name)
            )
        return opt


    def run (self):
        structure_xml = self.get_opt ('structure_xml')

        parent = docutils.nodes.section ()
        parent.document = self.state.document
        content = StringList ()
        objtype = RE_AUTOSTRIP.sub ('', self.name)  # strip prefix
        visited = set ()

        try:
            tree = etree.parse (structure_xml)
            self.state.document.settings.record_dependencies.add (structure_xml)

            self.functions  = set (tree.xpath ('//function/full_name/text ()', smart_strings = False))
            self.classes    = set (tree.xpath ('//class/full_name/text ()',    smart_strings = False))
            self.methods    = set (tree.xpath ('//method/full_name/text ()',   smart_strings = False))
            self.properties = set (tree.xpath ('//property/full_name/text ()', smart_strings = False))

            for k in list (self.functions):
                self.functions.add (strip_braces (k))
            for k in list (self.methods):
                self.methods.add (strip_braces (k))

            for argument in self.arguments:
                xpath_query = "//file[re:test (@path, '%s')]" % argument

                filenodes = {}
                for node in tree.xpath (xpath_query, namespaces = NS):
                    filenodes[node.get ('path')] = node

                for path in sorted (filenodes.keys ()):
                    if path in visited:
                        continue
                    visited.add (path)
                    node = filenodes[path]

                    if objtype == 'module':
                        PHPModule (node, 0, self).run (content)
                    if objtype == 'class':
                        PHPClass (node, 0, self).run (content)
                    if objtype == 'method':
                        PHPMethod (node, 0, self).run (content)
                    if objtype == 'function':
                        PHPFunction (node, 0, self).run (content)

            with switch_source_input (self.state, content):
                # logger.info (content.pprint ())
                nested_parse_with_titles (self.state, content, parent)

        except etree.LxmlError as exc:
            logger.error ('LXML Error in "%s" directive: %s.' % (self.name, str (exc)))

        return parent.children
