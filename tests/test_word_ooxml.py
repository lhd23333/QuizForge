"""Word OOXML 分节、字段与包完整性测试。"""

from pathlib import Path
import re
import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile

import config
import word_ooxml
from word_exporter import SectionSpec


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS = {"w": W, "r": R}


def _content_types() -> bytes:
    return b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/header1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"/>
  <Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>
  <Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
</Types>"""


def build_minimal_docx(path: Path, *, marker: str = "QF_SECTION_PRACTICE",
                       broken_media: bool = False) -> Path:
    document_rels = f"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/header" Target="header1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer1.xml"/>
  {('<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/missing.png"/>' if broken_media else '')}
</Relationships>""".encode("utf-8")
    parts = {
        "[Content_Types].xml": _content_types(),
        "_rels/.rels": b"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
</Relationships>""",
        "word/document.xml": f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="{W}" xmlns:r="{R}"><w:body>
  <w:p><w:r><w:t>标题</w:t></w:r></w:p>
  <w:p><w:r><w:t>{marker}</w:t></w:r></w:p>
  <w:p><w:r><w:t>QF-Q-1</w:t></w:r><w:r><w:t>题干</w:t></w:r></w:p>
  <w:tbl><w:tblPr/><w:tblGrid><w:gridCol/><w:gridCol/></w:tblGrid>
    <w:tr><w:tc><w:tcPr/><w:p><w:r><w:t>甲</w:t></w:r></w:p></w:tc>
    <w:tc><w:tcPr/><w:p><w:r><w:t>乙</w:t></w:r></w:p></w:tc></w:tr>
  </w:tbl>
  <w:sectPr><w:headerReference w:type="default" r:id="rId1"/>
    <w:footerReference w:type="default" r:id="rId2"/></w:sectPr>
</w:body></w:document>""".encode("utf-8"),
        "word/_rels/document.xml.rels": document_rels,
        "word/header1.xml": f"""<?xml version="1.0" encoding="UTF-8"?>
<w:hdr xmlns:w="{W}"><w:p><w:r><w:t>QF_HEADER_LEFT</w:t></w:r></w:p>
<w:p><w:r><w:t>QF_HEADER_CENTER</w:t></w:r></w:p>
<w:p><w:r><w:t>QF_HEADER_RIGHT</w:t></w:r></w:p></w:hdr>""".encode("utf-8"),
        "word/footer1.xml": f"""<?xml version="1.0" encoding="UTF-8"?>
<w:ftr xmlns:w="{W}"><w:p><w:r><w:t>QF_FOOTER_LEFT</w:t></w:r></w:p>
<w:p><w:r><w:t>QF_FOOTER_CENTER</w:t></w:r></w:p>
<w:p><w:r><w:t>QF_FOOTER_RIGHT</w:t></w:r></w:p></w:ftr>""".encode("utf-8"),
        "word/settings.xml": f"<w:settings xmlns:w=\"{W}\"/>".encode("utf-8"),
        "docProps/core.xml": b"""<?xml version="1.0" encoding="UTF-8"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title/></cp:coreProperties>""",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    return path


def read_part(path: Path, name: str) -> bytes:
    with zipfile.ZipFile(path) as archive:
        return archive.read(name)


class WordOoxmlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.temp_dir = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_reference_template_is_a_packaged_docx(self):
        self.assertEqual(config.WORD_REFERENCE_DOCX.name, "word-reference.docx")
        self.assertTrue(config.WORD_REFERENCE_DOCX.is_file())
        self.assertTrue(zipfile.is_zipfile(config.WORD_REFERENCE_DOCX))

    def test_patch_adds_practice_columns_page_fields_and_fixed_tables(self):
        path = build_minimal_docx(self.temp_dir / "minimal.docx")

        word_ooxml.patch_docx(
            path,
            title="刷题卷",
            sections=(
                SectionSpec("QF_SECTION_PRACTICE", "portrait", 2, "continuous"),
            ),
            header_footer={
                "header_left": "{标题}",
                "footer_center": "第 {页码} / {总页数} 页",
            },
        )

        document = ET.fromstring(read_part(path, "word/document.xml"))
        self.assertNotIn("QF_SECTION_PRACTICE", "".join(document.itertext()))
        self.assertNotIn("QF-Q-1", "".join(document.itertext()))
        column_counts = [node.get(f"{{{W}}}num", "1")
                         for node in document.findall(".//w:sectPr/w:cols", NS)]
        self.assertIn("2", column_counts)
        table_width = document.find(".//w:tbl/w:tblPr/w:tblW", NS)
        self.assertEqual(table_width.get(f"{{{W}}}type"), "dxa")
        self.assertEqual(table_width.get(f"{{{W}}}w"), "9746")
        grid_widths = [int(node.get(f"{{{W}}}w"))
                       for node in document.findall(".//w:tblGrid/w:gridCol", NS)]
        self.assertEqual(sum(grid_widths), 9746)

        footer = ET.fromstring(read_part(path, "word/footer1.xml"))
        instructions = [node.get(f"{{{W}}}instr")
                        for node in footer.findall(".//w:fldSimple", NS)]
        self.assertIn(" PAGE ", instructions)
        self.assertIn(" NUMPAGES ", instructions)
        header = ET.fromstring(read_part(path, "word/header1.xml"))
        header_instructions = [node.get(f"{{{W}}}instr")
                               for node in header.findall(".//w:fldSimple", NS)]
        self.assertIn(" DOCPROPERTY Title ", header_instructions)

    def test_validate_rejects_missing_media_relationship(self):
        path = build_minimal_docx(
            self.temp_dir / "broken.docx", broken_media=True)

        with self.assertRaisesRegex(word_ooxml.ExportError, "关系指向不存在"):
            word_ooxml.validate_docx(path)

    def test_patch_never_leaves_undeclared_ignorable_prefixes(self):
        path = self.temp_dir / "reference-copy.docx"
        shutil.copy2(config.WORD_REFERENCE_DOCX, path)

        word_ooxml.patch_docx(
            path,
            title="命名空间回归",
            sections=(SectionSpec("QF_UNUSED"),),
            header_footer={"footer_center": "第 {页码} 页"},
        )

        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if not name.endswith(".xml"):
                    continue
                xml = archive.read(name).decode("utf-8")
                match = re.search(r'(?:mc|ns\d+):Ignorable="([^"]+)"', xml)
                if match:
                    for prefix in match.group(1).split():
                        self.assertIn(
                            f"xmlns:{prefix}=", xml,
                            f"{name} 的 mc:Ignorable 引用了未声明前缀 {prefix}",
                        )
                for prefix in re.findall(r'xsi:type="([A-Za-z][\w.-]*):', xml):
                    self.assertIn(
                        f"xmlns:{prefix}=", xml,
                        f"{name} 的 xsi:type 引用了未声明前缀 {prefix}",
                    )


if __name__ == "__main__":
    unittest.main()
