"""血缘图（Lineage Graph）：从判决书反推「这条数据是谁处理的、影响了什么」。

## 与判决书的关系

`verdict.jsonl` 每条裁决都带 PROV-O 三元组（`prov.used` / `prov.wasGeneratedBy` /
`prov.activity`）。本模块把它**从一堆行还原成一张图**——判决书解决「单条能不能追溯」，
血缘图解决「改一个算子会影响谁」。

## 命名对齐 OpenLineage（只做最小语义子集）

真实生产环境里血缘是跨系统的，各家命名不一会直接导致无法互通。
本模块导出时使用 OpenLineage 的词汇（run / job / dataset / inputs / outputs），
**但不实现 OpenLineage 的完整事件模型**——完整实现需要 Kafka 后端与元数据服务，
成本远超收益（见 `docs/DS_DA_TRACK.md` 第四节「明确不做」）。

## 节点的三种类型

| 前缀 | 含义 |
|---|---|
| `entity:` | 数据实体（内容指纹，与 id 无关——换 id 不改变「这份内容被谁处理过」） |
| `job:` | 作业（一个算子的一次运行） |
| `verdict:` | 裁决（作业对实体的处理结果） |
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..verdict.ledger import iter_verdicts

NODE_ENTITY = "entity"
NODE_JOB = "job"
NODE_VERDICT = "verdict"

EDGE_USED = "used"
EDGE_GENERATED_BY = "wasGeneratedBy"


@dataclass
class LineageGraph:
    run_id: str
    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)
    edges: list[dict[str, str]] = field(default_factory=list)

    def add_node(self, nid: str, ntype: str, **attrs: Any) -> None:
        node = self.nodes.setdefault(nid, {"id": nid, "type": ntype})
        node.update(attrs)

    def add_edge(self, src: str, dst: str, kind: str) -> None:
        e = {"from": src, "to": dst, "kind": kind}
        if e not in self.edges:
            self.edges.append(e)

    # -- 查询 ---------------------------------------------------------------

    def jobs(self) -> list[str]:
        return sorted(n for n, v in self.nodes.items() if v["type"] == NODE_JOB)

    def entities(self) -> list[str]:
        return sorted(n for n, v in self.nodes.items() if v["type"] == NODE_ENTITY)

    def downstream_of(self, job: str) -> list[str]:
        """该作业之后的作业：处理过**同一批实体**且序号更大的作业。

        判据是「共享实体 + 序号更大」，不是配置文件里的顺序——
        配置顺序可以被重写，实体流不会被重写。
        """
        jid = f"{NODE_JOB}:{job}"
        if jid not in self.nodes:
            return []
        seq = int(self.nodes[jid].get("seq", 0))

        # 第一步：本作业处理过哪些实体（顺着本作业的裁决找 used 边）
        my_verdicts = {
            e["from"] for e in self.edges if e["to"] == jid and e["kind"] == EDGE_GENERATED_BY
        }
        mine = {e["to"] for e in self.edges if e["from"] in my_verdicts and e["kind"] == EDGE_USED}
        if not mine:
            return []

        # 第二步：**同时**满足「共享实体」和「序号更大」的其它作业才算下游。
        # 少了共享实体这个条件，任何序号更大的算子都会被算成下游——
        # 那等于把「配置里排在后面」当成「真的受它影响」，正是docstring 承诺要避免的。
        out: set[str] = set()
        for e in self.edges:
            if e["kind"] != EDGE_USED or e["to"] not in mine:
                continue
            v = self.nodes.get(e["from"]) or {}
            if v.get("type") != NODE_VERDICT:
                continue
            if int(v.get("seq", 0)) > seq and v.get("op") and v["op"] != job:
                out.add(str(v["op"]))
        return sorted(out)

    def upstream_entities(self, job: str) -> list[str]:
        """该作业消费过的实体（内容指纹）。

        两步跳：作业 ← 裁决 → 实体。直接找 `to == jid` 只能拿到裁决节点 id
        （`verdict:<run>:<seq>:<op>`），不是实体——这个错误不会崩，
        只会安静地返回 1 条，看起来像「这个算子只处理了一份数据」。
        """
        jid = f"{NODE_JOB}:{job}"
        my_verdicts = {
            e["from"] for e in self.edges if e["to"] == jid and e["kind"] == EDGE_GENERATED_BY
        }
        return sorted(
            e["to"].split(":", 1)[1]
            for e in self.edges
            if e["from"] in my_verdicts and e["kind"] == EDGE_USED
        )

    # -- 导出 ---------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "n_nodes": len(self.nodes),
            "n_edges": len(self.edges),
            "nodes": [self.nodes[k] for k in sorted(self.nodes)],
            "edges": self.edges,
        }

    def to_openlineage(self) -> dict[str, Any]:
        """导出为 OpenLineage 风格的最小 run event（语义对齐，非完整事件模型）。"""
        jobs = [{"namespace": "mm-curation", "name": n.split(":", 1)[1]} for n in self.jobs()]
        inputs = [{"namespace": "mm-curation", "name": n.split(":", 1)[1]} for n in self.entities()]
        return {
            "eventType": "COMPLETE",
            "run": {"runId": self.run_id},
            "job": jobs[0] if len(jobs) == 1 else {"name": "curation_funnel", "facets": {}},
            "inputs": inputs,
            "outputs": [{"namespace": "mm-curation", "name": f"verdict:{self.run_id}"}],
        }

    def to_mermaid(self, *, max_nodes: int = 40) -> str:
        """渲染成 Mermaid（可直接嵌进 Markdown 在 GitHub / 文档里看）。

        节点名一律走 `n0["标签"]` 形式：标签里有 `#`（裁决节点 `op#seq`）和
        `sha256:`，裸写会让 Mermaid 解析失败或把 `#` 当成实体编码，
        而失败方式是「图渲染成空白」——不报错，最难排查。
        """
        lines = ["graph LR"]
        idx: dict[str, str] = {}

        def nid(n: str) -> str:
            if n not in idx:
                idx[n] = f"n{len(idx)}"
            return idx[n]

        for e in self.edges:
            if len(idx) >= max_nodes:
                break
            a, b = e["from"], e["to"]
            la = self.nodes.get(a, {}).get("label", a)
            lb = self.nodes.get(b, {}).get("label", b)
            arrow = "-->" if e["kind"] == EDGE_GENERATED_BY else "-.->"
            lines.append(f'  {nid(a)}["{_safe(la)}"] {arrow} {nid(b)}["{_safe(lb)}"]')
        return "\n".join(lines)


def _safe(text: str) -> str:
    return str(text).replace('"', "'").replace("[", "(").replace("]", ")")


def _label(nid: str, node: dict[str, Any]) -> str:
    kind = node["type"]
    name = nid.split(":", 1)[1]
    if kind == NODE_JOB:
        return f"{name}"
    if kind == NODE_ENTITY:
        # 用 ASCII "..." 而不是 "…"：Windows 控制台（GBK）会把 U+2026 显示成乱码
        return f"{name[:12]}..."
    return f"{node.get('op', '?')}#{node.get('seq', '?')}"


def build_lineage(rows: Iterable[dict[str, Any]], *, run_id: str = "") -> LineageGraph:
    """由判决书行构造血缘图（纯函数，便于测试与复算）。"""
    g = LineageGraph(run_id=run_id or "unknown")
    for r in rows:
        op = str(r.get("op") or "?")
        seq = int(r.get("seq") or 0)
        prov = r.get("prov") or {}
        entity = prov.get("used") or r.get("input_fingerprint") or ""
        if not entity:
            continue
        rid = str(r.get("run_id") or run_id or "unknown")
        g.run_id = rid

        eid = f"{NODE_ENTITY}:{entity}"
        jid = f"{NODE_JOB}:{op}"
        vid = f"{NODE_VERDICT}:{rid}:{seq}:{op}"

        g.add_node(eid, NODE_ENTITY, label=_label(eid, {"type": NODE_ENTITY}))
        g.add_node(jid, NODE_JOB, seq=seq, label=op)
        g.add_node(
            vid,
            NODE_VERDICT,
            op=op,
            seq=seq,
            decision=r.get("decision"),
            rule=r.get("rule"),
            label=f"{op}#{seq}",
        )
        g.add_edge(vid, eid, EDGE_USED)
        g.add_edge(vid, jid, EDGE_GENERATED_BY)
    return g


def lineage_from_file(path: str | Path, *, limit: int = 0) -> LineageGraph:
    rows: list[dict[str, Any]] = []
    for i, r in enumerate(iter_verdicts(path)):
        if limit and i >= limit:
            break
        rows.append(r)
    run_id = rows[0].get("run_id", "") if rows else ""
    return build_lineage(rows, run_id=run_id)
