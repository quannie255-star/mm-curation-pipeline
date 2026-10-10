"""Studio 前端的**静态契约** —— 页面白屏/点不动时看这里。
⚠️ 为什么不直接用浏览器截图断言：Chromium 装进 CI 太重（~500MB），
而且**截图断言**只能抓视觉退化，抓不到「点了没反应」这类交互契约。
下面这些是**浏览器抓得到、但静态就能抓**的致命问题，且能进 CI。

⭐ 本文件踩过的坑（别重犯）：
判据第一版写 `re.findall(r"getElementById\\(['\"]#id")` → 抓到 **0 个**引用，
看着像「JS 没碰 DOM」。实际前端用了 `const $ = (id) => getElementById(id)`
的简写，于是判据**恒真地报告「一致」**。
**教训：前端静态判据必须按「真实写法」列全可能的引用形态，
且必须有一个「本该报红」的样例证明它抓得住。**
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "mm_curation" / "studio" / "static"


@pytest.fixture(scope="module")
def html() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def js() -> str:
    return (STATIC / "app.js").read_text(encoding="utf-8")


def _html_ids(html: str) -> set[str]:
    return set(re.findall(r'id="([^"]+)"', html))


def _js_static_ids(js: str) -> set[str]:
    """JS 里**字面量**形式的 DOM id 引用（覆盖 3 种常见写法）。

    ⚠️ 这里只收字面量。拼接式（`$('panel' + i)`）无法静态求值，
    所以另有一条测试用「前缀 + 数字」的规则单独校验（见下）。
    """
    out: set[str] = set()
    for pat in (
        r"getElementById\(\s*['\"]([A-Za-z0-9_-]+)['\"]\s*\)",
        r"querySelector\(\s*['\"]#([A-Za-z0-9_-]+)['\"]\s*\)",
        r"\$\(\s*['\"]([A-Za-z0-9_-]+)['\"]\s*\)",
    ):
        out |= set(re.findall(pat, js))
    return out


def test_前端静态文件齐全():
    for f in ("index.html", "app.js", "style.css"):
        p = STATIC / f
        assert p.exists(), f"缺静态文件 {f}"
        assert p.stat().st_size > 200, f"{f} 只有 {p.stat().st_size} 字节，可能是空壳"


def test_JS引用的dom元素在html里都存在(html, js):
    """点不动/ 白屏的头号原因：JS 找了一个 HTML 里没有的 id。"""
    ids_html = _html_ids(html)
    used = _js_static_ids(js)
    assert used, (
        "一个 DOM 引用都没抓到 —— 大概率是判据没覆盖前端的写法，"
        "**恒真地报告一致**，等于没测")
    missing = sorted(used - ids_html)
    assert not missing, f"JS 引用了 HTML 里不存在的元素：{missing}"


def test_本判据抓得住_反向样例():
    """证明上一条不是恒真：给一份「JS 引用不存在的 id」的样例，必须被抓住。

    没有这一条，上一条可能因为正则写错而**永远绿**。
    """
    bad_js = "const x = $('definitely_not_in_html');\n"
    fake_html = '<div id="real_one"></div>'
    used = _js_static_ids(bad_js)
    assert used & _html_ids(fake_html) == set(), "判据没抓到伪造的坏引用"
    assert used - _html_ids(fake_html) == {"definitely_not_in_html"}


def test_拼接式dom引用有对应容器(html, js):
    """拼接式（`$('panel' + i)`）静态求不出值，改用「前缀 + 数字」校验。

    前端用它做步骤切换（panel1..panel4），少一个面板 = 有一步永远看不见。
    """
    m = re.findall(r"\$\(\s*['\"]([A-Za-z_-]+)['\"]\s*\+\s*", js)
    assert m, "没找到拼接式引用 —— 若前端改成全字面量，本条可退役"
    ids = _html_ids(html)
    for prefix in set(m):
        # 只要 1..8 里有一个对应 id 存在就算过（不猜具体步数）
        present = [i for i in range(1, 9) if f"{prefix}{i}" in ids]
        assert present, f"拼接前缀 '{prefix}' 在 HTML 里没有任何对应容器"


def test_前端只调用存在的后端接口(html, js):
    """JS 调了后端没有的路径 = 该按钮永远转圈。

    判据用**后端源码里的路由表**做参照，而不是把路径写死在测试里 ——
    写死就等于「我以为的接口」，两边不一致时反而会互相掩盖。
    """
    from mm_curation.studio import serve

    src = Path(serve.__file__).read_text(encoding="utf-8")
    server_routes = set(re.findall(r"/api/[a-z_]+", src))
    used = set(re.findall(r"['\"](/api/[a-z_/]+)['\"]", js))
    assert used, "一个 API 调用都没抓到 —— 判据可能恒真"
    missing = sorted(used - server_routes)
    assert not missing, (
        f"前端调了后端没有的接口：{missing}\n后端现有：{sorted(server_routes)}")


def test_上传接口在html里有对应控件(html):
    """至少要有 file input，否则第1 步就走不下去。"""
    assert 'type="file"' in html, "没有文件选择控件"


def test_页面有中文标题与步骤导航(html):
    """面向外行：标题与步骤必须是中文，且向导完整。"""
    assert "<title>" in html
    assert re.search(r"[\u4e00-\u9fff]", html), "页面没有中文，不像给非技术用户"


def test_算子标签必须是中文而不是英文算子名(html, js):
    """⭐ 回归：产品经理看到 `text_minhash` 只会以为页面坏了。

    第一版前端直接渲染 `s.op`（英文算子名），等于没做产品化。
    修法是后端给每个 step 带 `label`（中文），前端渲染它、英文名做 title。
    这条把「必须用中文」变成可核对的契约。
    """
    # ⚠️ 判据必须**只看卡片标签那处模板**，不能全文件搜 `s.op`。
    # 第一版写 `assert "${escapeHtml(s.op)}" not in js` → 误报：
    # 漏斗结果表里的 `<code>${escapeHtml(s.op)}</code>` 是**技术细节视图**，
    # 那里显示英文算子名是对的（用户想看"到底哪一级拦的"）。
    # 真正要禁的是「场景卡的标签直接显示英文名」——
    # 又一次：判据要匹配语义位置，不是全串子串。
    # 注意：模板里有反引号（JS 模板字符串），所以**不能**用 `[^`]` 排除它——
    # 那样永远匹配不到（实测栽过）。直接按「class="tag 开头、到 .join 结束」
    # 的非贪婪跨行匹配即可。
    tag_tpl = re.search(r'class="tag.+?\.join', js, re.S)
    assert tag_tpl, "找不到场景卡的标签模板 —— 前端结构变了，请同步这条判据"
    tpl = tag_tpl.group(0)
    # ⭐⭐ 判据必须区分**正文**与**title 属性**：
    # 只查「模板里出现过 label」会被title 里的那处骗过——
    # 实测栽过：把正文换成 s.op（英文名），label 仍留在 title 里 → 判据放行，
    # 而用户看到的恰恰是正文那处。
    # ⚠️ `title="..."` 的值里本身含 `${...}`，用 `[^"]*` 会提前闭合 ——
    # 正则必须**先定位 title 属性结束**，再找紧跟其后的那个 `+ \`${escapeHtml(s.X)`。
    # 「用户看到的正文」= title 之后第一个插值，不是模板里第一个出现的 s.X。
    after_title = re.split(r'title="', tpl, maxsplit=1)
    assert len(after_title) == 2, f"标签模板里没有 title 属性：{tpl[:160]}"
    body = re.search(r'\+\s*`\$\{escapeHtml\(s\.(\w+)\)', after_title[1])
    assert body, f"判据本身匹配不到标签正文（前端结构变了？）：{tpl[:200]}"
    assert body.group(1) == "label", (
        f"标签正文渲染的是 s.{body.group(1)}（英文算子名），"
        "必须是 s.label（中文说明）")
    assert "escapeHtml(s.cost_zh" in tpl or "COST_LABEL[s.cost]" in tpl, (
        "场景卡标签的代价档没翻译成中文")
    assert "escapeHtml(s.cost_zh" in js or "COST_LABEL[s.cost]" in js, (
        "代价档没翻译成中文")


def test_每个配方算子都有中文说明():
    """中文表与配方**双向**同步（自检里也查了一遍，这里查对外行为）。"""
    from mm_curation.studio import backend
    from mm_curation.studio.recipes import COST_ZH, OPERATOR_ZH, assert_recipes_valid

    assert_recipes_valid()
    for sc in backend.list_scenarios():
        for s in sc["steps"]:
            assert s["label"] in OPERATOR_ZH.values() or s["op"] in OPERATOR_ZH, (
                f"{s['op']} 的 label 不是已知的中文说明")
            assert not s["label"].startswith("（缺"), (
                f"{s['op']} 缺中文说明，前端会显示占位符")
            assert s["cost_zh"] in COST_ZH.values(), (
                f"{s['op']} 的代价档 {s['cost']!r} 没翻译")


def test_中文说明不许出现英文算子名():
    """中文说明里混进英文名 = 没真的翻译（自欺）。"""
    from mm_curation.studio.recipes import RECIPES

    ops = {op for r in RECIPES for op, _ in r.operators}
    for op, zh in [(o, None) for o in ops]:
        from mm_curation.studio.recipes import OPERATOR_ZH
        zh = OPERATOR_ZH[op]
        assert op not in zh, f"{op} 的中文说明里还留着英文名：{zh}"
        assert re.search(r"[一-鿿]", zh), f"{op} 的说明不是中文：{zh}"


def test_步骤条与面板数一致(html):
    """⭐ 用**交叉对账**而不是数固定个数。

    写死「必须有 4 个步骤」的问题：第一版写 `html.count('class="step"') == 4`，
    结果只数到 3 —— 因为首个元素是 `class="step active"`。
    **精确匹配字符串数标签，必然漏掉带修饰class 的那一个。**

    正确判据：导航条里的 `data-n` 集合 == 面板 id 集合 == {1..N}。
    这样「面板加了导航没加」「导航加了面板没加」两种错都会被抓到，
    且不用把步数写死（以后加一步不必改测试）。
    """
    nav_nums = set(re.findall(r'class="step[^"]*"\s+data-n="(\d+)"', html))
    panels = set(re.findall(r'id="panel(\d+)"', html))
    assert nav_nums, "导航条里没有 data-n，步骤指示器坏了"
    assert panels, "没有 panel，向导内容丢了"
    assert nav_nums == panels, (
        f"导航步数 {sorted(nav_nums)} 与面板 {sorted(panels)} 不一致 —— "
        "有一步永远看不见，或有一步点了没反应")
    assert nav_nums == {str(i) for i in range(1, len(panels) + 1)}, (
        f"步号必须从 1 连续编号，实际 {sorted(nav_nums)}")


def test_页面声明了charset避免中文乱码(html):
    assert "charset=\"utf-8\"" in html.lower(), "没声明 utf-8，中文会乱码"


def test_不用外部CDN(html, js):
    """本地工具：CDN 挂了/断网时页面必须还能用，不能白屏。"""
    for pat, name in ((r"https?://(?!127\.0\.0\.1|localhost)", html),):
        hits = re.findall(pat, name)
        assert not hits, f"HTML 引用了外部地址（断网就白屏）：{hits[:3]}"
    for pat in (r"""['"]https?://(?!127\.0\.0\.1|localhost)""",):
        hits = re.findall(pat, js)
        assert not hits, f"JS 引用了外部地址（断网就失败）：{hits[:3]}"


@pytest.fixture(scope="module")
def server():
    import socket
    import subprocess
    import sys
    import time

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", str(ROOT / "scripts" / "run_studio.py"),
         "--no-browser", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace")
    base = f"http://127.0.0.1:{port}"
    for _ in range(150):
        try:
            urllib.request.urlopen(base + "/", timeout=2)
            break
        except (urllib.error.URLError, OSError):
            if proc.poll() is not None:
                out = proc.stdout.read() if proc.stdout else ""
                raise AssertionError(f"服务没起来就退出了：\n{out[-800:]}") from None
            time.sleep(0.2)
    else:
        proc.kill()
        raise AssertionError("服务 30 秒内没就绪")
    yield base
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def test_首页返回的html是完整的不是报错页(server):
    """真起服务取首页：确认 HTML 能取到且不是 traceback。"""
    with urllib.request.urlopen(server + "/", timeout=20) as r:
        body = r.read().decode("utf-8")
    assert r.status == 200
    assert "Traceback" not in body
    assert "<title>" in body and len(body) > 1500, f"首页只有 {len(body)} 字节"


def test_静态资源都取得到且不是404(server):
    """CSS/JS 404 → 页面能显示但完全不能动，这类问题很容易漏。"""
    for path, ctype in (("/style.css", "css"), ("/app.js", "javascript")):
        with urllib.request.urlopen(server + path, timeout=20) as r:
            body = r.read()
        assert r.status == 200, f"{path} -> {r.status}"
        assert len(body) > 500, f"{path} 只有 {len(body)} 字节"
        assert ctype in (r.headers.get("Content-Type") or ""), (
            f"{path} 的 Content-Type 不对：{r.headers.get('Content-Type')}")
