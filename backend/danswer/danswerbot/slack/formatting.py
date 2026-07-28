"""Markdown -> Slack mrkdwn via a real markdown parser (mistune AST), ported from
upstream Onyx (`onyxbot/slack/formatting.py`).

This is the *alternative* renderer to the lightweight regex helper
`utils.markdown_to_slack`, kept side-by-side so the two can be A/B'd on real answers
(select via the SLACK_MARKDOWN_RENDERER env var — see blocks.py). Unlike the regex
helper, this walks the parsed markdown tree, so it also gets ordered lists, single-*
italics (-> `_i_`), strikethrough, code spans/blocks, and `&<>` escaping right.

Ordering note: this renderer *owns* links and bold, so callers must run it BEFORE the
Slack-entity sanitizer and must use the reduced sanitizer
(`utils.remove_slack_entities_only`) that does NOT strip links or re-handle bold —
otherwise the `<url|text>` links it emits get collapsed back to plain text.
"""
import re
from typing import Any

from mistune import Markdown  # type: ignore
from mistune import Renderer  # type: ignore

# Sentinel markers (chars that won't appear in answer prose) used to segment list
# items and table cells/rows during rendering, then consumed before output.
_LIST_ITEM = "\x00LI\x00"
_CELL_SEP = "\x01"
_ROW_SEP = "\x02"


def format_slack_message(message: str | None) -> str:
    renderer = Markdown(renderer=SlackRenderer())
    return renderer.render(message)


class SlackRenderer(Renderer):
    SPECIALS: dict[str, str] = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}

    def escape_special(self, text: str) -> str:
        for special, replacement in self.SPECIALS.items():
            text = text.replace(special, replacement)
        return text

    def header(self, text: str, level: int, raw: str | None = None) -> str:
        # Diverge from upstream (which returns "*text*\n"): force a blank line BEFORE
        # each section title so multi-section answers don't club together. mistune
        # concatenates block outputs with no separator, so the leading "\n" here is
        # what produces the blank line between sections. The stray leading newline on
        # the very first header is stripped by the caller (render_answer_markdown).
        return f"\n*{text}*\n"

    def emphasis(self, text: str) -> str:
        return f"_{text}_"

    def double_emphasis(self, text: str) -> str:
        return f"*{text}*"

    def strikethrough(self, text: str) -> str:
        return f"~{text}~"

    # List rendering uses sentinel markers (not the upstream "li: " line-prefix hack,
    # which clubbed a parent item onto its first nested bullet). Each item is wrapped
    # in _LIST_ITEM so list() can split reliably; nested lists start with a newline so
    # an item's own text never glues onto a child bullet. Slack can't indent nested
    # lists, so children are shown as indented bullet lines (best effort).
    def list(self, body: str, ordered: bool = True) -> str:
        items = [seg for seg in body.split(_LIST_ITEM) if seg.strip()]
        lines: list[str] = []
        for i, item in enumerate(items, start=1):
            item_lines = item.strip("\n").split("\n")
            prefix = f"{i}. " if ordered else "• "
            lines.append(prefix + item_lines[0])
            for cont in item_lines[1:]:
                lines.append(f"    {cont}" if cont.strip() else cont)
        # leading newline: a nested list must start on its own line, not glued to the
        # parent item's inline text
        return "\n" + "\n".join(lines) + "\n"

    def list_item(self, text: str) -> str:
        return _LIST_ITEM + text

    def block_quote(self, text: str) -> str:
        # Slack renders a leading "> " as a real block quote
        lines = text.strip("\n").split("\n")
        quoted = "\n".join(f"> {ln}" if ln.strip() else ">" for ln in lines)
        return f"\n{quoted}\n"

    def hrule(self) -> str:
        # Slack has no horizontal rule; approximate with a line of box-drawing dashes
        return "\n───────\n"

    def table_cell(self, content: str, **flags: Any) -> str:
        return content.strip().replace("\n", " ") + _CELL_SEP

    def table_row(self, content: str) -> str:
        return content + _ROW_SEP

    def table(self, header: str, body: str) -> str:
        # Slack has no table primitive; render as a monospace code block (which at
        # least keeps rows/columns legible) instead of dumping raw <table> HTML.
        rows: list[str] = []
        for raw_row in (header + body).split(_ROW_SEP):
            if not raw_row:
                continue
            cells = [c for c in raw_row.split(_CELL_SEP) if c != ""]
            rows.append(" | ".join(cells))
        table_text = "\n".join(rows)
        return f"```\n{table_text}\n```\n"

    def link(self, link: str, title: str | None, content: str | None) -> str:
        escaped_link = self.escape_special(link)
        if content:
            return f"<{escaped_link}|{content}>"
        if title:
            return f"<{escaped_link}|{title}>"
        return f"<{escaped_link}>"

    def image(self, src: str, title: str | None, text: str | None) -> str:
        escaped_src = self.escape_special(src)
        display_text = title or text
        return f"<{escaped_src}|{display_text}>" if display_text else f"<{escaped_src}>"

    def codespan(self, text: str) -> str:
        return f"`{text}`"

    def block_code(self, text: str, lang: str | None) -> str:
        return f"```\n{text}\n```\n"

    def paragraph(self, text: str) -> str:
        return f"{text}\n"

    def linebreak(self) -> str:
        # upstream/default emits "<br>\n", which Slack shows literally. A hard line
        # break is just a newline in mrkdwn. (~18% of prod answers hit this.)
        return "\n"

    def _demote_html(self, html: str) -> str:
        # Slack renders raw HTML literally, so never pass it through: turn <br> into a
        # newline and escape any other stray tag so it reads as text, not markup.
        html = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
        return self.escape_special(html)

    def inline_html(self, html: str) -> str:
        return self._demote_html(html)

    def block_html(self, html: str) -> str:
        return self._demote_html(html) + "\n"

    def autolink(self, link: str, is_email: bool) -> str:
        return link if is_email else self.link(link, None, None)
