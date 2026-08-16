"""对 Pandoc 生成的 DOCX 做小范围、可测试的 OOXML 修补。"""

from __future__ import annotations

import copy
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import tempfile
from typing import Mapping, Sequence
import xml.etree.ElementTree as ET
import zipfile

from exporter import ExportError
from word_exporter import SectionSpec


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
CP = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
DC = "http://purl.org/dc/elements/1.1/"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
XML = "http://www.w3.org/XML/1998/namespace"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
NS = {"w": W, "r": R, "cp": CP, "dc": DC}

_PREFIX_URIS = {
    "w": W,
    "r": R,
    "cp": CP,
    "dc": DC,
    "dcterms": "http://purl.org/dc/terms/",
    "dcmitype": "http://purl.org/dc/dcmitype/",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
    "mc": MC,
    "m": "http://schemas.openxmlformats.org/officeDocument/2006/math",
    "o": "urn:schemas-microsoft-com:office:office",
    "v": "urn:schemas-microsoft-com:vml",
    "w10": "urn:schemas-microsoft-com:office:word",
    "w14": "http://schemas.microsoft.com/office/word/2010/wordml",
    "w15": "http://schemas.microsoft.com/office/word/2012/wordml",
    "w16cid": "http://schemas.microsoft.com/office/word/2016/wordml/cid",
    "w16se": "http://schemas.microsoft.com/office/word/2015/wordml/symex",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "wp14": "http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing",
    "wpg": "http://schemas.microsoft.com/office/word/2010/wordprocessingGroup",
    "wpi": "http://schemas.microsoft.com/office/word/2010/wordprocessingInk",
    "wps": "http://schemas.microsoft.com/office/word/2010/wordprocessingShape",
}
for prefix, uri in _PREFIX_URIS.items():
    ET.register_namespace(prefix, uri)


def _q(namespace: str, name: str) -> str:
    return f"{{{namespace}}}{name}"


def _read_package(path: Path) -> dict[str, bytes]:
    if not path.is_file() or not zipfile.is_zipfile(path):
        raise ExportError("DOCX 文件不存在或不是有效的 ZIP 包")
    parts: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                name = info.filename.replace("\\", "/")
                pure = PurePosixPath(name)
                if pure.is_absolute() or ".." in pure.parts or name.startswith("/"):
                    raise ExportError(f"DOCX 包含越界路径：{name}")
                if not info.is_dir():
                    parts[name] = archive.read(info)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ExportError(f"DOCX 包读取失败：{exc}") from exc
    return parts


def _xml(parts: dict[str, bytes], name: str) -> ET.Element:
    try:
        return ET.fromstring(parts[name])
    except KeyError:
        raise ExportError(f"DOCX 缺少必要部件：{name}") from None
    except ET.ParseError as exc:
        raise ExportError(f"DOCX XML 损坏：{name}：{exc}") from exc


def _serialize(root: ET.Element) -> bytes:
    """序列化并保持 ``mc:Ignorable`` 与实际命名空间声明一致。

    ElementTree 会丢弃只出现在 Ignorable 属性值中的 xmlns 声明。Word 会把这类
    XML 判为损坏，因此只保留文档里确有元素/属性使用、且能稳定恢复前缀的命名空间。
    """
    used_uris: set[str] = set()
    for element in root.iter():
        for name in (element.tag, *element.attrib):
            if isinstance(name, str) and name.startswith("{"):
                used_uris.add(name[1:].split("}", 1)[0])
    ignorable_name = _q(MC, "Ignorable")
    value = root.get(ignorable_name)
    if value:
        kept = [prefix for prefix in value.split()
                if _PREFIX_URIS.get(prefix) in used_uris]
        if kept:
            root.set(ignorable_name, " ".join(kept))
        else:
            root.attrib.pop(ignorable_name, None)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _relationship_base(name: str) -> str:
    path = PurePosixPath(name)
    if name == "_rels/.rels":
        return ""
    if path.parent.name != "_rels":
        return path.parent.as_posix()
    return path.parent.parent.as_posix()


def _validate_relationships(parts: dict[str, bytes]) -> None:
    for name in sorted(part for part in parts if part.endswith(".rels")):
        root = _xml(parts, name)
        base = _relationship_base(name)
        for relationship in root.findall(_q(PKG_REL, "Relationship")):
            if relationship.get("TargetMode") == "External":
                continue
            target = relationship.get("Target", "")
            normalized = posixpath.normpath(posixpath.join(base, target))
            if (not target or normalized == ".." or normalized.startswith("../")
                    or normalized not in parts):
                raise ExportError(
                    f"DOCX 关系指向不存在：{name} -> {target or '<empty>'}")


def validate_docx(path: Path) -> None:
    """校验核心部件、所有 XML 和内部关系目标。"""
    parts = _read_package(Path(path))
    for required in ("[Content_Types].xml", "_rels/.rels", "word/document.xml"):
        if required not in parts:
            raise ExportError(f"DOCX 缺少必要部件：{required}")
    for name, data in parts.items():
        if name.endswith((".xml", ".rels")):
            try:
                ET.fromstring(data)
            except ET.ParseError as exc:
                raise ExportError(f"DOCX XML 损坏：{name}：{exc}") from exc
    _validate_relationships(parts)


def _paragraph_text(paragraph: ET.Element) -> str:
    return "".join(node.text or "" for node in paragraph.findall(".//w:t", NS))


def _clear_paragraph(paragraph: ET.Element) -> ET.Element:
    properties = paragraph.find("w:pPr", NS)
    for child in list(paragraph):
        if child is not properties:
            paragraph.remove(child)
    if properties is None:
        properties = ET.Element(_q(W, "pPr"))
        paragraph.insert(0, properties)
    return properties


def _set_child(parent: ET.Element, name: str,
               attributes: Mapping[str, str]) -> ET.Element:
    child = parent.find(f"w:{name}", NS)
    if child is None:
        child = ET.SubElement(parent, _q(W, name))
    for key, value in attributes.items():
        child.set(_q(W, key), str(value))
    return child


def _section_xml(spec: SectionSpec, source: ET.Element | None = None,
                 break_type: str | None = None) -> ET.Element:
    section = ET.Element(_q(W, "sectPr"))
    if source is not None:
        for name in ("headerReference", "footerReference", "titlePg"):
            for child in source.findall(f"w:{name}", NS):
                section.append(copy.deepcopy(child))
    if break_type:
        _set_child(section, "type", {"val": break_type})
    if spec.orientation == "slides":
        _set_child(section, "pgSz", {"w": "19199", "h": "10800", "orient": "landscape"})
        _set_child(section, "pgMar", {
            "top": "720", "right": "720", "bottom": "720", "left": "720",
            "header": "432", "footer": "432", "gutter": "0",
        })
    else:
        _set_child(section, "pgSz", {"w": "11906", "h": "16838"})
        _set_child(section, "pgMar", {
            "top": "1080", "right": "1080", "bottom": "1080", "left": "1080",
            "header": "576", "footer": "576", "gutter": "0",
        })
    column_attributes = {"space": "432"}
    if spec.columns > 1:
        column_attributes["num"] = str(spec.columns)
    _set_child(section, "cols", column_attributes)
    return section


def _replace_layout_markers(document: ET.Element,
                            sections: Sequence[SectionSpec]) -> None:
    body = document.find("w:body", NS)
    if body is None:
        raise ExportError("DOCX 正文结构损坏")
    final_section = body.find("w:sectPr", NS)
    if final_section is None:
        final_section = ET.SubElement(body, _q(W, "sectPr"))
    marker_specs = {section.marker: section for section in sections}
    current = SectionSpec("QF_DEFAULT")
    has_visible_content = False

    for paragraph in list(body.findall("w:p", NS)):
        text = _paragraph_text(paragraph).strip()
        if text == "QF_PAGE_BREAK":
            _clear_paragraph(paragraph)
            run = ET.SubElement(paragraph, _q(W, "r"))
            ET.SubElement(run, _q(W, "br"), {_q(W, "type"): "page"})
            has_visible_content = True
            continue
        spec = marker_specs.get(text)
        if spec is not None:
            if not has_visible_content:
                body.remove(paragraph)
            else:
                properties = _clear_paragraph(paragraph)
                old = properties.find("w:sectPr", NS)
                if old is not None:
                    properties.remove(old)
                break_type = "continuous" if spec.start == "continuous" else "nextPage"
                properties.append(_section_xml(current, final_section, break_type))
            current = spec
            continue
        if text:
            has_visible_content = True

    for node in document.findall(".//w:t", NS):
        if re.fullmatch(r"QF-Q-\d+", node.text or ""):
            node.text = ""

    replacement = _section_xml(current, final_section)
    index = list(body).index(final_section)
    body.remove(final_section)
    body.insert(index, replacement)


_FIELD_TOKENS = re.compile(r"(\{页码\}|\{总页数\}|\{标题\})")
_FIELD_CODES = {
    "{页码}": " PAGE ",
    "{总页数}": " NUMPAGES ",
    "{标题}": " DOCPROPERTY Title ",
}


def _append_text(paragraph: ET.Element, text: str) -> None:
    if not text:
        return
    run = ET.SubElement(paragraph, _q(W, "r"))
    node = ET.SubElement(run, _q(W, "t"))
    if text[:1].isspace() or text[-1:].isspace():
        node.set(_q(XML, "space"), "preserve")
    node.text = text


def _replace_field_paragraph(paragraph: ET.Element, value: str) -> None:
    _clear_paragraph(paragraph)
    for token in _FIELD_TOKENS.split(value):
        code = _FIELD_CODES.get(token)
        if code is None:
            _append_text(paragraph, token)
            continue
        field = ET.SubElement(paragraph, _q(W, "fldSimple"), {_q(W, "instr"): code})
        run = ET.SubElement(field, _q(W, "r"))
        ET.SubElement(run, _q(W, "t")).text = "1" if token != "{标题}" else value


def _patch_header_footer(parts: dict[str, bytes],
                         values: Mapping[str, str]) -> None:
    markers = {
        "QF_HEADER_LEFT": "header_left",
        "QF_HEADER_CENTER": "header_center",
        "QF_HEADER_RIGHT": "header_right",
        "QF_FOOTER_LEFT": "footer_left",
        "QF_FOOTER_CENTER": "footer_center",
        "QF_FOOTER_RIGHT": "footer_right",
    }
    for name in sorted(part for part in parts
                       if re.fullmatch(r"word/(?:header|footer)\d+\.xml", part)):
        root = _xml(parts, name)
        changed = False
        for paragraph in root.findall(".//w:p", NS):
            key = markers.get(_paragraph_text(paragraph).strip())
            if key is not None:
                _replace_field_paragraph(paragraph, str(values.get(key) or ""))
                changed = True
        if changed:
            parts[name] = _serialize(root)


def _fix_table_geometry(root: ET.Element, width: int = 9746) -> None:
    for table in root.findall(".//w:tbl", NS):
        properties = table.find("w:tblPr", NS)
        if properties is None:
            properties = ET.Element(_q(W, "tblPr"))
            table.insert(0, properties)
        _set_child(properties, "tblW", {"w": str(width), "type": "dxa"})
        _set_child(properties, "tblInd", {"w": "120", "type": "dxa"})
        _set_child(properties, "tblLayout", {"type": "fixed"})
        grid = table.find("w:tblGrid", NS)
        cells = table.findall(".//w:tr[1]/w:tc", NS)
        column_count = len(grid.findall("w:gridCol", NS)) if grid is not None else len(cells)
        column_count = max(1, column_count)
        base, remainder = divmod(width, column_count)
        column_widths = [base + (1 if index < remainder else 0)
                         for index in range(column_count)]
        if grid is None:
            grid = ET.Element(_q(W, "tblGrid"))
            table.insert(1, grid)
        for child in list(grid):
            grid.remove(child)
        for column_width in column_widths:
            ET.SubElement(grid, _q(W, "gridCol"), {_q(W, "w"): str(column_width)})
        for row in table.findall("w:tr", NS):
            for index, cell in enumerate(row.findall("w:tc", NS)):
                cell_properties = cell.find("w:tcPr", NS)
                if cell_properties is None:
                    cell_properties = ET.Element(_q(W, "tcPr"))
                    cell.insert(0, cell_properties)
                _set_child(cell_properties, "tcW", {
                    "w": str(column_widths[min(index, column_count - 1)]),
                    "type": "dxa",
                })


def _patch_core_title(parts: dict[str, bytes], title: str) -> None:
    root = _xml(parts, "docProps/core.xml")
    node = root.find("dc:title", NS)
    if node is None:
        node = ET.SubElement(root, _q(DC, "title"))
    node.text = title
    parts["docProps/core.xml"] = _serialize(root)


def _enable_field_updates(parts: dict[str, bytes]) -> None:
    if "word/settings.xml" not in parts:
        root = ET.Element(_q(W, "settings"))
    else:
        root = _xml(parts, "word/settings.xml")
    _set_child(root, "updateFields", {"val": "true"})
    parts["word/settings.xml"] = _serialize(root)


def _write_package_atomic(path: Path, parts: Mapping[str, bytes]) -> None:
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".part", dir=path.parent)
    os.close(handle)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in sorted(parts):
                archive.writestr(name, parts[name])
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def patch_docx(path: Path, *, title: str, sections: Sequence[SectionSpec],
               header_footer: Mapping[str, str]) -> None:
    """原子修补 DOCX；失败时不覆盖调用方已有的完整文件。"""
    path = Path(path)
    parts = _read_package(path)
    document = _xml(parts, "word/document.xml")
    _replace_layout_markers(document, sections)
    _fix_table_geometry(document)
    parts["word/document.xml"] = _serialize(document)

    _patch_header_footer(parts, header_footer)
    for name in sorted(part for part in parts
                       if re.fullmatch(r"word/(?:header|footer)\d+\.xml", part)):
        root = _xml(parts, name)
        _fix_table_geometry(root)
        parts[name] = _serialize(root)
    _patch_core_title(parts, str(title or "试卷"))
    _enable_field_updates(parts)
    _write_package_atomic(path, parts)
    validate_docx(path)
