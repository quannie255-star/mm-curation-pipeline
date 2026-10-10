"""守住 YAML「注释紧贴值」这类静默失效。

为什么需要这个门禁（不是洁癖）：
`target: 0.005# 目标：…` —— `#` 前缺空格时，YAML 把它当**值的一部分**，
解析结果是字符串 `"0.005# 目标：…"` 而不是数字。

它的可怕之处在于**失效形态取决于用法**：
  · 用它做算术比较 → 报 TypeError（还算好，至少炸得响）
  · 用它做 `if cfg.x == 0.005` → 永远 False，**门禁永远绿**
  · 用它当覆盖率阈值 → 阈值变成一个非空字符串，`if`判断同样永远为真
最后两种就是**恒真判据**的标准产地。本项目已在
`configs/detection_slo.yaml` 踩过一次（0.005 与字符串比较 → TypeError）。

门禁判据：对每个含 `#` 的行，若 `#` 前的字符**不是空白**且该行去掉注释后仍有内容
→ 判FAIL。允许的情况：`#` 整行注释、行首缩进注释、值与 `#` 之间有空格。
"""

from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
#: 只扫「人写的 YAML」——生成物与数据文件不在此列。
#:
#: ⚠️ 2026-10-08 扩到 ``.github/workflows``：写 gate-ci.yml 时**当场**犯了
#:   同类错误（`- name:断言 …` 冒号后缺空格 → YAML 报
#:   "mapping values are not allowed"）。也就是说这条纪律的适用范围
#:   一直包含 CI 文件，只是扫描范围没跟上。
#:   **扫描范围落后于实际写入面= 门禁在它最该管的地方是空的。**
CONFIG_DIRS = ("configs",)
CONFIG_GLOBS = (
    ("configs", "*.yaml"),
    ("configs", "*.yml"),
    (".github/workflows", "*.yml"),
    (".github/workflows", "*.yaml"),
    ("packages", "**/*.yaml"),
)


def find_bad_lines(path: pathlib.Path) -> list[tuple[int, str, str]]:
    """返回 [(行号, 行内容, 形态)]，形态 ∈ {comment-glued, colon-glued}。

    **两种形态都要查** —— 只查第一种会漏掉本项目当场犯过的第二种：

    ① `comment-glued`：`target: 0.005# 目标` —— `#` 前缺空格，
       `#` 被并入值，静默失效（本脚本的原始职责）。
    ② `colon-glued`：`- name:断言 上游` —— **映射键的冒号后缺空格**，
       YAML 解析器把 `name:断言` 当键、后面再出现 `:` 时报
       "mapping values are not allowed"，或更糟：静默解析成别的结构。

    ⚠️⚠️ **必须跟踪块标量**（`run: |` / `script: >` 之类）：
       块内的行是 **shell/命令文本，不是 YAML 结构**，
       里面的 `8081:8080`、`$PWD/data:/app/data`、`mm-curation-app:ci`
       全是值内部的冒号。第一版没跟踪块标量，把这些全判成粘连 ——
       **过度判据也是假门禁**（永远红 ⇒ 没人看 ⇒ 真问题一起被忽略）。

    ②的判据用**位置 + 结构**（键必须紧跟行首列表标记）而非子串，
    避免误伤 `http://`这类值里的冒号。
    """
    bad: list[tuple[int, str, str]] = []
    lines = path.read_text(encoding="utf-8").split("\n")
    in_block = False
    block_indent = 0
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if in_block:
            # 块内空行不结束块；有内容则必须比块标量缩进更深
            if stripped:
                indent = len(line) - len(line.lstrip())
                if indent <= block_indent:
                    in_block = False  # 块结束，落到下面的结构解析
                else:
                    continue  # 仍是块内 → shell 文本，跳过所有判据
            else:
                continue
        # 块标量起始行：`key: |` / `key: >` / `key: |-` / `key: >-`
        if _starts_block_scalar(line):
            in_block = True
            block_indent = len(line) - len(line.lstrip())
            continue
        if "#" in line:
            before = line[: line.index("#")]
            # 整行注释 / 行首注释合法
            if before.strip() and before[-1] not in " \t":
                bad.append((i, line.rstrip(), "comment-glued"))
                continue
        if _colon_glued(line):
            bad.append((i, line.rstrip(), "colon-glued"))
    return bad


_BLOCK_RE = __import__("re").compile(r":\s*[|>][-+0-9]*\s*(#.*)?$")


def _starts_block_scalar(line: str) -> bool:
    """该行是否开启了一个块标量（`run: |`、`script: >-` 等）。"""
    return bool(_BLOCK_RE.search(line.split("#", 1)[0]))


#: 键位置的合法形态：`- key:` / `key:` / `  - name:` / `- name:`。
#: 允许键名含 `- . _ / ` （覆盖 `no.abs`、`utf-8`、`a/b`）。
_KEY_RE = __import__("re").compile(r"""^\s*(?:-\s+)?[A-Za-z_][\w.\-/ ]*$""")


def _colon_glued(line: str) -> bool:
    """该行是否存在「映射键的冒号后缺空格」。

    逐个冒号判定，四个条件全满足才算粘连：
      ① 冒号前的内容是**合法键名**形态（否则是值里的冒号）
      ② 该键名以 `- ` 或缩进开头（即这一行确实在声明键）
      ③ 冒号后紧跟非空白
      ④ 冒号前的内容**不含空格**（含空格说明已经是「键: 值」形态，
         后面那个冒号必然在值里，如 `run: docker run -p 8081:8080`）

    ⚠️ ④ 是踩过坑才加的：第一版漏了它，于是把
      `pip install torch --index-url https://download.pytorch.org/whl/cpu`
      和 `-p 8081:8080` 全判成粘连 —— **过度判据也是假门禁**
      （永远红 ⇒ 没人看 ⇒ 真问题也一起被忽略）。
    """
    import re

    body = line.split("#", 1)[0]  # 注释里的冒号不算
    if not body.strip():
        return False
    stripped = body.lstrip()
    # 只有「行首是列表项或缩进」的行才可能在声明键
    if not (stripped.startswith("- ") or body.startswith((" ", "\t"))):
        return False
    # ⚠️ 键只能紧跟在**行首第一个**列表标记之后。
    #   第二个 `-` 之后的内容是**列表项的值**（`docker run -p 8081:8080`
    #   里的 `8081:8080`、`mm-curation-app:ci` 都在这一段里），
    #   它的冒号是值内部结构，不是 YAML 键。
    #   判据用「位置」而不是「正则匹配键名」—— 正则会把
    #   `mm-curation-app` 当成合法键名（它含 `-`），从而产生假红。
    list_marker = re.match(r"^(\s*)-\s+", body)
    if list_marker:
        # 键必须出现在列表标记**紧后面**，即该行只有一个 `-` 标记
        head_span = (list_marker.end(), len(body))
    else:
        head_span = (0, len(body))
    if list_marker and "-" in body[list_marker.end() :]:
        # 列表标记之后还有第二个 `-` → 该行是「命令 + 选项」，不是键声明
        tail = body[list_marker.end() :]
        if re.search(r"\s-\S", tail):
            return False
    for m in re.finditer(r":", body):
        pos = m.start()
        if not (head_span[0] <= pos < head_span[1]):
            continue  # 冒号在键位置之外 → 值内部
        head = body[head_span[0] : pos]
        if not head.strip():
            continue
        if " " in head.strip() or "\t" in head.strip():
            continue  # ④键名里带空白 → 这是值里的冒号
        if not _KEY_RE.match(head):
            continue  # ① 不是合法键名 → 值里的冒号
        after = body[pos + 1 : pos + 2]
        if after and not after.isspace():
            return True
    return False


def main() -> int:
    targets: list[pathlib.Path] = []
    for d, pattern in CONFIG_GLOBS:
        targets.extend(sorted((ROOT / d).glob(pattern)))
    # 去重（同一个文件可能被多个 glob 命中）但**保序**，便于输出可复现
    seen: set[pathlib.Path] = set()
    uniq: list[pathlib.Path] = []
    for p in targets:
        if p not in seen:
            seen.add(p)
            uniq.append(p)

    total = 0
    for p in uniq:
        bad = find_bad_lines(p)
        if bad:
            total += len(bad)
            print(f"★ {p.relative_to(ROOT)}:{len(bad)} 处粘连：")
            for n, line, kind in bad:
                print(f"    {n} [{kind}]: {line.strip()[:90]}")
    if total:
        print()
        print("修复：")
        print("  · comment-glued → 在 `#` 前加一个空格。缺空格会让 `#` 被并入值")
        print("    → 静默失效，典型后果是门禁永远绿（恒真判据）。")
        print("  · colon-glued  → 在映射键的 `:` 后加一个空格（如 `- name:标题`）。")
        print("    缺空格会让 YAML 把后半段当键的一部分，解析成别的结构或直接报错。")
        return 1
    print(f"✅ 扫描 {len(uniq)} 个 YAML，无注释紧贴值问题")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
