from __future__ import annotations

import asyncio

from docket import commands as C
from docket.ui import PMUI, _project_body_for_panel


def test_status_section_leads_when_after_charter():
    # body 里 charter 在前、现状在后 → 重排后现状领先,charter 跟随,「现状·历史」殿后。
    body = """# 知识库 (KB)

长期边界与路线索引。

## 现状 (2026-06-25)
到哪：稳定。
下一步：定期 eval。

## 现状·历史
- 2026-06-25：首版。
"""
    out = _project_body_for_panel(body)
    sp = out.find("## 现状 (2026-06-25)")
    charter = out.find("# 知识库 (KB)")
    hist = out.find("## 现状·历史")
    assert sp == 0, f"现状段未领先: {out[:40]!r}"
    assert sp < charter < hist, f"段序不对: 现状@{sp} charter@{charter} 历史@{hist}"


def test_status_history_stays_in_background():
    # 「现状·历史」前缀虽含「现状」,但属背景档,不应被当作活状态提到最前。
    body = """## 现状·历史
- old

## 现状 (2026-06-25)
活脉搏。
"""
    out = _project_body_for_panel(body)
    assert out.startswith("## 现状 (2026-06-25)"), f"活现状未领先: {out[:30]!r}"
    assert out.find("## 现状 (2026-06-25)") < out.find("## 现状·历史")


def test_no_status_section_returns_body_stripped():
    body = "\n# 项目\n\n只有 charter,无现状段。\n\n"
    out = _project_body_for_panel(body)
    assert out == "# 项目\n\n只有 charter,无现状段。"


def test_empty_body():
    assert _project_body_for_panel("") == ""
    assert _project_body_for_panel(None) == ""


def test_leading_status_with_no_charter():
    # 现状段已在最前、无 charter → 内容不变(仍领先)。
    body = """## 现状 (2026-06-25)
到哪：x。

## 现状·历史
- y
"""
    out = _project_body_for_panel(body)
    assert out.startswith("## 现状 (2026-06-25)")
    assert "## 现状·历史" in out


def _seed_tier(path, monkeypatch, *titles):
    (path / "issues").mkdir()
    monkeypatch.setenv("DOCKET_ROOT", str(path))
    for t in titles:
        C.cmd_new(t, "", "No priority", None, "", "", "", status="started")


def test_reload_reaggregates_tiers(tmp_path, monkeypatch):
    """R 键刷新(action_reload)在多 tier 启动下必须重走 _load_multi:_load_multi
    init 时会 pop DOCKET_ROOT,旧实现的 reload 走单 tier load_all 会 DocketError 崩溃
    (并丢掉非首 tier 的聚合)。这里跑两 tier、在无 .git 的 cwd 下按 R,验证不抛且总数稳定。"""
    monkeypatch.delenv("DOCKET_ID_PREFIX", raising=False)
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _seed_tier(a, monkeypatch, "a-one", "a-two")
    _seed_tier(b, monkeypatch, "b-one")
    # 让 reload 落在无 .git 的 cwd:旧实现 find_repo_root fallback 必崩,坐实回归。
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DOCKET_ROOT", raising=False)

    async def go():
        app = PMUI(roots=[("a", str(a)), ("b", str(b))])
        async with app.run_test(size=(124, 40)) as pilot:
            await pilot.pause()
            n0 = len(app.issues)
            await app.action_reload()  # 旧代码在此抛 DocketError
            return n0, len(app.issues)

    n0, n1 = asyncio.run(go())
    assert n0 == 3, f"两 tier 聚合应得 3 issue,实得 {n0}"
    assert n1 == 3, f"reload 后应仍是 3 issue(重新聚合两 tier),实得 {n1}"
