# QuizForge Word 导出实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在题库首页现有组卷流程中增加可继续编辑、兼容 Microsoft Word 与 WPS 的 DOCX 导出，同时保持 PDF 预览和既有 PDF／TeX／ZIP 导出行为不变。

**Architecture:** `service_ports.py` 保持统一许可证边界，并把 `fmt=docx` 分派给新的 `word_exporter.py`；该模块独立生成语义化 Pandoc Markdown，不复用现有 raw LaTeX 文本。Pandoc 以受控的 `assets/word-reference.docx` 生成基础文档，`word_ooxml.py` 再定向处理分节、页眉页脚字段、图片上限和表格几何。

**Tech Stack:** Python 3.13、Flask、Pandoc、DOCX/OOXML（ZIP + `xml.etree.ElementTree`）、`python-docx`（仅开发期生成参考模板）、`unittest`、Microsoft Word、WPS、LibreOffice 渲染验收。

## Global Constraints

- 只修改 `软件版/` 题库首页“组卷导出”，独立讲义工作区不增加 DOCX。
- Word 导入保持“DOCX → PDF → 现有识别”路径不变。
- PDF 预览保持固定 `fmt=pdf`；DOCX 不提供浏览器预览。
- DOCX 支持首页当前全部模式：`exam`、`exam_std`、`note`、`lecture`、`slides`、`practice`、`list`、`handout`。
- DOCX 追求语义、结构和可编辑性，不与 PDF 做逐像素一致性比较。
- `paper_tone` 和现有 PDF 格式 WIMath 标志在 DOCX 下禁用并显示原因，不静默忽略。
- 不新增运行时 Python 依赖，不使用 Word COM、PDF 转 Word、TeX 转 Word或云端 Office。
- 运行时改动通过验证后执行 `update_installed.ps1 -DirectBundle`；用户未要求，不构建 Setup、不改版本号、不打 tag。
- 工作区已有结构化搜索和治理文档的未提交改动；每次只暂存本任务明确列出的文件，禁止 `git add -A`。

---

## 文件结构

- Create: `word_exporter.py` — Word 语义模型、正文清洗、表格/图片转换、Pandoc 调用和导出工作目录生命周期。
- Create: `word_ooxml.py` — DOCX ZIP/XML 的分节、字段、图片、表格几何和包完整性修补。
- Create: `tools/build_word_reference.py` — 使用开发期 `python-docx` 生成确定性的参考模板，不进入运行时调用链。
- Create: `assets/word-reference.docx` — 随程序分发的 Pandoc 样式模板。
- Create: `tests/test_word_exporter.py` — 纯语义渲染、内容转换、Pandoc 失败和真实 DOCX 集成测试。
- Create: `tests/test_word_ooxml.py` — OOXML 分节、字段、表格、图片和损坏包测试。
- Modify: `config.py` — 暴露 `WORD_REFERENCE_DOCX` 只读资源路径。
- Modify: `service_ports.py` — 在统一许可证门控后按格式分派 Word/PDF 导出器。
- Modify: `app.py` — 校验导出格式、保持 PDF 预览、返回 DOCX 下载 MIME/文件名。
- Modify: `templates/index.html` — 增加 DOCX 格式并同步禁用 PDF 专属设置。
- Modify: `build_desktop.ps1`、`tools/verify_desktop_bundle.py`、`update_installed.ps1` — 纳入新模块和参考模板。
- Modify: `README.md`、`docs/PRODUCT.md`、`CHANGELOG.md`、总路线规格 — 同步用户能力、边界与更新公告素材。

---

### Task 1: 建立 Word 语义模型和模式渲染

**Files:**
- Create: `word_exporter.py`
- Create: `tests/test_word_exporter.py`

**Interfaces:**
- Produces: `SectionSpec(marker: str, orientation: str, columns: int, start: str)`。
- Produces: `WordPlan(markdown: str, sections: tuple[SectionSpec, ...], image_widths: tuple[tuple[str, int], ...])`。
- Produces: `build_word_plan(questions, *, title, mode, keypoints="", fullpage_ids=None, solution_mode="none", std_opts=None, bank_subject="math") -> WordPlan`。
- Test helper: `sample_questions() -> list[dict]` 固定返回一道单选题和一道解答题，题目字典包含完整 `img_*` 字段。
- Consumes: 题目字典字段 `id/body/type/difficulty/solution/img_*` 和现有导出参数，不修改输入对象。

- [ ] **Step 1: 写模式、题号和解析位置的失败测试**

```python
class WordPlanTests(unittest.TestCase):
    def test_every_homepage_mode_builds_a_plan(self):
        for mode in sorted(word_exporter.SUPPORTED_MODES):
            with self.subTest(mode=mode):
                plan = word_exporter.build_word_plan(
                    sample_questions(), title="模式回归", mode=mode)
                self.assertIn("模式回归", plan.markdown)

    def test_standard_exam_has_title_info_sections_and_stable_numbers(self):
        plan = word_exporter.build_word_plan(
            sample_questions(), title="期中测试", mode="exam_std",
            solution_mode="separate",
            std_opts={"subject": "数学", "info_bar": True,
                      "secret_notice": "绝密★启用前", "exam_notes": "先写姓名",
                      "section_points": {"single": "5", "multi": "6",
                                         "blank": "5", "solve": ""}},
        )
        self.assertIn("期中测试", plan.markdown)
        self.assertIn("姓名", plan.markdown)
        self.assertIn("单选题", plan.markdown)
        self.assertIn("答案与解析", plan.markdown)
        self.assertEqual(plan.markdown.count("QF-Q-1"), 2)

    def test_practice_uses_native_two_column_section(self):
        plan = word_exporter.build_word_plan(
            sample_questions(), title="刷题", mode="practice")
        self.assertTrue(any(s.columns == 2 and s.start == "continuous"
                            for s in plan.sections))

    def test_slides_are_landscape_and_one_question_per_page(self):
        plan = word_exporter.build_word_plan(
            sample_questions(), title="课件", mode="slides")
        self.assertEqual(plan.sections[-1].orientation, "slides")
        self.assertEqual(plan.markdown.count("QF_PAGE_BREAK"), 1)
```

- [ ] **Step 2: 运行测试并确认因模块不存在而失败**

Run: `.venv\Scripts\python.exe -m unittest tests.test_word_exporter.WordPlanTests -v`

Expected: FAIL，错误包含 `ModuleNotFoundError: No module named 'word_exporter'`。

- [ ] **Step 3: 实现不可变语义模型、固定模式枚举和基础 Markdown 渲染**

```python
SUPPORTED_MODES = frozenset({
    "exam", "exam_std", "note", "lecture", "slides",
    "practice", "list", "handout",
})

@dataclass(frozen=True)
class SectionSpec:
    marker: str
    orientation: str = "portrait"
    columns: int = 1
    start: str = "newPage"

@dataclass(frozen=True)
class WordPlan:
    markdown: str
    sections: tuple[SectionSpec, ...]
    image_widths: tuple[tuple[str, int], ...] = ()

def build_word_plan(questions, *, title, mode, keypoints="",
                    fullpage_ids=None, solution_mode="none", std_opts=None,
                    bank_subject="math") -> WordPlan:
    if mode not in SUPPORTED_MODES:
        raise ExportError(f"Word 暂不支持导出模式：{mode}")
    if solution_mode not in {"none", "inline", "separate"}:
        raise ExportError("Word 解析位置无效")
    # 按原始题目顺序建立稳定题号，再由模式渲染标题、分区、题目和解析。
```

实现中使用 Pandoc custom-style 围栏绑定 `ExamTitle`、`QuestionType`、`Question`、`Solution` 样式；每道题加入内部标记 `QF-Q-<题号>`，后续 OOXML 阶段删除可见标记但保留题号关联。双栏刷题在标题后产生连续双栏 `SectionSpec`；16:9 课件产生横向 `slides` 分节并在题目之间插入 `QF_PAGE_BREAK`；文末解析新起单栏分节。

- [ ] **Step 4: 运行语义模型测试并确认通过**

Run: `.venv\Scripts\python.exe -m unittest tests.test_word_exporter.WordPlanTests -v`

Expected: PASS，且没有调用 Pandoc 或写入磁盘。

- [ ] **Step 5: 提交本任务**

```powershell
git add -- word_exporter.py tests/test_word_exporter.py
git commit -m "新增：建立 Word 语义渲染骨架"
```

---

### Task 2: 转换公式、表格和图片

**Files:**
- Modify: `word_exporter.py`
- Modify: `tests/test_word_exporter.py`
- Reuse: `export_tables.py`

**Interfaces:**
- Produces: `normalize_word_markdown(text: str) -> str`，保留 `$...$`/`$$...$$` 供 Pandoc 生成 OMML。
- Produces: `stage_word_images(questions: list[dict], work_dir: Path, stem: str) -> tuple[list[dict], tuple[tuple[str, int], ...]]`。
- Consumes: `export_tables.TABLE_RE/html_table_rows/pipe_text_cells/PIPE_SEP_RE`，不消费 `exporter.py` 生成的 LaTeX 表格令牌。

- [ ] **Step 1: 写公式、两类表格、缺图和多图布局失败测试**

```python
def test_math_and_html_table_remain_pandoc_semantics(self):
    text = ("已知 $x^2$。\n\n"
            "<table><tr><td>名称</td><td>值</td></tr>"
            "<tr><td>A</td><td>$1$</td></tr></table>")
    rendered = word_exporter.normalize_word_markdown(text)
    self.assertIn("$x^2$", rendered)
    self.assertIn("| 名称 | 值 |", rendered)
    self.assertNotIn("<table", rendered)
    self.assertNotIn("\\begin{tabular}", rendered)

def test_missing_image_reports_question_and_resource(self):
    with tempfile.TemporaryDirectory() as td, \
         mock.patch.object(config, "ASSETS_DIR", Path(td) / "assets"):
        with self.assertRaisesRegex(exporter.ExportError,
                                    "第 1 题.*missing.png"):
            word_exporter.stage_word_images(
                [{"id": "q1", "body": "![[missing.png]]", "solution": ""}],
                Path(td) / "work", "word_test")
```

- [ ] **Step 2: 运行内容转换测试并确认失败**

Run: `.venv\Scripts\python.exe -m unittest tests.test_word_exporter.WordContentTests -v`

Expected: FAIL，错误指出 `normalize_word_markdown` 或 `stage_word_images` 尚不存在。

- [ ] **Step 3: 实现 Word 专用清洗、表格重建和安全图片暂存**

```python
def normalize_word_markdown(text: str) -> str:
    text = _EXPORT_CONTROL_RE.sub("", str(text or "")).replace("\uf8f3", "")
    text = _html_tables_to_pipe_tables(text)
    return _normalize_pipe_tables(text)

def stage_word_images(questions, work_dir: Path, stem: str):
    work_dir.mkdir(parents=True, exist_ok=True)
    # 只允许从 config.ASSETS_DIR 解析普通文件；拒绝符号链接和越界路径。
    # 每个资源复制为 word_<随机>_img_<序号><后缀>，正文改成相对 Markdown 图片。
    # img_layouts/sol_img_layouts 决定 width 百分比、对齐和并排/上下意图。
```

HTML 与管道表格都重建为 Pandoc 原生管道表格；无法构成矩形时抛出包含题号的 `ExportError`。并排图片生成无表头 Markdown 布局表格，上下图片生成独立段落；单图宽度夹取到 10–100%，双栏模式的最终 OOXML 宽度仍以当前栏宽为上限。缺图不再沿用 PDF 路径的静默删除行为。

- [ ] **Step 4: 运行 Word 内容测试和既有表格测试**

Run: `.venv\Scripts\python.exe -m unittest tests.test_word_exporter.WordContentTests tests.test_export_tables -v`

Expected: PASS；公式定界符未栅格化，表格不含 raw LaTeX，缺图明确失败。

- [ ] **Step 5: 提交本任务**

```powershell
git add -- word_exporter.py tests/test_word_exporter.py
git commit -m "新增：转换 Word 公式表格与图片"
```

---

### Task 3: 生成参考模板并实现 OOXML 后处理

**Files:**
- Create: `tools/build_word_reference.py`
- Create: `assets/word-reference.docx`
- Create: `word_ooxml.py`
- Create: `tests/test_word_ooxml.py`
- Modify: `config.py`

**Interfaces:**
- Produces: `config.WORD_REFERENCE_DOCX = config.BASE_DIR / "assets" / "word-reference.docx"`。
- Produces: `patch_docx(path: Path, *, title: str, sections: Sequence[SectionSpec], header_footer: Mapping[str, str]) -> None`。
- Produces: `validate_docx(path: Path) -> None`，损坏或关系缺失时抛出 `exporter.ExportError`。
- Test helper: `build_minimal_docx(path: Path, *, marker="QF_SECTION_1", broken_media=False) -> Path` 用 `zipfile` 写入最小 `[Content_Types].xml`、关系、正文、页眉、页脚和可选损坏媒体关系。
- Test helper: `read_part(path: Path, name: str) -> bytes` 从 DOCX ZIP 读取指定部件。
- Consumes: Task 1 的 `SectionSpec` 和文档中的 `QF_SECTION_*`、`QF_PAGE_BREAK` 标记。

- [ ] **Step 1: 写分节、字段、表格几何和损坏包失败测试**

```python
def test_patch_adds_practice_columns_page_fields_and_fixed_tables(self):
    path = build_minimal_docx(self.temp_dir / "minimal-pandoc.docx")
    word_ooxml.patch_docx(
        path, title="刷题卷",
        sections=(SectionSpec("QF_SECTION_1", "portrait", 2, "continuous"),),
        header_footer={"header_left": "{标题}",
                       "footer_center": "第 {页码} / {总页数} 页"},
    )
    xml = read_part(path, "word/document.xml")
    self.assertIn(b'w:num="2"', xml)
    self.assertNotIn(b"QF_SECTION_1", xml)
    self.assertIn(b'w:type="dxa"', xml)
    footer = read_part(path, "word/footer1.xml")
    self.assertIn(b" PAGE ", footer)
    self.assertIn(b" NUMPAGES ", footer)

def test_validate_rejects_missing_media_relationship(self):
    path = build_minimal_docx(
        self.temp_dir / "broken.docx", broken_media=True)
    with self.assertRaisesRegex(exporter.ExportError, "DOCX 关系指向不存在"):
        word_ooxml.validate_docx(path)
```

- [ ] **Step 2: 运行 OOXML 测试并确认模块不存在**

Run: `.venv\Scripts\python.exe -m unittest tests.test_word_ooxml -v`

Expected: FAIL，错误包含 `ModuleNotFoundError: No module named 'word_ooxml'`。

- [ ] **Step 3: 按文档技能生成确定性参考模板**

在首次创建 DOCX 前调用工作区依赖定位，并按 `documents` 技能执行一次 `mark_artifact_operation_started.mjs --artifact-type docx --operation create`。`tools/build_word_reference.py` 固化名为 `quizforge_exam_document` 的版式覆盖：

```python
TOKENS = {
    "a4_page": (11906, 16838),
    "a4_margins": (1080, 1080, 1080, 1080),
    "header_footer_distance": 576,
    "a4_content_width": 9746,
    "body_font_east_asia": "宋体",
    "heading_font_east_asia": "黑体",
    "body_size_half_points": 21,
    "body_line": 300,
    "body_after": 80,
    "question_indent": 540,
    "question_hanging": 360,
    "table_indent": 120,
    "table_cell_margins": (80, 120, 80, 120),
}
```

模板定义 `ExamTitle`（18 pt 黑体居中）、`ExamSubtitle`（11 pt）、`QuestionType`（14 pt 黑体）、`Question`（10.5 pt 宋体、1.25 倍行距）、`Solution`（10 pt）、真实十进制编号定义，以及页眉页脚各一个三列表格，单栏表宽固定 9746 DXA。生成后用文档技能的 `render_docx.py` 渲染，逐页检查无缺字、裁切或表格错位，再提交二进制模板。

- [ ] **Step 4: 用标准库实现原子 OOXML 修补**

```python
def patch_docx(path: Path, *, title: str, sections, header_footer) -> None:
    with tempfile.TemporaryDirectory(dir=path.parent) as td:
        unpacked = Path(td) / "package"
        _safe_extract_docx(path, unpacked)
        _patch_core_title(unpacked, title)
        _replace_layout_markers(unpacked, sections)
        _patch_header_footer(unpacked, header_footer, title)
        _fix_table_geometry(unpacked)
        _cap_inline_images(unpacked)
        _write_atomic_docx(unpacked, path)
    validate_docx(path)
```

精确分节参数：A4 为 11906×16838 DXA、四边 1080 DXA；双栏 gutter 432 DXA；16:9 为 19199×10800 DXA、四边 720 DXA。`{页码}` 转 `PAGE` 字段、`{总页数}` 转 `NUMPAGES` 字段、`{标题}` 转 `DOCPROPERTY Title` 字段；设置 `w:updateFields w:val="true"`。ZIP 解包拒绝绝对路径和 `..`，写回先生成同目录临时文件再 `os.replace`。

- [ ] **Step 5: 运行 OOXML 测试并重新渲染模板**

Run: `.venv\Scripts\python.exe -m unittest tests.test_word_ooxml -v`

Expected: PASS；所有标记被删除、字段存在、表格 `tblW/tblGrid/tcW` 一致、损坏关系被拒绝。

Run: 使用 `documents` 技能的 `render_docx.py assets/word-reference.docx --output_dir <临时目录>`，检查生成的每一页 PNG。

Expected: 中文字体、编号、表格、页眉页脚占位布局正常，无裁切和异常空白页。

- [ ] **Step 6: 提交本任务**

```powershell
git add -- config.py word_ooxml.py tools/build_word_reference.py assets/word-reference.docx tests/test_word_ooxml.py
git commit -m "新增：实现 Word 分节与页眉页脚"
```

---

### Task 4: 接通 Pandoc 导出和许可证分派

**Files:**
- Modify: `word_exporter.py`
- Modify: `service_ports.py`
- Modify: `tests/test_word_exporter.py`
- Modify: `tests/test_desktop_product.py`

**Interfaces:**
- Produces: `word_exporter.export(questions, title="试卷", fmt="docx", mode="list", keypoints="", fullpage_ids=None, header_footer=None, solution_mode="none", std_opts=None, paper_tone="white", wimath_logo=False, bank_subject="math") -> Path`，签名与现有端口调用兼容。
- Consumes: `word_ooxml.patch_docx/validate_docx`、`config.PANDOC/WORD_REFERENCE_DOCX/OUTPUT_DIR`。
- Keeps: `service_ports.export_document(*args, **kwargs)` 是唯一许可证和本地/远程模式门控。
- Test helper: `patched_export_environment()` 创建临时输出目录与最小参考 DOCX，mock `subprocess.run` 写入最小 DOCX，并暴露收到的参数数组。
- Test helper: `run_export_with_failed_pandoc()` 让 mock Pandoc 写入 `.docx.part` 后返回非零码，用于验证清理。

- [ ] **Step 1: 写分派、Pandoc 参数、失败清理和并发隔离测试**

```python
def test_local_gateway_dispatches_docx_after_license_gate(self):
    with mock.patch.object(service_ports.word_exporter, "export",
                           return_value=Path("x.docx")) as call:
        result = service_ports.export_document([{"id": "q1"}], fmt="docx")
    self.assertEqual(result, Path("x.docx"))
    call.assert_called_once_with([{"id": "q1"}], fmt="docx")

def test_export_invokes_pandoc_with_reference_doc_and_argument_array(self):
    with patched_export_environment() as env:
        result = word_exporter.export(sample_questions(), title="含 空格", fmt="docx")
    self.assertEqual(result.suffix, ".docx")
    self.assertIn("--reference-doc", env.command)
    self.assertIn(str(config.WORD_REFERENCE_DOCX), env.command)
    self.assertNotIsInstance(env.command, str)

def test_pandoc_failure_removes_partial_docx(self):
    with self.assertRaisesRegex(exporter.ExportError, "Pandoc 生成 Word 失败"):
        run_export_with_failed_pandoc()
    self.assertFalse(any(config.OUTPUT_DIR.rglob("*.docx.part")))
```

- [ ] **Step 2: 运行分派和管线测试并确认失败**

Run: `.venv\Scripts\python.exe -m unittest tests.test_word_exporter.WordPipelineTests tests.test_desktop_product.ServicePortsTests -v`

Expected: FAIL，现有端口仍把 `docx` 交给 `exporter.export`。

- [ ] **Step 3: 实现独占工作目录、Pandoc 调用、修补和成功暴露**

```python
def export(questions, title="试卷", fmt="docx", mode="list", keypoints="",
           fullpage_ids=None, header_footer=None, solution_mode="none",
           std_opts=None, paper_tone="white", wimath_logo=False,
           bank_subject="math") -> Path:
    if fmt != "docx":
        raise ExportError("Word 导出器只接受 docx 格式")
    work_dir = config.OUTPUT_DIR / f"word_{timestamp}_{uuid.uuid4().hex}"
    work_dir.mkdir(parents=True)
    try:
        staged, image_widths = stage_word_images(questions, work_dir, "word")
        plan = build_word_plan(
            staged, title=title, mode=mode, keypoints=keypoints,
            fullpage_ids=fullpage_ids, solution_mode=solution_mode,
            std_opts=std_opts, bank_subject=bank_subject)
        markdown_path.write_text(plan.markdown, encoding="utf-8")
        _run_pandoc([
            config.PANDOC, str(markdown_path), "--from", "markdown+raw_attribute",
            "--reference-doc", str(config.WORD_REFERENCE_DOCX),
            "--resource-path", str(work_dir), "-o", str(part_path),
        ], cwd=work_dir)
        word_ooxml.patch_docx(part_path, title=title,
                              sections=plan.sections,
                              header_footer=header_footer or {})
        os.replace(part_path, output_path)
        return output_path
    except Exception:
        part_path.unlink(missing_ok=True)
        raise
```

成功产物继续留在独占目录供 `/outfile` 下载和 24 小时清理；只删除 Markdown、临时包和不再需要的暂存图片。失败删除整个本次工作目录。`subprocess.run` 始终传参数列表、`shell=False`、`cwd=work_dir`，并把 stderr 的安全摘要包装为 `ExportError`。

- [ ] **Step 4: 在许可证端口按格式分派**

```python
def export_document(*args, **kwargs):
    _require_local_export()
    if kwargs.get("fmt", "pdf") == "docx":
        return word_exporter.export(*args, **kwargs)
    return exporter.export(*args, **kwargs)
```

保持 `exporter.ExportError` 作为两条链路的公共错误类型，避免路由层扩大异常捕获。

- [ ] **Step 5: 运行管线、许可证和 PDF 回归测试**

Run: `.venv\Scripts\python.exe -m unittest tests.test_word_exporter.WordPipelineTests tests.test_desktop_product.ServicePortsTests tests.test_license_manager -v`

Expected: PASS；`docx` 走新模块，`pdf/tex/zip` 的 mock 断言保持不变。

- [ ] **Step 6: 提交本任务**

```powershell
git add -- word_exporter.py service_ports.py tests/test_word_exporter.py tests/test_desktop_product.py
git commit -m "新增：接入 DOCX 导出链"
```

---

### Task 5: 开放首页入口并纳入桌面发行资源

**Files:**
- Modify: `app.py`
- Modify: `templates/index.html`
- Modify: `tests/test_core_reliability.py`
- Modify: `build_desktop.ps1`
- Modify: `tools/verify_desktop_bundle.py`
- Modify: `update_installed.ps1`

**Interfaces:**
- Consumes: Task 4 的 `service_ports.export_document(..., fmt="docx")`。
- Produces: `/export` 返回 `.docx` 文件名和 `/outfile/<token>?dl=1` 下载地址。
- Keeps: `/preview` 无论格式选择都固定 PDF。

- [ ] **Step 1: 写界面、路由、MIME 和发行资源失败测试**

```python
def test_homepage_exposes_docx_and_marks_pdf_only_controls(self):
    page = app_module.app.test_client().get("/?all=1")
    self.assertIn(b'<option value="docx">Word', page.data)
    self.assertIn(b'id="paper-tone-field"', page.data)
    self.assertIn(b'id="wimath-logo-field"', page.data)

def test_docx_export_registers_office_filename_and_mime(self):
    produced = config.OUTPUT_DIR / "mock.docx"
    produced.write_bytes(b"docx-route-fixture")
    question = {"id": "q1", "body": "题干", "type": "填空题",
                "difficulty": "3", "solution": "", "img_align": "",
                "img_width": None, "img_split": None, "img_layouts": [],
                "sol_img_split": None, "sol_img_layouts": []}
    with mock.patch.object(app_module, "_collect_questions",
                           return_value=[question]), \
         mock.patch.object(app_module.service_ports, "export_document",
                           return_value=produced):
        response = app_module.app.test_client().post(
            "/export", data={"fmt": "docx", "title": "月考"},
            headers={"X-CSRF-Token": app_module._WRITE_TOKEN})
    download = app_module.app.test_client().get(response.get_json()["url"])
    self.assertEqual(download.mimetype,
                     "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    self.assertIn("月考.docx", download.headers["Content-Disposition"])
```

- [ ] **Step 2: 运行页面和路由测试并确认失败**

Run: `.venv\Scripts\python.exe -m unittest tests.test_core_reliability.PageTests.test_homepage_exposes_docx_and_marks_pdf_only_controls tests.test_core_reliability.PageTests.test_docx_export_registers_office_filename_and_mime -v`

Expected: FAIL，页面没有 DOCX 选项，下载 MIME 仍是默认值。

- [ ] **Step 3: 增加格式白名单、DOCX MIME 和界面联动**

`app.py` 将格式收敛为 `{"pdf", "tex", "zip", "docx"}`，非法格式返回 400，不传入导出器；`/outfile` 对 `.docx` 使用 Office MIME。`templates/index.html` 增加：

```html
<option value="docx">Word 文档（.docx，可继续编辑）</option>
```

增加 `syncExportFormatUI()`：选择 DOCX 时禁用 `paper_tone` 和 `wimath_logo` 控件，显示“Word 会自行重排；底色与 PDF 标志仅用于 PDF/TeX”的说明；切回其他格式恢复控件。预览按钮仍复制当前表单后执行 `fd.set('fmt', 'pdf')`。

- [ ] **Step 4: 将模块和模板加入目录版构建与扫描**

`build_desktop.ps1` 的 Nuitka 和 PyInstaller 参数都加入 `assets/word-reference.docx`；`tools/verify_desktop_bundle.py` 的必需资源加入该路径；`update_installed.ps1` 的 `$compileFiles` 加入 `word_exporter.py`、`word_ooxml.py`，模板存在性检查加入参考 DOCX。

- [ ] **Step 5: 运行页面、模板和发行扫描测试**

Run: `.venv\Scripts\python.exe -m unittest tests.test_core_reliability tests.test_desktop_product -v`

Run: `.venv\Scripts\python.exe -c "from app import app; [app.jinja_env.get_template(t) for t in app.jinja_env.list_templates()]"`

Run: `.venv\Scripts\python.exe -m py_compile app.py service_ports.py word_exporter.py word_ooxml.py tools\build_word_reference.py tools\verify_desktop_bundle.py`

Expected: 全部通过；预览仍向端口发送 `fmt=pdf`，非法格式返回 400，扫描器会拒绝缺少参考模板的发行目录。

- [ ] **Step 6: 提交本任务**

```powershell
git add -- build_desktop.ps1 tools/verify_desktop_bundle.py
git add -p -- app.py templates/index.html tests/test_core_reliability.py update_installed.ps1
git diff --cached --name-only
git commit -m "新增：开放首页 Word 导出入口"
```

逐块暂存时只选择 DOCX 相关区块；若 Word 与搜索改动落在同一 diff 区块，先用 `s` 拆分，仍无法安全拆分时本任务不提交该文件，并在交付中说明。

---

### Task 6: 建立真实 DOCX 回归与视觉验收

**Files:**
- Modify: `tests/test_word_exporter.py`
- Create: `tests/fixtures/word_export/representative_questions.json`
- Create: `tests/fixtures/word_export/images/diagram.png`

**Interfaces:**
- Consumes: 完整 `word_exporter.export`。
- Produces: 可重复生成的标准试卷、双栏刷题和 16:9 课件 DOCX 测试产物；夹具不包含真实用户题库数据。
- Test helper: `export_fixture(*, mode: str, solution_mode: str) -> Path` 从 `representative_questions.json` 和夹具图片复制到临时题库资源目录后调用完整导出器。

- [ ] **Step 1: 增加真实 Pandoc 集成测试**

```python
@unittest.skipUnless(word_exporter.pandoc_available(), "本机未安装 Pandoc")
def test_real_docx_roundtrip_keeps_text_math_tables_and_images(self):
    output = export_fixture(mode="exam_std", solution_mode="separate")
    word_ooxml.validate_docx(output)
    markdown = subprocess.run(
        [config.PANDOC, str(output), "-t", "markdown"],
        check=True, capture_output=True, text=True, encoding="utf-8",
    ).stdout
    self.assertIn("二次函数", markdown)
    self.assertGreaterEqual(markdown.count("$"), 4)
    self.assertIn("答案与解析", markdown)
```

- [ ] **Step 2: 运行真实 Pandoc 测试**

Run: `.venv\Scripts\python.exe -m unittest tests.test_word_exporter.WordPandocIntegrationTests -v`

Expected: 在当前开发机 Pandoc 可用时 PASS；若明确显示 skip，不能把它报告为已验证，需先修复工具路径再继续安装版同步。

- [ ] **Step 3: 生成三份代表性 DOCX 并逐页渲染**

生成 `exam_std + separate`、`practice + inline`、`slides + none` 三份临时 DOCX。使用 `documents` 技能的 `render_docx.py` 分别输出逐页 PNG，检查全部页面而非抽样：标题、中文、OMML 公式、真实表格、图片、题号、解析、分栏、横向页、分页和页眉页脚均无裁切、重叠、破表、缺字或异常空白页。

- [ ] **Step 4: 用 Word 与 WPS 做可编辑性验收**

分别打开标准试卷代表产物：编辑一道题干、一个公式、一个表格单元格和图片宽度，保存后重新打开。两端均不得出现“发现不可读内容／需要修复”提示。记录 Word/WPS 版本、页数差异和允许的自然重排差异，不以分页完全一致作为通过条件。

- [ ] **Step 5: 运行全量自动化回归**

Run: `.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"`

Run: `git diff --check`

Expected: 全量通过，Markdown 无空白错误；既有 PDF、TeX、ZIP 和讲义导出测试无倒退。

- [ ] **Step 6: 提交本任务**

```powershell
git add -- tests/test_word_exporter.py tests/fixtures/word_export/representative_questions.json tests/fixtures/word_export/images/diagram.png
git commit -m "测试：覆盖 Word 导出真实产物"
```

---

### Task 7: 同步产品文档并覆盖日常安装版

**Files:**
- Modify: `README.md`
- Modify: `docs/PRODUCT.md`
- Modify: `CHANGELOG.md`
- Modify: `docs/superpowers/specs/2026-08-16-commercial-evolution-roadmap-design.md`
- Modify: `docs/superpowers/specs/2026-08-16-word-export-design.md`
- Modify: root `../docs/STATUS.md`

**Interfaces:**
- Consumes: 已通过的源码、DOCX 和兼容性验证结果。
- Produces: `[Unreleased]` 更新公告素材、产品能力边界和准确状态快照。

- [ ] **Step 1: 更新用户文档和路线口径**

在 README 增加“组卷可导出可编辑 Word”；PRODUCT 的“组卷导出”板块写明 PDF 是印刷权威、DOCX 是可编辑语义版；CHANGELOG `[Unreleased]` 的“新增”记录各组卷模式可导出 DOCX，“已知限制”记录 Word/WPS 自然重排及底色/WIMath 标志仅用于 PDF。总路线删除旧的模式限制，Word 规格状态改为“已确认并实施”。

- [ ] **Step 2: 更新状态文档且不虚构验证**

根级 `docs/STATUS.md` 只记录实际完成的自动化、Word/WPS、渲染和安装版验收；未执行项明确写“未执行”，不得写成通过。

- [ ] **Step 3: 做最终源码验证**

Run: `.venv\Scripts\python.exe -m py_compile app.py service_ports.py word_exporter.py word_ooxml.py tools\build_word_reference.py tools\verify_desktop_bundle.py`

Run: `.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"`

Run: `.venv\Scripts\python.exe -c "from app import app; [app.jinja_env.get_template(t) for t in app.jinja_env.list_templates()]"`

Run: `git diff --check`

Expected: 全部通过。

- [ ] **Step 4: 提交文档**

```powershell
git add -- docs/superpowers/specs/2026-08-16-commercial-evolution-roadmap-design.md docs/superpowers/specs/2026-08-16-word-export-design.md
git add -p -- README.md docs/PRODUCT.md CHANGELOG.md
git diff --cached --name-only
git commit -m "文档：记录 Word 导出能力"
```

执行前核对暂存 diff，确保未把结构化搜索和治理体系的既有改动误并入本提交。根级 `../docs/STATUS.md` 位于软件版 Git 仓库之外，直接保留工作区更新，不放进上述提交。

- [ ] **Step 5: 原位覆盖日常安装版**

关闭所有 QuizForge 窗口，执行：

```powershell
.\update_installed.ps1 -DirectBundle
```

Expected: 目录版构建与覆盖成功，受保护数据哈希不变，默认安装目录中的 `QuizForge.exe` 启动后 `/healthz` 返回 `status=ok`。

- [ ] **Step 6: 验收安装版 Word 导出**

在安装版首页选择两道含公式、表格和图片的题，分别导出标准试卷和双栏刷题 DOCX；确认下载名正确、Word/WPS 可打开且内容可编辑。记录安装路径、健康端口和产物检查结果。默认不运行 `build_installer.ps1`。

---

## 完成判据

- 首页可选择 DOCX，所有现有组卷模式都能生成结构正确的 Word 文档。
- 公式为可编辑 OMML，表格和题号是 Word 原生结构，图片可单独编辑。
- 三种解析位置、页眉页脚字段、双栏和 16:9 横向模式通过结构与视觉验收。
- 缺失 Pandoc、模板、图片或损坏 DOCX 都返回明确错误且没有半成品。
- Word/WPS 实测可编辑保存，不出现修复提示；允许自然换页差异。
- PDF 预览及 PDF／TeX／ZIP 导出全量回归通过。
- `DirectBundle` 已同步日常安装版并通过健康与功能验收。
- Setup 安装包未构建，版本号和 tag 未改变。
