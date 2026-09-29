"""Namespace-aware XML parser (Python 3, standard library only).

Produces a flat event stream of expanded (qualified) names following the
"Namespaces in XML" rules:

  * ``xmlns`` / ``xmlns:prefix`` attributes declare bindings scoped to the
    element they appear on (and its descendants).
  * A prefix may be *redefined* on a nested element; the inner binding
    shadows the outer one only within that subtree.
  * Unprefixed *elements* fall back to the default namespace (``xmlns=...``);
    unprefixed *attributes* never do -- they stay in no namespace.
  * Using an undeclared prefix is a fatal error reported with line/column.

Expanded names use the Clark notation ``{namespace-uri}local-name`` so the
output can be diffed directly against ``xml.etree.ElementTree``.
"""

from __future__ import annotations

import re

XML_NS = "http://www.w3.org/XML/1998/namespace"
XMLNS_NS = "http://www.w3.org/2000/xmlns/"

_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.:\-]*")

_ENTITIES = {
    "lt": "<",
    "gt": ">",
    "amp": "&",
    "quot": '"',
    "apos": "'",
}


class NsParseError(Exception):
    """Parse/namespace error carrying 1-based line and column."""

    def __init__(self, message: str, line: int, column: int):
        super().__init__(f"{message} (line {line}, column {column})")
        self.message = message
        self.line = line
        self.column = column


def parse_events(text: str):
    """Parse *text* and return a list of events.

    Events are tuples:
      ("start", qname, attrs)  -- attrs maps expanded name -> value
      ("end",   qname)
      ("text",  data)
    """
    return _Parser(text).run()


class _Parser:
    def __init__(self, text: str):
        self.text = text
        self.pos = 0
        self.events = []
        # Element stack entries: (raw_tag, expanded_tag, scope_dict)
        self.stack = []
        # Base in-scope bindings: the 'xml' prefix is always bound.
        self.base_scope = {"xml": XML_NS}

    # -- low-level helpers -------------------------------------------------

    def _location(self, pos):
        line = self.text.count("\n", 0, pos) + 1
        last_nl = self.text.rfind("\n", 0, pos)
        column = pos - last_nl
        return line, column

    def _error(self, message, pos=None):
        line, column = self._location(self.pos if pos is None else pos)
        raise NsParseError(message, line, column)

    def _peek(self):
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def _starts(self, s):
        return self.text.startswith(s, self.pos)

    def _skip_ws(self):
        while self.pos < len(self.text) and self.text[self.pos] in " \t\r\n":
            self.pos += 1

    def _expect(self, ch):
        if self._peek() != ch:
            self._error(f"expected {ch!r}")
        self.pos += 1

    def _read_name(self, pos=None):
        if pos is not None:
            self.pos = pos
        m = _NAME_RE.match(self.text, self.pos)
        if not m:
            self._error("expected a name")
        self.pos = m.end()
        return m.group(0)

    def _read_quoted(self):
        quote = self._peek()
        if quote not in "\"'":
            self._error("expected a quoted attribute value")
        self.pos += 1
        end = self.text.find(quote, self.pos)
        if end < 0:
            self._error("unterminated attribute value")
        raw = self.text[self.pos:end]
        self.pos = end + 1
        return self._normalize_attr_value(raw)

    @staticmethod
    def _decode_entities(raw, pos):
        def repl(m):
            body = m.group(1)
            if body.startswith("#x") or body.startswith("#X"):
                return chr(int(body[2:], 16))
            if body.startswith("#"):
                return chr(int(body[1:], 10))
            if body in _ENTITIES:
                return _ENTITIES[body]
            raise ValueError(body)

        try:
            return re.sub(r"&([^;\s]+);", repl, raw)
        except (ValueError, KeyError) as exc:
            line, col = pos
            raise NsParseError(f"unknown entity '&{exc.args[0]};'", line, col)

    def _normalize_attr_value(self, raw):
        # XML attribute-value normalization: whitespace -> space.
        raw = raw.replace("\t", " ").replace("\n", " ").replace("\r", " ")
        return self._decode_entities(raw, self._location(self.pos))

    # -- main loop ----------------------------------------------------------

    def run(self):
        while self.pos < len(self.text):
            if self._starts("<!--"):
                self._skip_comment()
            elif self._starts("<![CDATA["):
                self._parse_cdata()
            elif self._starts("<?"):
                self._skip_pi()
            elif self._starts("</"):
                self._parse_end_tag()
            elif self._starts("<!"):
                self._skip_decl()
            elif self._peek() == "<":
                self._parse_start_tag()
            else:
                self._parse_text()
        if self.stack:
            raw, _, _ = self.stack[-1]
            self._error(f"unclosed element <{raw}>")
        return self.events

    def _parse_text(self):
        start = self.pos
        end = self.text.find("<", self.pos)
        if end < 0:
            end = len(self.text)
        self.pos = end
        data = self._decode_entities(self.text[start:end], self._location(start))
        if data.strip():
            self.events.append(("text", data))

    def _skip_comment(self):
        end = self.text.find("-->", self.pos + 4)
        if end < 0:
            self._error("unterminated comment")
        self.pos = end + 3

    def _skip_pi(self):
        end = self.text.find("?>", self.pos + 2)
        if end < 0:
            self._error("unterminated processing instruction")
        self.pos = end + 2

    def _parse_cdata(self):
        start = self.pos + 9
        end = self.text.find("]]>", start)
        if end < 0:
            self._error("unterminated CDATA section")
        self.pos = end + 3
        data = self.text[start:end]
        if data.strip():
            self.events.append(("text", data))

    def _skip_decl(self):
        # DOCTYPE and other <!...> declarations; honor quotes and [ ] depth.
        self.pos += 2
        depth = 0
        quote = None
        while self.pos < len(self.text):
            ch = self.text[self.pos]
            if quote:
                if ch == quote:
                    quote = None
            elif ch in "\"'":
                quote = ch
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
            elif ch == ">" and depth == 0:
                self.pos += 1
                return
            self.pos += 1
        self._error("unterminated declaration")

    # -- tags ----------------------------------------------------------------

    def _parse_start_tag(self):
        self.pos += 1  # consume '<'
        name_pos = self.pos
        raw_name = self._read_name()
        raw_attrs = []  # (raw_name, value, position)
        self_closing = False
        while True:
            self._skip_ws()
            ch = self._peek()
            if ch == ">":
                self.pos += 1
                break
            if ch == "/":
                self.pos += 1
                self._expect(">")
                self_closing = True
                break
            if not ch:
                self._error("unexpected end of input inside a tag")
            attr_pos = self.pos
            attr_name = self._read_name()
            self._skip_ws()
            self._expect("=")
            self._skip_ws()
            value = self._read_quoted()
            raw_attrs.append((attr_name, value, attr_pos))

        seen_raw = set()
        for attr_name, _, attr_pos in raw_attrs:
            if attr_name in seen_raw:
                self._error(f"duplicate attribute '{attr_name}'", attr_pos)
            seen_raw.add(attr_name)

        scope = self._build_scope(raw_attrs)
        qname = self._expand_element_name(raw_name, scope, name_pos)
        attrs = self._expand_attributes(raw_attrs, scope)

        self.events.append(("start", qname, attrs))
        if self_closing:
            self.events.append(("end", qname))
        else:
            self.stack.append((raw_name, qname, scope))

    def _parse_end_tag(self):
        self.pos += 2  # consume '</'
        name_pos = self.pos
        raw_name = self._read_name()
        self._skip_ws()
        self._expect(">")
        if not self.stack:
            self._error(f"unexpected closing tag </{raw_name}>", name_pos)
        open_raw, qname, _scope = self.stack.pop()
        if raw_name != open_raw:
            self._error(
                f"mismatched closing tag </{raw_name}>; expected </{open_raw}>",
                name_pos,
            )
        self.events.append(("end", qname))

    # -- namespace handling ---------------------------------------------------

    def _current_scope(self):
        return self.stack[-1][2] if self.stack else self.base_scope

    def _build_scope(self, raw_attrs):
        scope = dict(self._current_scope())
        for attr_name, value, attr_pos in raw_attrs:
            if attr_name == "xmlns":
                # xmlns="" undeclares the default namespace.
                scope[""] = value
            elif attr_name.startswith("xmlns:"):
                prefix = attr_name[len("xmlns:"):]
                if prefix == "xmlns":
                    self._error(
                        "the 'xmlns' prefix must not be declared", attr_pos
                    )
                if prefix == "xml" and value != XML_NS:
                    self._error(
                        "the 'xml' prefix must be bound to " + XML_NS, attr_pos
                    )
                if not value:
                    self._error(
                        f"cannot undeclare prefix '{prefix}' with an empty URI",
                        attr_pos,
                    )
                scope[prefix] = value
        return scope

    @staticmethod
    def _split_qname(raw, pos, error):
        parts = raw.split(":")
        if len(parts) > 2 or "" in parts:
            error(f"malformed qualified name '{raw}'", pos)
        if len(parts) == 2:
            return parts[0], parts[1]
        return None, parts[0]

    def _expand_element_name(self, raw, scope, pos):
        prefix, local = self._split_qname(raw, pos, self._error)
        if prefix is not None:
            uri = scope.get(prefix)
            if not uri:
                self._error(f"undeclared namespace prefix '{prefix}'", pos)
            return f"{{{uri}}}{local}"
        default_uri = scope.get("")
        if default_uri:
            return f"{{{default_uri}}}{local}"
        return local

    def _expand_attributes(self, raw_attrs, scope):
        attrs = {}
        for attr_name, value, attr_pos in raw_attrs:
            if attr_name == "xmlns" or attr_name.startswith("xmlns:"):
                continue  # namespace declarations are not attributes
            prefix, local = self._split_qname(attr_name, attr_pos, self._error)
            if prefix is not None:
                uri = scope.get(prefix)
                if not uri:
                    self._error(
                        f"undeclared namespace prefix '{prefix}' "
                        f"on attribute '{attr_name}'",
                        attr_pos,
                    )
                expanded = f"{{{uri}}}{local}"
            else:
                # Unprefixed attributes are in NO namespace: the default
                # namespace never applies to attributes.
                expanded = local
            if expanded in attrs:
                self._error(
                    f"attributes expand to the same name '{expanded}'", attr_pos
                )
            attrs[expanded] = value
        return attrs
