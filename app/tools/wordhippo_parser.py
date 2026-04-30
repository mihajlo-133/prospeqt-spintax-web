"""Parser for WordHippo synonym pages."""

from __future__ import annotations

import re
from typing import Dict, List

from app.tools.utils import clean_text, unique_preserve_order


_WORDTYPE_RE = re.compile(
    r'<div class="wordtype"[^>]*>(.*?)<div class="totoparrow".*?<a class="contextAnchor" name="(C0-\d+)"',
    re.S,
)

_SECTION_RE = re.compile(
    r'<a class="contextAnchor" name="(C0-\d+)">.*?</div>\s*'
    r'<div class="tabdesc">(.*?)</div>\s*'
    r'<div class="relatedwords">(.*?)</div>\s*</div>',
    re.S,
)

_LINK_RE = re.compile(r'<a href="[^"]+">(.*?)</a>', re.S)


def parse_wordhippo_sections(raw_html: str) -> List[Dict[str, object]]:
    wordtypes: Dict[str, str] = {}
    for match in _WORDTYPE_RE.finditer(raw_html):
        wordtypes[match.group(2)] = clean_text(match.group(1))

    sections: List[Dict[str, object]] = []
    for match in _SECTION_RE.finditer(raw_html):
        context_id = match.group(1)
        definition = clean_text(match.group(2))
        related_html = match.group(3)
        synonyms = unique_preserve_order(
            [clean_text(item) for item in _LINK_RE.findall(related_html) if clean_text(item)]
        )
        sections.append(
            {
                "context_id": context_id,
                "word_type": wordtypes.get(context_id),
                "definition": definition,
                "synonyms": synonyms,
                "synonym_count": len(synonyms),
            }
        )
    return sections
