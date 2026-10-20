"""
High-fidelity Markdown to HTML & Platform-Specific Rich Text Parser.
Built strictly with Python standard library. Zero external dependencies.
Guaranteed linear time complexity O(N), no infinite loops.
"""
import re
from typing import List

LINK_RE = re.compile(r'\[([^\]]+)\]\(([^)\s]+)\)')
BARE_URL_RE = re.compile(r'https?://[^\s<>()"\']+[^\s<>()"\'.,;:!?»]')


def _emphasis(text: str) -> str:
    # Bold: **bold** or __bold__
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'(?<!\w)__(?!\s)(.+?)(?<!\s)__(?!\w)', r'<strong>\1</strong>', text)
    # Italic: *italic* or _italic_; an underscore inside a word (snake_case, @user_name) is not emphasis
    text = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<em>\1</em>', text)
    text = re.sub(r'(?<![\w_])_(?![_\s])(.+?)(?<![_\s])_(?![\w_])', r'<em>\1</em>', text)
    return text


def parse_inline_formatting(text: str) -> str:
    """Transforms markdown inline elements (bold, italic, links, code) into HTML.

    URLs are set aside before emphasis: an underscore inside `t.me/name_bot?start=dzen_audit` is part of the
    address, not italics (prod 2026-10-20: every bot link in a published Dzen post was broken this way)."""
    kept: List[str] = []

    def keep(html: str) -> str:
        kept.append(html)
        return f"\x00{len(kept) - 1}\x00"

    # Inline code: `code`
    text = re.sub(r'`([^`]+)`', lambda m: keep(f'<code>{m.group(1)}</code>'), text)
    # Links: [text](url) — the text may carry emphasis, the url is left as is
    text = LINK_RE.sub(lambda m: keep(f'<a href="{m.group(2)}" target="_blank" rel="noopener">'
                                      f'{_emphasis(m.group(1))}</a>'), text)
    text = BARE_URL_RE.sub(lambda m: keep(m.group(0)), text)
    text = _emphasis(text)
    return re.sub(r'\x00(\d+)\x00', lambda m: kept[int(m.group(1))], text)

def parse_markdown_table(table_lines: List[str]) -> str:
    """Converts markdown table lines into clean HTML table markup."""
    if len(table_lines) < 2:
        return "\n".join(table_lines)
    
    header_raw = [cell.strip() for cell in table_lines[0].strip('|').split('|')]
    header_html = "".join(f"<th>{parse_inline_formatting(cell)}</th>" for cell in header_raw)
    
    body_rows_html = []
    for line in table_lines[2:]:
        if not line.strip() or '|' not in line:
            continue
        cells = [cell.strip() for cell in line.strip('|').split('|')]
        while len(cells) < len(header_raw):
            cells.append("")
        cells = cells[:len(header_raw)]
        row_content = "".join(f"<td>{parse_inline_formatting(cell)}</td>" for cell in cells)
        body_rows_html.append(f"<tr>{row_content}</tr>")
    
    tbody = "".join(body_rows_html)
    return f'<div class="table-wrapper"><table class="styled-table"><thead><tr>{header_html}</tr></thead><tbody>{tbody}</tbody></table></div>'

def table_to_dzen_list(table_lines: List[str]) -> str:
    """
    Draft.js on Dzen strips <table> tags, collapsing rows into a single unreadable string.
    This converts a markdown table into an elegant structured comparison list for Dzen.
    """
    if len(table_lines) < 3:
        return ""
    headers = [cell.strip() for cell in table_lines[0].strip('|').split('|')]
    
    items_html = []
    for line in table_lines[2:]:
        if not line.strip() or '|' not in line:
            continue
        cells = [cell.strip() for cell in line.strip('|').split('|')]
        if not cells:
            continue
        param_name = parse_inline_formatting(cells[0])
        comparisons = []
        for i in range(1, len(headers)):
            col_name = headers[i]
            val = parse_inline_formatting(cells[i]) if i < len(cells) else "—"
            comparisons.append(f"{col_name}: <strong>{val}</strong>")
        
        comp_str = " | ".join(comparisons)
        items_html.append(f"<li><strong>{param_name}</strong> &rarr; {comp_str}</li>")
    
    return f'<p><strong>Сравнительный анализ:</strong></p><ul>{"".join(items_html)}</ul>'

def markdown_to_html(md_text: str, platform: str = "general", strip_h1: bool = False) -> str:
    """
    Converts complete markdown document to clean, semantic HTML.
    - If platform == 'dzen', converts tables into structured comparison lists to prevent Draft.js collapse.
    - If strip_h1 is True (for UGC editors that have separate title inputs), omits the top <h1> header.
    """
    if not md_text:
        return ""
    
    normalized = md_text.replace('\r\n', '\n')
    raw_blocks = re.split(r'\n\s*\n', normalized)
    output_html_blocks = []
    
    for block in raw_blocks:
        block = block.strip()
        if not block:
            continue
            
        lines = [l.strip() for l in block.split('\n') if l.strip()]
        if not lines:
            continue
            
        first_line = lines[0]
        
        # Heading 1
        if first_line.startswith('# '):
            if not strip_h1:
                output_html_blocks.append(f"<h1>{parse_inline_formatting(first_line[2:].strip())}</h1>")
            continue
            
        # Heading 2
        if first_line.startswith('## '):
            output_html_blocks.append(f"<h2>{parse_inline_formatting(first_line[3:].strip())}</h2>")
            continue
            
        # Heading 3
        if first_line.startswith('### '):
            output_html_blocks.append(f"<h3>{parse_inline_formatting(first_line[4:].strip())}</h3>")
            continue
            
        # Heading 4
        if first_line.startswith('#### '):
            output_html_blocks.append(f"<h4>{parse_inline_formatting(first_line[5:].strip())}</h4>")
            continue
            
        # Markdown table
        if all(l.startswith('|') and l.endswith('|') for l in lines) and len(lines) >= 2:
            if platform == 'dzen':
                output_html_blocks.append(table_to_dzen_list(lines))
            else:
                output_html_blocks.append(parse_markdown_table(lines))
            continue
            
        # Blockquote
        if all(l.startswith('>') for l in lines):
            q_lines = [l[1:].strip() for l in lines]
            output_html_blocks.append(f"<blockquote><p>{'<br>'.join(parse_inline_formatting(ql) for ql in q_lines)}</p></blockquote>")
            continue
            
        # Bullet list
        if all(re.match(r'^[-*]\s+', l) for l in lines):
            items = [f"<li>{parse_inline_formatting(re.sub(r'^[-*]\s+', '', l))}</li>" for l in lines]
            output_html_blocks.append(f"<ul>{''.join(items)}</ul>")
            continue
            
        # Numbered list
        if all(re.match(r'^\d+\.\s+', l) for l in lines):
            items = [f"<li>{parse_inline_formatting(re.sub(r'^\d+\.\s+', '', l))}</li>" for l in lines]
            output_html_blocks.append(f"<ol>{''.join(items)}</ol>")
            continue
            
        # Italic lead hook (*...*)
        combined = " ".join(lines)
        if combined.startswith('*') and combined.endswith('*') and '**' not in combined:
            inner = combined[1:-1].strip()
            output_html_blocks.append(f'<p class="lead-hook"><em>{parse_inline_formatting(inner)}</em></p>')
            continue
            
        # Regular paragraph
        output_html_blocks.append(f"<p>{parse_inline_formatting(combined)}</p>")
        
    return "\n".join(output_html_blocks)

def to_plain_text(md_text: str) -> str:
    """
    Readable text of a markdown document without markup: used to count length against platform limits.
    Links keep their text, list/heading/quote markers and table pipes are dropped, whitespace is collapsed.
    """
    if not md_text:
        return ""
    text = md_text.replace('\r\n', '\n')
    text = re.sub(r'^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$', ' ', text, flags=re.MULTILINE)
    text = re.sub(r'!?\[([^\]]*)\]\([^)]*\)', r'\1', text)
    text = re.sub(r'^\s{0,3}#{1,6}\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*>\s?', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*(?:[-*+]|\d+\.)\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'`{1,3}', '', text)
    text = re.sub(r'(\*\*|__)(.+?)\1', r'\2', text)
    text = re.sub(r'(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])', r'\1', text)
    text = re.sub(r'(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])', r'\1', text)
    text = text.replace('|', ' ')
    return re.sub(r'\s+', ' ', text).strip()
