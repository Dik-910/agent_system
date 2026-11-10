#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
手册 RAG 检索模块（手册感知版 v2）

把广立微 Scan 引擎用户手册（PDF）按**编号章节**切块，建立带 IDF 权重的
关键词倒排索引。运行时按查询（命令名 / 错误码 / 选项名 / 中文需求描述）
检索相关手册段落，注入 LLM 上下文。

相对 v1 的升级（v1 为「按空行分段」）：
  1. **章节感知切块**：识别 `2.1.5.4 输出 SCANDEF 文件` 这类标题，
     一个章节 = 一个 chunk，并记录章节号与 PDF 页码。
     v1 按空行切会把「章节标题」和「正文」切散，导致检索返回邻块。
  2. **章节标题入索引**：标题里的中英文词也建索引，使「输出 SCANDEF」
     这类查询能直接命中对应小节。
  3. **IDF 加权打分**：罕见关键词（dump_def / SCAN-0620）权重高于
     高频泛词（set_scan_signal 在手册出现 77 次），修正 v1 的纯计数偏差。
  4. **中文 n-gram 索引 + 自动停用词**：高频中文 n-gram（用户/可以/使用）
     按文档频率自动剪枝，等价于自动停用词表。
  5. **缓存指纹校验**：缓存文件记录 PDF 的 名称+大小+mtime，手册被替换
     时自动重建，避免 v1 的陈旧缓存问题。
  6. **检索片段带出处**：返回 `【手册 · 2.1.5.4 输出 SCANDEF 文件 (p64)】`，
     让 LLM 知道结论来自手册哪一节。

设计要点：
  1. 零外部服务依赖：纯本地关键词 + 正则索引，评测环境网络受限也能稳定工作。
  2. 优雅降级：手册 PDF 缺失 / 无法解析时，RAG 返回空，不影响主流程。
  3. 索引缓存：首次解析后缓存为 .json，避免重复解析 PDF。

手册放置位置：
  将 PDF 放在 scan_agent/submission/manual/ 目录下（任意文件名 *.pdf），
  随代码打包进镜像。运行时自动发现并建立索引。
"""

import json
import math
import os
import re
from pathlib import Path

try:
    from . import config  # noqa: F401  （保持与原模块一致的导入副作用）
except ImportError:
    try:
        import config  # noqa: F401
    except ImportError:
        pass


# ============================================================================
# 配置
# ============================================================================

# 手册目录（相对 submission 包目录）
MANUAL_DIR = Path(__file__).parent / "manual"

# 索引缓存文件名
INDEX_CACHE = MANUAL_DIR / ".index_cache.json"

# 检索返回的最大 chunk 数。
# 为什么是 3 而不是 5：检索片段直接进 LLM prompt，而 LLM 单次生成耗时与输入量
# 正相关 —— 150s 预算的 case 预运行上限只有 90s，片段给太多会把生成推出预算
# （实测 5 段 × 1800 字符 = 8737 字符，占整个 user prompt 的一半）。
# 3 段足以覆盖「本 case 要用的命令」的语法小节，且多为标题命中的精准小节。
TOP_K = int(os.environ.get("RAG_TOP_K", "3"))

# 每个 chunk 的最大字符数
CHUNK_MAX_CHARS = int(os.environ.get("RAG_CHUNK_SIZE", "2000"))

# 单个片段返回给 LLM 时的最大字符数（防止 prompt 被撑爆）
SNIPPET_MAX_CHARS = int(os.environ.get("RAG_SNIPPET_CHARS", "2000"))

# 索引结构版本；不兼容升级时递增，旧缓存自动失效
# v3：新增 manual/**/*.md（工具自带的 additional_cmds 命令文档，如 add_inst/connect_net）
# v4：md 文档按 `## 命令名` 切块 + 文件名作为标题别名入索引
#     （v3 把整篇 md 当一个「页」，标题是 `### JsonConfig` 这类垃圾标题，
#      命令名只出现在正文里，被 PDF 正文的稀释后检索命中率极低）
INDEX_VERSION = 4

# md 命令文档的标题加成系数（相对 PDF 章节）。
# 为什么单独加权：additional_cmds/*.md 是**命令的权威定义**（参数、Syntax、示例），
# 而 PDF 手册正文里同名命令只是被顺带提及。实测同一查询下 PDF 的
# 「2.2.4 对多个设计进行 Scan 插入」以 31.0 分压过 md 的定义节，导致
# LLM 只看到一句示例、看不到 -from/-to 的写法。
MD_TITLE_BONUS_MUL = 2.0

# 高文档频率词剪枝阈值（占比超过该值的词视为停用词）
_STOPWORD_DF_RATIO = 0.40

# 章节标题命中加成倍数。
# 纯命令名查询（如 dump_ctl）在正文里到处出现，单靠正文 IDF 无区分度；
# 而「命令名与章节标题同现」几乎总是指向该命令的正式定义小节（如
# '2.1.5.3 输出 CTL 文件'），因此对标题命中的 chunk 额外加权。
_TITLE_BONUS = 2.5


# ============================================================================
# PDF 文本抽取（逐页，保留页码）
# ============================================================================

def _extract_pdf_pages(pdf_path):
    """
    从 PDF 逐页抽取文本。

    Returns:
        list[(page_no, text)]   page_no 为 1-based PDF 页号；失败返回 []
    """
    # 方法 1：pypdf
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(pdf_path))
        pages = []
        for i, page in enumerate(reader.pages):
            pages.append((i + 1, page.extract_text() or ""))
        if any(t.strip() for _, t in pages):
            return pages
    except Exception:
        pass

    # 方法 2：pdftotext 命令行（无页号信息，整体作为一页）
    try:
        import subprocess
        proc = subprocess.run(
            ["pdftotext", "-layout", str(pdf_path), "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=180, text=True,
        )
        if proc.stdout and proc.stdout.strip():
            return [(1, proc.stdout)]
    except Exception:
        pass

    return []


def _extract_pdf_text(pdf_path):
    """兼容旧接口：返回全文 str。"""
    return "\n\n".join(t for _, t in _extract_pdf_pages(pdf_path))


# ============================================================================
# 章节切分
# ============================================================================

# 章节标题：  '2.1.5.4 输出 SCANDEF 文件'  /  '2 运行 Scan 引擎'
_HEADING_RE = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,2}){0,4})\s+(\S[^\n]{0,60})$")

# 目录行的点线引导符
_TOC_DOTS_RE = re.compile(r"\.{4,}")

# 明显不是标题的起始字符（代码注释、引用等）
_BAD_TITLE_START = ("//", "#", "\\", "\"", "“", "'", ">", "-", "*")


def _match_heading(line):
    """
    判断一行是否为章节标题。是则返回 (章节号, 标题)，否则返回 None。

    过滤策略（这一步的准确性直接决定切块质量）：
      - 排除目录行（含连续点线）
      - 标题必须含字母或汉字（排除 '32   32' 这类表格数据行）
      - 标题内部不得有连续空格/制表符（排除 '4  data_in_n  none ...' 数据行）
      - 排除以代码注释符开头的伪标题（如 '1  //Case-2: ...'）
    """
    m = _HEADING_RE.match(line)
    if not m:
        return None
    num, title = m.group(1), m.group(2).strip()
    if _TOC_DOTS_RE.search(line):
        return None
    if not re.search(r"[A-Za-z\u4e00-\u9fff]", title):
        return None
    if title.startswith(_BAD_TITLE_START):
        return None
    if "  " in title or "\t" in title:
        return None
    if len(title) > 40:
        return None
    return num, title


def _split_sections(pages):
    """
    把逐页文本切成章节。

    Returns:
        list[dict]: [{section, title, page, text}]
    """
    sections = []
    cur = None

    for pno, ptext in pages:
        for line in ptext.splitlines():
            # 目录行的点线引导符：'2.1.5.4 输出 SCANDEF 文件 ....... 64'
            # 这类行把「全部章节标题」堆在一起，会让任何标题查询都命中目录页，
            # 是纯粹的检索噪声，必须剔除。
            if _TOC_DOTS_RE.search(line):
                continue
            hit = _match_heading(line)
            if hit:
                cur = {"section": hit[0], "title": hit[1], "page": pno, "lines": []}
                sections.append(cur)
            else:
                if cur is None:
                    cur = {"section": "", "title": "封面与目录", "page": pno, "lines": []}
                    sections.append(cur)
                cur["lines"].append(line)

    out = []
    for s in sections:
        body = "\n".join(s["lines"]).strip()
        head = (f"{s['section']} {s['title']}").strip()
        if not body and not s["section"]:
            continue
        out.append({
            "section": s["section"],
            "title": s["title"],
            "page": s["page"],
            "head": head,
            "body": body,
        })
    return out


def _assign_sibling_ranks(chunks):
    """
    给「同源同节」的多个分段编号（rank=None 表示该节未被切分）。

    检索阶段据此做「同节只取一段」去重：宁可给两个不同命令各一段，
    也不要把 3 个名额全花在同一个命令文档的 3 个分段上。
    """
    seen = {}
    for c in chunks:
        key = (c.get("source"), c.get("section"), c.get("title"))
        seen[key] = seen.get(key, 0) + 1
    counter = {}
    for c in chunks:
        key = (c.get("source"), c.get("section"), c.get("title"))
        if seen[key] <= 1:
            c["sibling_rank"] = None
            continue
        counter[key] = counter.get(key, -1) + 1
        c["sibling_rank"] = counter[key]


def _split_md_sections(text, doc_name):
    """
    markdown 命令文档 -> 章节。

    additional_cmds/*.md 的结构固定为：

        ## add_inst          <- 命令名（一级语义标题）
        ### JsonConfig
        ### Argument
        **name_list**
        ...
        - Syntax
        **reference_name**
        ...

    不能直接喂给 `_split_sections`：它只认「数字 标题」，而这些 md 的标题是
    `### JsonConfig` / `### Argument`，切出来的 section 号为空、标题是
    「JsonConfig」——命令名 `add_inst` 反而不在标题里，拿不到标题加成，
    检索时被 PDF 正文的小节压过去。

    这里把 `## xxx` 作为章节标题（section 用文档名，便于提示出处），
    无 `##` 时整篇作一节，标题退回文件名（add_inst.md -> add_inst）。
    """
    secs, cur = [], None
    for line in text.splitlines():
        m = re.match(r"^##\s+(\S.*?)\s*$", line)
        if m:
            # `## add_inst   —— Creates leaf or hierarchical insts ...`：
            # 标题里的破折号后是**一句话简介**（briefInfo/description），
            # 拆成 title=命令名 + body 首行简介，检索时命令名才能精确命中标题
            head_txt = m.group(1)
            desc = ""
            for sep in ("——", "—", "--", " - "):
                if sep in head_txt:
                    head_txt, desc = head_txt.split(sep, 1)
                    break
            cur = {"section": doc_name, "title": head_txt.strip(),
                   "page": 1, "lines": [desc.strip()] if desc.strip() else []}
            secs.append(cur)
        else:
            if cur is None:
                cur = {"section": doc_name,
                       "title": Path(doc_name).stem,
                       "page": 1, "lines": []}
                secs.append(cur)
            # `###` 子标题保留为正文的一行：JsonConfig/Argument/Syntax/Example
            # 这些词是**语义结构**，LLM 需要看到才知道哪段是参数表、哪段是示例。
            cur["lines"].append(line)

    out = []
    for s in secs:
        body = "\n".join(s["lines"]).strip()
        head = s["title"]
        if not body and not head:
            continue
        out.append({"section": s["section"], "title": s["title"],
                    "page": s["page"], "head": head, "body": body})
    return out


def _split_into_chunks(text):
    """兼容旧接口：按空行聚合成块（仅在非章节模式下使用）。"""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks, cur = [], ""
    for p in paragraphs:
        if len(cur) + len(p) + 2 > CHUNK_MAX_CHARS and cur:
            chunks.append(cur)
            cur = p
        else:
            cur = p if not cur else cur + "\n\n" + p
    if cur:
        chunks.append(cur)
    return chunks


def _slice_long(body, limit):
    """把过长正文按行切成若干段，每段不超过 limit 字符。"""
    parts, cur = [], ""
    for line in body.splitlines():
        if len(cur) + len(line) + 1 > limit and cur:
            parts.append(cur.rstrip())
            cur = line
        else:
            cur = line if not cur else cur + "\n" + line
    if cur.strip():
        parts.append(cur.rstrip())
    return parts or [""]


def _build_chunks(sections, source):
    """
    章节 -> chunk。超长章节按行切分，每段都带上章节标题作为上下文。

    Returns:
        list[dict]: [{id, text, section, title, page, source}]
    """
    chunks = []
    for s in sections:
        head, body = s["head"], s["body"]
        if len(head) + len(body) + 1 <= CHUNK_MAX_CHARS:
            chunks.append({"section": s["section"], "title": s["title"],
                           "page": s["page"], "text": f"{head}\n{body}".strip()})
            continue
        for part in _slice_long(body, CHUNK_MAX_CHARS - len(head) - 1):
            chunks.append({"section": s["section"], "title": s["title"],
                           "page": s["page"], "text": f"{head}\n{part}".strip()})

    for i, c in enumerate(chunks):
        c["id"] = i
        c["source"] = source
    return chunks


# ============================================================================
# 关键词提取
# ============================================================================

# 命令名：dftexp_scan 的 TCL 命令（前缀 set_/load_/present_/insert_/examine_/rpt_/dump_/add_ 等）
# 必须包含 ECO 编辑命令族：connect_pin / connect_net / disconnect_net / rewire / get_obj_*。
# 漏掉它们的后果不是「少一个词」—— `connect_pin` 只被拆成 connect + pin，查询侧
# 拿不到整体命令名，标题加成永远落空（实测 PDF 的无关小节压在 md 定义节前面）。
_COMMAND_RE = re.compile(
    r"\b((?:set|load|present|insert|examine|rpt|dump|add|remove|check|report|write|read|delete"
    r"|connect|disconnect|rewire|get|create|source|trim)"
    r"_[\w]+)\b"
)

# 错误码：SCAN-xxxx / COM-xxxx / CMD-xxxx / DFTDRC-xxxx
_ERROR_CODE_RE = re.compile(r"\b((?:SCAN|COM|CMD|DFTRC|DFT)-\d+)\b")

# DRC 规则号：仅手册 2.1.3.3 认可的形式（DFTR1-DFTR17 / DFTR-TIEn / DFTR-Ln）
_DRC_RULE_RE = re.compile(r"\b(DFTR-?TIE\d|DFTR-?L\d|DFTR\d+)\b")

# 选项名：-xxx
_OPTION_RE = re.compile(r"(-[a-z_][a-z_\d]*)\b")

# 拉丁技术标识符（含下划线，排除普通英文单词以免稀释索引）
_IDENT_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*_[A-Za-z0-9_]+)\b")

# 普通英文词（标题里的 Scan / Clock / Wrapper 等）
_WORD_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9]{2,})\b")

# 连续汉字串
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")

# 中文功能词（进一步过滤，避免"用户/可以"这类词靠 n-gram 漏过剪枝）
_CJK_STOP = {
    "用户", "可以", "使用", "进行", "通过", "如下", "以下", "对于", "其中",
    "需要", "支持", "设计", "命令", "参数", "文件", "当前", "如果", "则不",
    "上述", "所示", "示例", "该命", "将在", "指定", "包含", "以及", "或者",
    "默认", "情况", "示例", "说明", "一个", "这个", "某个", "其他", "所有",
}


def _cjk_ngrams(text, ns=(2, 3)):
    """提取连续汉字串的 n-gram。"""
    out = set()
    for m in _CJK_RUN_RE.finditer(text):
        s = m.group(0)
        for n in ns:
            for i in range(len(s) - n + 1):
                g = s[i:i + n]
                if g not in _CJK_STOP:
                    out.add(g)
    return out


def _extract_keywords(text, title=""):
    """从 chunk 文本 + 章节标题提取关键词（用于建倒排索引）。"""
    kws = set()
    for rx in (_COMMAND_RE, _ERROR_CODE_RE, _DRC_RULE_RE, _OPTION_RE):
        kws.update(m.group(1).lower() for m in rx.finditer(text))

    for m in _IDENT_RE.finditer(text):
        w = m.group(1).lower()
        if 4 <= len(w) <= 60:
            kws.add(w)

    # 标题权重高：标题里的英文词与中文 n-gram 全部入索引
    if title:
        kws.update(m.group(1).lower() for m in _WORD_RE.finditer(title))
        kws.update(t.lower() for t in _cjk_ngrams(title))

    # 复合标识符「整体」也要入索引：命令名/信号名本身就是 `add_inst` 这种
    # 带下划线的整体，_WORD_RE 只抓 `inst`，会让「查 add_inst」时标题加成落空。
    # 只补查询侧会产出的整体词（与 _query_keywords 同规则），不额外拆分。
    if text or title:
        for m in _COMMAND_RE.finditer(f"{title}\n{text}"):
            kws.add(m.group(1).lower())

    return kws


def _trim_around_keywords(body, kws, limit):
    """
    超长正文截断时，以**关键词首次出现的位置为中心**取窗口，而不是从头截。

    为什么必须这样做（2026-11-02 核对手册覆盖时发现）：`remove_scan_chain`
    在 2.1.2.4 章的检索分是 10.09（命中很准），但整章合并后 1930 字，
    从头截到 2000 字正好把命令原文切掉 —— 检索"命中了"，模型却看不到那条命令。
    命令名往往出现在章节中后部，从头截等于白检索。
    """
    if not body or len(body) <= limit:
        return body
    pos = -1
    low = body.lower()
    for kw in (kws or ()):
        i = low.find(str(kw).lower())
        if i >= 0 and (pos < 0 or i < pos):
            pos = i
    if pos < 0:
        return body[:limit] + " …（截断）"
    start = max(0, min(pos - limit // 3, len(body) - limit))
    head = "…" if start > 0 else ""
    tail = "…（截断）" if start + limit < len(body) else ""
    return head + body[start:start + limit] + tail


def _query_keywords(query):
    """从查询文本提取关键词（与索引侧保持同一套规则）。"""
    kws = set()
    for rx in (_COMMAND_RE, _ERROR_CODE_RE, _DRC_RULE_RE, _OPTION_RE):
        kws.update(m.group(1).lower() for m in rx.finditer(query))

    for m in _IDENT_RE.finditer(query):
        w = m.group(1).lower()
        if 4 <= len(w) <= 60:
            kws.add(w)

    kws.update(m.group(1).lower() for m in _WORD_RE.finditer(query))
    kws.update(_cjk_ngrams(query))
    return kws


def _title_probe_keywords(query):
    """
    章节标题加成**专用**的关键词集。

    在常规查询关键词之外，额外加入复合命令名的**末段**
    （dump_ctl -> ctl、rpt_scan_element -> element），
    用于命中那些只写缩写的章节标题（如 '2.1.5.3 输出 CTL 文件'）。

    刻意只取「末段」而非全部子词：若把 set_scan_cell_mapping 拆成
    set/scan/cell/mapping，'scan' 会让「配置 Scan Chain」等无关小节也获得加成；
    同理 insert_dft_logic 的 'dft' 会命中「定义 DFT Signal」。只取末段
    （mapping / logic）在这些场景下都无命中，不会引入误伤。
    """
    kws = set(_query_keywords(query))
    for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", query):
        if "_" in tok:
            last = tok.rsplit("_", 1)[-1].lower()
            if len(last) >= 3:
                kws.add(last)
    return kws


# ============================================================================
# 索引构建
# ============================================================================

def _rel(p):
    """相对 MANUAL_DIR 的路径（多级目录下文件名可能重名，指纹要用相对路径）。"""
    try:
        return p.relative_to(MANUAL_DIR).as_posix()
    except Exception:
        return p.name


def _manual_files():
    """
    手册源文件：PDF 用户手册 + 工具自带的 markdown 命令文档。

    为什么要把 md 也索引：更正材料 1012 在工具目录 `md/` 下给了 20 个 ECO 命令的
    说明（add_inst / add_net / connect_net / connect_pin / disconnect_net / rewire …）。
    只索引 PDF 的话，这些命令对 LLM 完全不可见 —— 而新 task2/case6 的正确解**只能**
    用 ECO 命令落地（缺陷在网表，改 dofile 声明清不了 DRC）。把它们放进
    submission/manual/ 后随镜像打包，这里自动发现。
    """
    if not MANUAL_DIR.exists():
        return []
    found = [p for p in MANUAL_DIR.rglob("*")
             if p.is_file() and p.suffix.lower() in (".pdf", ".md")]
    # 缓存文件与说明性 README 不参与索引
    found = [p for p in found
             if p.name not in ("README.md",) and not p.name.startswith(".")]
    return sorted(found, key=_rel)


def _fingerprint(pdf_files):
    """
    手册指纹：文件名 + 大小。

    刻意不使用 mtime：镜像构建（Dockerfile 里预建索引）与容器运行时看到的
    mtime 可能不一致，若把 mtime 计入指纹会导致预热好的缓存在运行时被判失效、
    白白重新解析一遍。名称+大小已足以在手册被替换时触发重建。
    """
    items = []
    for p in pdf_files:
        try:
            items.append(f"{_rel(p)}:{p.stat().st_size}")
        except Exception:
            items.append(f"{_rel(p)}:?")
    return "|".join(items)


def build_index(force=False):
    """
    构建（或加载缓存）手册索引。

    Returns:
        dict: {
            "version": int,
            "available": bool,
            "chunks": [{"id", "text", "section", "title", "page", "source"}],
            "inverted": {keyword: [chunk_id, ...]},
            "idf": {keyword: float},
            "source": "手册文件名" 或 None,
            "fingerprint": str
        }
    """
    files = _manual_files()
    fp = _fingerprint(files)

    # 优先加载缓存（需版本与指纹都匹配）
    if not force and INDEX_CACHE.exists():
        try:
            cached = json.loads(INDEX_CACHE.read_text(encoding="utf-8"))
            if (cached.get("version") == INDEX_VERSION
                    and cached.get("fingerprint") == fp):
                return cached
        except Exception:
            pass

    if not files:
        return {"version": INDEX_VERSION, "available": False, "chunks": [],
                "inverted": {}, "idf": {}, "source": None, "fingerprint": fp}

    # 逐文件抽取 -> 章节 -> chunk
    chunks, sources = [], []
    for src in files:
        name = _rel(src)
        is_md = src.suffix.lower() == ".md"
        if is_md:
            # markdown 命令文档：**按 `## 命令名` 切节**（见 _split_md_sections）
            try:
                text = src.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            sections = _split_md_sections(text, name)
            if sections:
                for s in sections:
                    # 文件名本身也是命令名（add_inst.md -> add_inst），
                    # 并入标题关键词，保证「查命令名 -> 命中定义节」；
                    # 已含则不再重复（否则标题里同一个词出现两次，只影响可读性）
                    stem = Path(name).stem
                    if stem.lower() not in s["title"].lower():
                        s["title"] = f"{s['title']} {stem}"
                got = _build_chunks(sections, name)
                if got:
                    chunks.extend(got)
                    sources.append(name)
            continue
        pages = _extract_pdf_pages(src)
        if not any(t.strip() for _, t in pages):
            continue
        sections = _split_sections(pages)
        got = _build_chunks(sections, name)
        if got:
            chunks.extend(got)
            sources.append(name)

    if not chunks:
        return {"version": INDEX_VERSION, "available": False, "chunks": [],
                "inverted": {}, "idf": {}, "source": None, "fingerprint": fp}

    # 重新编号
    for i, c in enumerate(chunks):
        c["id"] = i

    # 同源去重标记：同一文档被切成多段时，只保留排序最靠前的一段进 prompt。
    # 理由：md 定义节被 2000 字符上限切成 2~3 段，查询命中后 TOP_K=3 全被同一
    # 文档占满 —— 真正需要的「Syntax」和「Example」可能落在没被选中的那段，
    # 而其它命令（如 add_net 的写法）直接被挤掉。
    _assign_sibling_ranks(chunks)

    # 倒排索引 + 文档频率
    inverted = {}
    for c in chunks:
        for kw in _extract_keywords(c["text"], c.get("title", "")):
            inverted.setdefault(kw, []).append(c["id"])

    n_docs = len(chunks)
    cutoff = max(3, int(n_docs * _STOPWORD_DF_RATIO))
    inverted = {k: v for k, v in inverted.items() if len(v) <= cutoff}

    # IDF：log(1 + N/df)
    idf = {k: round(math.log(1.0 + n_docs / len(v)), 4) for k, v in inverted.items()}

    # 被剪枝的高频词（如 ctl，出现在 40+ 个 chunk）没有 IDF 值，
    # 但它们在**章节标题**里出现时仍有指示意义（'2.1.5.3 输出 CTL 文件'）。
    # 为此保留一个下限权重，供标题加成时兜底使用。
    min_idf = round(math.log(1.0 + n_docs / cutoff), 4)

    index = {
        "version": INDEX_VERSION,
        "available": True,
        "chunks": chunks,
        "inverted": inverted,
        "idf": idf,
        "min_idf": min_idf,
        "source": ", ".join(sources),
        "fingerprint": fp,
    }

    # 写缓存
    try:
        INDEX_CACHE.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

    return index


# ============================================================================
# 检索
# ============================================================================

class ManualRAG:
    """手册检索器（单例模式）。"""

    def __init__(self):
        self._index = None

    @property
    def index(self):
        if self._index is None:
            self._index = build_index()
        return self._index

    @property
    def available(self):
        return self.index.get("available", False)

    def search_chunks(self, query, top_k=None):
        """检索并按相关性返回 chunk 列表（含章节出处）。"""
        if not self.available:
            return []

        top_k = top_k or TOP_K
        kws = _query_keywords(query)
        if not kws:
            return []

        inverted = self.index["inverted"]
        idf = self.index.get("idf", {})
        chunks = self.index["chunks"]

        # IDF 加权打分：罕见关键词权重更高
        scores = {}
        for kw in kws:
            weight = idf.get(kw, 0.0)
            if weight <= 0:
                continue
            for cid in inverted.get(kw, []):
                scores[cid] = scores.get(cid, 0.0) + weight

        # 章节标题命中加成（含「正文无命中但标题命中」的小节）
        probe = _title_probe_keywords(query)
        fallback = self.index.get("min_idf", 0.0)
        for c in chunks:
            hit = probe & _extract_keywords("", c.get("title", ""))
            if hit:
                bonus = _TITLE_BONUS * sum(idf.get(k, fallback) for k in hit)
                # md 命令文档是命令的权威定义，标题命中时额外加权
                if str(c.get("source", "")).endswith(".md"):
                    bonus *= MD_TITLE_BONUS_MUL
                if bonus > 0:
                    scores[c["id"]] = scores.get(c["id"], 0.0) + bonus

        ranked = sorted(scores.items(), key=lambda x: -x[1])

        # 同源同节的多段**合并成一条**，而不是只取其中一段。
        #
        # 旧实现是「同节只取一段」（`taken_sections` 去重），目的是把 top_k 名额
        # 留给不同命令；但代价太大：84 个章节里有 41 个被切成多段，只取一段
        # 等于**丢掉该章一半正文**。实测 `set_scan_cell_mapping` 就在 2.1.2.9 的
        # 第 2 段里 —— 检索能定位到 2.1.2.9，返回的却是第 1 段，命令原文根本到不了
        # 模型手里（2026-11-02 核对手册覆盖时发现）。
        # 现在按 sibling_rank 拼接整章，名额仍然只占一个。
        grouped, order = {}, []
        for cid, score in ranked:
            c = chunks[cid]
            key = (c.get("source"), c.get("section"), c.get("title"))
            if key not in grouped:
                grouped[key] = {"score": score, "parts": [], "head_chunk": c}
                order.append(key)
            g = grouped[key]
            g["score"] = max(g["score"], score)
            g["parts"].append(c)

        hits = []
        for key in order:
            g = grouped[key]
            parts = sorted(g["parts"],
                           key=lambda c: (c.get("sibling_rank") is None,
                                          c.get("sibling_rank") or 0))
            text = "\n".join(p.get("text", "") for p in parts)
            c = g["head_chunk"]
            head = f"{c.get('section', '')} {c.get('title', '')}".strip()
            hits.append({
                "id": c["id"],
                "score": round(g["score"], 3),
                "section": c.get("section", ""),
                "title": c.get("title", ""),
                "page": c.get("page"),
                "source": c.get("source"),
                "head": head,
                "text": text,
            })
            if len(hits) >= top_k:
                break
        return hits

    def search(self, query, top_k=None):
        """
        检索并返回可直接注入 prompt 的文本。

        Args:
            query: 查询文本（命令名 / 错误码 / 规则号 / 中文问题描述）
            top_k: 返回 chunk 数（默认 TOP_K）

        Returns:
            str: 带章节出处的拼接片段（无结果返回 ""）
        """
        hits = self.search_chunks(query, top_k)
        if not hits:
            return ""

        kws = _query_keywords(query) or set()
        parts = []
        for h in hits:
            body = _trim_around_keywords(h["text"], kws, SNIPPET_MAX_CHARS)
            loc = h["head"] or "手册"
            parts.append(f"【手册 · {loc}（p{h['page']}）】\n{body}")
        return "\n\n---\n\n".join(parts)


# 全局单例
_rag = ManualRAG()


def get_rag() -> ManualRAG:
    return _rag


def search_manual(query, top_k=None):
    """便捷函数：检索手册并返回相关段落。"""
    return get_rag().search(query, top_k)


def manual_stats():
    """返回手册索引概况（供启动横幅/调试使用）。"""
    idx = get_rag().index
    return {
        "available": idx.get("available", False),
        "source": idx.get("source"),
        "sections": len(idx.get("chunks", [])),
        "keywords": len(idx.get("inverted", {})),
    }
