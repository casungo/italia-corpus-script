"""Akoma Ntoso metadata extraction and deterministic Markdown rendering."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from .refs import RefContext, resolve_ref

AKN_NS = "http://docs.oasis-open.org/legaldocml/ns/akn/3.0"
ELI_NS = "http://data.europa.eu/eli/ontology#"
NS = {"akn": AKN_NS, "eli": ELI_NS}


@dataclass(frozen=True)
class AknFrontmatter:
    tipo: str | None
    numero: str | None
    data: str | None
    titolo: str | None
    urn: str | None
    codice_redazionale: str | None
    stato_atto: str
    versione_data: str | None
    entrata_in_vigore: str | None
    abrogazione_data: str | None
    fonte_versione: str

    @property
    def vigente(self) -> bool:
        """Deprecated act-level compatibility flag."""
        return self.stato_atto == "vigente"


@dataclass
class RenderStats:
    articles: int = 0
    internal_links: int = 0
    external_links: int = 0
    unresolved_links: int = 0
    unsupported_tags: set[str] = field(default_factory=set)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(el: ET.Element | None) -> str | None:
    if el is None:
        return None
    text = "".join(el.itertext()).strip()
    return " ".join(text.split()) if text else None


def _find_one(root: ET.Element, path: str) -> ET.Element | None:
    return root.find(path, NS)


def _event_date(root: ET.Element, event_type: str) -> str | None:
    event = _find_one(
        root, f".//akn:meta/akn:lifecycle//akn:eventRef[@type='{event_type}']"
    )
    return event.get("date") if event is not None else None


def extract_frontmatter(root: ET.Element, source_format: str = "V") -> AknFrontmatter:
    tipo = _text(_find_one(root, ".//akn:preface//akn:docType"))
    numero = _text(_find_one(root, ".//akn:preface//akn:docNumber"))
    doc_date = _find_one(root, ".//akn:preface//akn:docDate")
    data = doc_date.get("date") if doc_date is not None else None
    titolo = _text(_find_one(root, ".//akn:preface//akn:docTitle"))
    urn_el = _find_one(
        root, ".//akn:meta/akn:identification//akn:FRBRalias[@name='urn:nir']"
    )
    urn = urn_el.get("value") if urn_el is not None else None
    codice_redazionale = _text(
        _find_one(root, ".//akn:meta/akn:proprietary//eli:id_local")
    )
    entrata = _event_date(root, "generation") or _event_date(root, "entryIntoForce")
    abrogazione = _event_date(root, "repeal")
    versione_el = _find_one(
        root, ".//akn:meta/akn:identification//akn:FRBRexpression/akn:FRBRdate"
    )
    versione = versione_el.get("date") if versione_el is not None else None
    reference_date = versione or data or "9999-12-31"
    if abrogazione and abrogazione <= reference_date:
        stato = "abrogato"
    elif entrata and entrata > reference_date:
        stato = "futuro"
    elif source_format.upper() in {"V", "M"}:
        stato = "vigente"
    else:
        stato = "ignoto"
    fonte = {"V": "vigente", "O": "originale", "M": "multivigente"}.get(
        source_format.upper(), "ignoto"
    )
    return AknFrontmatter(
        tipo, numero, data, titolo, urn, codice_redazionale, stato, versione,
        entrata, abrogazione, fonte,
    )


def format_frontmatter(fm: AknFrontmatter) -> str:
    values: list[tuple[str, str | bool | None]] = [
        ("schema_version", "2"), ("tipo", fm.tipo), ("numero", fm.numero),
        ("data", fm.data), ("titolo", fm.titolo), ("urn", fm.urn),
        ("codice_redazionale", fm.codice_redazionale),
        ("stato_atto", fm.stato_atto), ("versione_data", fm.versione_data),
        ("entrata_in_vigore", fm.entrata_in_vigore),
        ("abrogazione_data", fm.abrogazione_data),
        ("fonte_versione", fm.fonte_versione), ("vigente", fm.vigente),
    ]
    lines = ["---"]
    for key, value in values:
        if value is None:
            lines.append(f"{key}: null")
        elif isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        elif key == "titolo":
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{key}: "{escaped}"')
        else:
            lines.append(f"{key}: {value}")
    return "\n".join(lines + ["---", ""])


def _slug(value: str) -> str:
    value = value.lower().replace("°", "")
    return re.sub(r"[^a-z0-9]+", "-", value).strip("-") or "x"


def _anchor(el: ET.Element, ancestors: tuple[str, ...]) -> str:
    eid = el.get("eId") or el.get("id")
    if eid:
        return _slug(eid.replace("__", "-"))
    num = _text(el.find(f"{{{AKN_NS}}}num")) or str(len(ancestors) + 1)
    names = {"article": "art", "paragraph": "comma", "point": "punto", "item": "item"}
    return "-".join((*ancestors, names.get(_local(el.tag), _local(el.tag)), _slug(num)))


def _anchor_tag(el: ET.Element, anchor: str) -> str:
    attributes = [f'id="{anchor}"']
    if valid_from := el.get("start") or el.get("periodStart"):
        attributes.append(f'data-valid-from="{valid_from.lstrip("#")}"')
    if valid_to := el.get("end") or el.get("periodEnd"):
        attributes.append(f'data-valid-to="{valid_to.lstrip("#")}"')
    return f"<a {' '.join(attributes)}></a>"


_CONTAINERS = {
    "akomaNtoso", "act", "doc", "body", "preamble", "conclusions", "mainBody",
    "component", "components", "attachment", "attachments", "container",
    "content", "intro", "wrapUp", "blockList", "list", "item", "point",
    "paragraph", "subparagraph", "clause", "quotedStructure", "mod",
}
_HEADINGS = {"article", "section", "chapter", "part", "division", "title", "subtitle", "book", "tome"}
_INLINE = {"ref", "span", "b", "i", "u", "ins", "del", "quotedText", "date", "term", "abbr", "sub", "sup"}
_IGNORED = {"meta", "preface", "num", "heading"}


def _render_inline(el: ET.Element, ctx: RefContext, stats: RenderStats) -> str:
    if _local(el.tag) == "ref":
        href = el.get("href") or ""
        label = _text(el) or href
        rendered, kind = resolve_ref(href, label, ctx, with_kind=True)
        if kind == "internal":
            stats.internal_links += 1
        elif kind == "external":
            stats.external_links += 1
        else:
            stats.unresolved_links += 1
        return rendered
    parts = [el.text or ""]
    for child in el:
        parts.append(_render_inline(child, ctx, stats))
        parts.append(child.tail or "")
    return "".join(parts).strip()


def _render_table(el: ET.Element, lines: list[str], ctx: RefContext, stats: RenderStats) -> None:
    rows = []
    for row in el.iter():
        if _local(row.tag) == "tr":
            rows.append([_render_inline(c, ctx, stats) for c in row if _local(c.tag) in {"th", "td"}])
    if not rows:
        return
    width = max(map(len, rows))
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines.extend(["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * width) + " |"])
    lines.extend("| " + " | ".join(row) + " |" for row in rows[1:])
    lines.append("")


def _render_block(el: ET.Element, lines: list[str], ctx: RefContext, stats: RenderStats,
                  level: int = 2, ancestors: tuple[str, ...] = ()) -> None:
    tag = _local(el.tag)
    if tag in _IGNORED:
        return
    if tag == "table":
        _render_table(el, lines, ctx, stats)
        return
    if tag in _HEADINGS:
        anchor = _anchor(el, ancestors)
        num = _text(el.find(f"{{{AKN_NS}}}num"))
        heading = _text(el.find(f"{{{AKN_NS}}}heading"))
        title = " — ".join(x for x in (num, heading) if x)
        if title:
            lines.extend([_anchor_tag(el, anchor), f"{'#' * min(level, 6)} {title}", ""])
        if tag == "article":
            stats.articles += 1
        for child in el:
            if _local(child.tag) not in {"num", "heading"}:
                _render_block(child, lines, ctx, stats, level + 1, (*ancestors, anchor))
        return
    if tag == "p":
        text = _render_inline(el, ctx, stats)
        if text:
            lines.extend([text, ""])
        return
    if tag in _INLINE:
        text = _render_inline(el, ctx, stats)
        if text:
            lines.extend([text, ""])
        return
    if tag not in _CONTAINERS:
        stats.unsupported_tags.add(tag)
    children = list(el)
    if children:
        for child in children:
            _render_block(child, lines, ctx, stats, level, ancestors)
    else:
        text = (el.text or "").strip()
        if text:
            lines.extend([text, ""])


def body_to_markdown(root: ET.Element, ctx: RefContext) -> tuple[str, RenderStats]:
    lines: list[str] = []
    stats = RenderStats()
    document = next((child for child in root if _local(child.tag) in {"act", "doc"}), root)
    for child in document:
        if _local(child.tag) not in {"meta", "preface"}:
            _render_block(child, lines, ctx, stats)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines), stats


def parse_akn_xml(content: str) -> ET.Element:
    if not content.strip():
        raise ValueError("Empty content")
    return ET.fromstring(content)


def akn_xml_to_markdown(content: str, urn_index: dict[str, str], source_repo_path: str,
                        source_format: str = "V") -> tuple[AknFrontmatter, str, RenderStats]:
    root = parse_akn_xml(content)
    fm = extract_frontmatter(root, source_format)
    body, stats = body_to_markdown(root, RefContext(urn_index, source_repo_path))
    return fm, format_frontmatter(fm) + body + "\n", stats
