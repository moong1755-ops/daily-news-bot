"""Conservative article-local context extraction from email HTML (no network)."""

from dataclasses import dataclass, field
from html.parser import HTMLParser
import re


@dataclass(eq=False)
class _Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    parent: object = None
    children: list = field(default_factory=list)

    def text(self) -> str:
        if self.tag in {"script", "style", "head"}:
            return ""
        return re.sub(r"\s+", " ", " ".join(
            child.text() if isinstance(child, _Node) else child
            for child in self.children
        )).strip()

    def links(self) -> set:
        own = {self.attrs["href"]} if self.tag == "a" and self.attrs.get("href") else set()
        return own.union(*(child.links() for child in self.children if isinstance(child, _Node)))


class _MailHTML(HTMLParser):
    _VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("root")
        self.stack = [self.root]
        self.anchors = []
        self.nodes = []

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, dict(attrs), self.stack[-1])
        node.parent.children.append(node)
        self.nodes.append(node)
        if tag == "a":
            self.anchors.append(node)
        if tag not in self._VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self._VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def article_contexts(html: str, headlines: dict[str, str], limit: int) -> dict[str, str]:
    """Use the linked card/paragraph, never a shared email introduction.

    A separate paragraph after a linked heading is accepted only when it has
    no other destination. Ambiguous layouts get an empty description, not text
    borrowed from a different story. The limit applies to context, not titles.
    """
    parser = _MailHTML()
    parser.feed(html or "")
    contexts = {}
    blocks = {"p", "li", "td", "div", "article", "section", "h1", "h2", "h3", "h4", "h5", "h6"}
    for anchor in parser.anchors:
        url = anchor.attrs.get("href", "")
        if url not in headlines:
            continue
        title = headlines[url]
        block = anchor.parent
        while block and block.tag not in blocks | {"body", "html", "root"}:
            block = block.parent
        text = anchor.text()
        if block and block.tag in blocks and block.links() <= {url}:
            candidate = block.text()
            # A one-link table cell can also contain the email introduction.
            # Never copy material preceding this headline into its description.
            title_start = candidate.find(title)
            if title_start >= 0:
                candidate = candidate[title_start:]
            if len(candidate) <= len(title) + limit:
                text = candidate
            if block.tag.startswith("h") and block.parent:
                siblings = block.parent.children
                following = [node for node in siblings[siblings.index(block) + 1:]
                             if isinstance(node, _Node) or str(node).strip()]
                if following and isinstance(following[0], _Node):
                    paragraph = following[0]
                    if paragraph.tag == "p" and paragraph.links() <= {url}:
                        text += " " + paragraph.text()
        context = text.replace(title, "", 1).strip()
        if context and url not in contexts:
            contexts[url] = context[:limit]
    return contexts
