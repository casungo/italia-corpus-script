"""Two-pass AKN discovery, canonical selection and rendering."""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .akn import AKN_NS, AknFrontmatter, akn_xml_to_markdown, extract_frontmatter, parse_akn_xml

_ILLEGAL_XML10 = re.compile(rb"[\x00-\x08\x0B\x0C\x0E-\x1F]")
_FORMAT_RANK = {"V": 0, "M": 1, "O": 2}
_UPSTREAM_TRUNCATION_SIZE = 1024 * 1024


@dataclass(frozen=True)
class Candidate:
    xml_path: Path
    collection: str
    source_format: str
    metadata: AknFrontmatter
    content: str
    source_articles: int
    source: str = ""

    @property
    def repo_path(self) -> str:
        return f"atti/{self.metadata.codice_redazionale}.md"

    def rank(self) -> tuple[int, int, int, str, str]:
        priorities = [
            value.strip().casefold()
            for value in os.getenv("CANONICAL_COLLECTION_PRIORITY", "Codici").split(",")
            if value.strip()
        ]
        collection = self.collection.casefold()
        primary_rank = next(
            (index for index, name in enumerate(priorities) if name in collection),
            len(priorities),
        )
        return (
            _FORMAT_RANK.get(self.source_format, 9),
            -self.source_articles,
            primary_rank,
            self.collection.casefold(),
            (self.source or self.xml_path.as_posix()).casefold(),
        )


@dataclass
class ConversionError:
    source: str
    message: str


@dataclass
class ConversionReport:
    xml_received: int = 0
    converted: int = 0
    skipped: int = 0
    articles: int = 0
    internal_links: int = 0
    external_links: int = 0
    unresolved_links: int = 0
    urns: int = 0
    editorial_codes: int = 0
    duplicates: int = 0
    errors: list[ConversionError] = field(default_factory=list)
    unsupported_tags: set[str] = field(default_factory=set)
    hashes: dict[str, str] = field(default_factory=dict)
    document_articles: dict[str, int] = field(default_factory=dict)
    document_anchors: dict[str, list[str]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        value = asdict(self)
        value["unsupported_tags"] = sorted(self.unsupported_tags)
        return value


def _read_xml(path: Path) -> str:
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


def discover_candidate(
    collection: str,
    source_format: str,
    source: str,
    raw: bytes,
    report: ConversionReport,
) -> Candidate | None:
    """Parse one XML candidate without materializing its collection on disk."""
    report.xml_received += 1
    content = raw.decode("utf-8", errors="replace")
    xml_bytes = raw
    try:
        if not content.lstrip().startswith("<"):
            try:
                decoded = base64.b64decode(content, validate=True)
            except (ValueError, binascii.Error) as exc:
                if len(raw) == _UPSTREAM_TRUNCATION_SIZE:
                    raise ValueError(
                        "source payload is exactly 1 MiB and appears truncated"
                    ) from exc
                raise ValueError("payload is not XML") from exc
            prefix = decoded.lstrip().lower()
            if prefix.startswith((b"<html", b"<!doctype html")):
                raise ValueError("base64-wrapped HTML, not AKN XML")
            try:
                content = decoded.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("base64 payload is not UTF-8 XML") from exc
            xml_bytes = decoded
        try:
            xml_root = parse_akn_xml(content)
        except ET.ParseError:
            content = _ILLEGAL_XML10.sub(b"", xml_bytes).decode("utf-8", errors="replace")
            try:
                xml_root = parse_akn_xml(content)
            except ET.ParseError as exc:
                if len(raw) == _UPSTREAM_TRUNCATION_SIZE:
                    raise ValueError(
                        "source payload is exactly 1 MiB and appears truncated"
                    ) from exc
                raise
        if xml_root.tag != f"{{{AKN_NS}}}akomaNtoso":
            raise ValueError("XML root is not Akoma Ntoso")
        metadata = extract_frontmatter(xml_root, source_format)
        if not metadata.urn:
            raise ValueError("missing URN")
        if not metadata.codice_redazionale:
            raise ValueError("missing codice_redazionale")
        article_count = sum(
            1 for element in xml_root.iter() if element.tag.rsplit("}", 1)[-1] == "article"
        )
        return Candidate(
            Path(source), collection, source_format, metadata, content, article_count, source
        )
    except Exception as exc:
        report.skipped += 1
        report.errors.append(ConversionError(source, str(exc)))
        return None


def discover_candidates(
    collection_dirs: list[tuple[str, str, Path]], report: ConversionReport
) -> list[Candidate]:
    """Parse metadata for every XML without rendering or mutating an index."""
    candidates: list[Candidate] = []
    for collection, source_format, root in sorted(collection_dirs):
        for path in sorted(root.rglob("*.xml"), key=lambda p: p.as_posix().casefold()):
            candidate = discover_candidate(
                collection, source_format, str(path), path.read_bytes(), report
            )
            if candidate:
                candidates.append(candidate)
    return candidates


def select_canonical(candidates: list[Candidate], report: ConversionReport) -> list[Candidate]:
    by_urn: dict[str, list[Candidate]] = {}
    by_code: dict[str, list[Candidate]] = {}
    for candidate in candidates:
        code = candidate.metadata.codice_redazionale or ""
        by_code.setdefault(code, []).append(candidate)

    collisions = {
        code: group
        for code, group in by_code.items()
        if len({candidate.metadata.urn for candidate in group}) > 1
    }
    for code, group in sorted(collisions.items()):
        sources = " <> ".join(candidate.source or str(candidate.xml_path) for candidate in group)
        urns = " and ".join(sorted({candidate.metadata.urn or "" for candidate in group}))
        report.errors.append(ConversionError(
            f"{code}: {sources}", f"editorial code maps to {urns}"
        ))

    for candidate in candidates:
        if (candidate.metadata.codice_redazionale or "") in collisions:
            continue
        urn = candidate.metadata.urn or ""
        by_urn.setdefault(urn, []).append(candidate)
    report.duplicates = sum(max(0, len(group) - 1) for group in by_urn.values())
    return [min(group, key=Candidate.rank) for _, group in sorted(by_urn.items())]


def render_candidates(candidates: list[Candidate], output: Path, report: ConversionReport) -> dict[str, str]:
    urn_index = {candidate.metadata.urn or "": candidate.repo_path for candidate in candidates}
    output.mkdir(parents=True, exist_ok=True)
    for candidate in sorted(candidates, key=lambda c: c.repo_path):
        target = output / candidate.repo_path
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            content = candidate.content or _read_xml(candidate.xml_path)
            fm, markdown, stats = akn_xml_to_markdown(
                content, urn_index, candidate.repo_path, candidate.source_format
            )
            if not markdown.partition("---\n")[2].strip("-\n "):
                raise ValueError("empty rendered document")
            target.write_text(markdown, encoding="utf-8", newline="\n")
            report.converted += 1
            report.articles += stats.articles
            report.internal_links += stats.internal_links
            report.external_links += stats.external_links
            report.unresolved_links += stats.unresolved_links
            report.unsupported_tags.update(stats.unsupported_tags or ())
            report.hashes[candidate.repo_path] = hashlib.sha256(markdown.encode()).hexdigest()
            report.document_articles[candidate.repo_path] = stats.articles
            report.document_anchors[candidate.repo_path] = re.findall(
                r'<a id="([^"]+)"', markdown
            )
            report.urns += bool(fm.urn)
            report.editorial_codes += bool(fm.codice_redazionale)
        except Exception as exc:
            report.skipped += 1
            report.errors.append(ConversionError(str(candidate.xml_path), str(exc)))
    return urn_index


def convert_akn_dir_to_md(src_dir: str, md_dir: str, urn_index: dict[str, str],
                          collection_name: str) -> int:
    """Compatibility wrapper; new callers should use the explicit two-pass API."""
    report = ConversionReport()
    candidates = discover_candidates([(collection_name, "V", Path(src_dir))], report)
    chosen = select_canonical(candidates, report)
    render_candidates(chosen, Path(md_dir), report)
    urn_index.update({c.metadata.urn or "": c.repo_path for c in chosen})
    if report.errors:
        raise RuntimeError("; ".join(error.message for error in report.errors))
    return report.converted
