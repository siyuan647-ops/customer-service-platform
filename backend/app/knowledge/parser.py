from __future__ import annotations

import re
from dataclasses import dataclass


_METADATA_LINE = re.compile(r"^\s*-\s*([^：:]+)[：:]\s*(.*?)\s*$")
_SECTION_HEADING = re.compile(r"^(\d+)\.\s+(.+)$")
_CATEGORY_SEPARATOR = re.compile(r"\s*[、,，;；/]\s*")


class DocumentParseError(ValueError):
    pass


@dataclass(slots=True)
class ParsedDocument:
    title: str
    product_categories: list[str]
    metadata: dict[str, str]
    cleaned_text: str
    body: str


@dataclass(slots=True)
class ParsedChunk:
    section: str
    content: str


def clean_text(text: str) -> str:
    text = text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    cleaned: list[str] = []
    previous_blank = False
    for line in lines:
        blank = not line.strip()
        if blank and previous_blank:
            continue
        cleaned.append(line)
        previous_blank = blank
    return "\n".join(cleaned).strip()


def parse_document(text: str) -> ParsedDocument:
    cleaned = clean_text(text)
    if not cleaned:
        raise DocumentParseError("政策文档不能为空")

    metadata: dict[str, str] = {}
    body_start = 0
    lines = cleaned.split("\n")
    for index, line in enumerate(lines):
        match = _METADATA_LINE.match(line)
        if match:
            metadata[match.group(1).strip()] = match.group(2).strip()
            body_start = index + 1
        elif metadata and line.strip() and not line.startswith("【"):
            body_start = index
            break

    title = metadata.get("政策标题", "").strip()
    category = metadata.get("适用商品分类", "").strip()
    if not title:
        raise DocumentParseError("缺少元数据：政策标题")
    if not category:
        raise DocumentParseError("缺少元数据：适用商品分类")
    body = "\n".join(lines[body_start:]).strip()
    if not body:
        raise DocumentParseError("政策正文不能为空")
    categories = parse_product_categories(category)
    return ParsedDocument(title, categories, metadata, cleaned, body)


def parse_product_categories(value: str) -> list[str]:
    value = value.strip()
    if value.startswith("全品类"):
        return ["全品类"]
    categories = [item.strip() for item in _CATEGORY_SEPARATOR.split(value) if item.strip()]
    return list(dict.fromkeys(categories))


def split_document(
    document: ParsedDocument, *, max_chars: int, overlap_chars: int
) -> list[ParsedChunk]:
    if max_chars < 200 or overlap_chars < 0 or overlap_chars >= max_chars:
        raise ValueError("Invalid chunk size configuration")

    sections: list[tuple[str, list[str]]] = []
    current_title = "政策正文"
    current_lines: list[str] = []
    for line in document.body.split("\n"):
        heading = _SECTION_HEADING.match(line.strip())
        if heading:
            if current_lines:
                sections.append((current_title, current_lines))
            current_title = line.strip()
            current_lines = [line.strip()]
        else:
            current_lines.append(line)
    if current_lines:
        sections.append((current_title, current_lines))

    chunks: list[ParsedChunk] = []
    for section, lines in sections:
        section_text = clean_text("\n".join(lines))
        if not section_text:
            continue
        if len(section_text) <= max_chars:
            chunks.append(ParsedChunk(section, section_text))
            continue
        start = 0
        part = 1
        while start < len(section_text):
            end = min(start + max_chars, len(section_text))
            if end < len(section_text):
                boundary = section_text.rfind("\n", start, end)
                if boundary > start + max_chars // 2:
                    end = boundary
            content = section_text[start:end].strip()
            if content:
                chunks.append(ParsedChunk(f"{section}（{part}）", content))
            if end >= len(section_text):
                break
            start = max(end - overlap_chars, start + 1)
            part += 1
    if not chunks:
        raise DocumentParseError("文档未生成有效分块")
    return chunks
