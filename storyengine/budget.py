"""制作費の上限（月・1本）の確認と集計。

- 方針: budget/policy.json（月の上限・1本の上限・円換算レートなど）
- 入金と、生成記録に載らない費用: budget/ledger.json
- 生成の消費額: 各連載の media/epNNN/records.json の cost_usd（失敗分も含む）
- 月は policy の timezone（既定 Asia/Tokyo）で区切る。

有料の処理は、送る前に check() を通す。月の上限を超える依頼は送らない（常に優先）。
1本の上限は原則で、超える場合は利用者の明示の指示（allow_over_episode）が必要。
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .storage import read_json

REPO_ROOT = Path(__file__).resolve().parent.parent


class BudgetError(RuntimeError):
    pass


def _paths(root: Path | None = None) -> tuple[Path, Path]:
    base = (root or REPO_ROOT) / "budget"
    return base / "policy.json", base / "ledger.json"


def load(root: Path | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    pol, led = _paths(root)
    if not pol.is_file():
        raise BudgetError(f"予算方針がありません: {pol}")
    ledger = read_json(led) if led.is_file() else {"deposits": [], "planned_deposits": [], "other_spends": []}
    return read_json(pol), ledger


def month_of(ts: str, tz: str) -> str:
    t = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=_dt.timezone.utc)
    return t.astimezone(ZoneInfo(tz)).strftime("%Y-%m")


def current_month(tz: str, now: _dt.datetime | None = None) -> str:
    return (now or _dt.datetime.now(_dt.timezone.utc)).astimezone(ZoneInfo(tz)).strftime("%Y-%m")


def spends(root: Path | None = None) -> list[dict[str, Any]]:
    """生成記録と ledger の other_spends から、費用の一覧を作る。"""
    policy, ledger = load(root)
    tz = policy.get("timezone", "Asia/Tokyo")
    out = []
    base = root or REPO_ROOT
    for rec_path in sorted((base / "series").glob("*/media/ep*/records.json")):
        rec = read_json(rec_path)
        series_id = rec_path.parts[-4]
        for slot, entry in rec.get("slots", {}).items():
            for v in entry.get("versions", []):
                usd = v.get("cost_usd")
                if usd is None or not v.get("created_at"):
                    continue
                out.append({"month": month_of(v["created_at"], tz), "series": series_id,
                            "episode": rec.get("episode_number"), "item": slot, "status": v.get("status"),
                            "usd": float(usd), "source": "generation_record"})
    for s in ledger.get("other_spends", []):
        out.append({**s, "source": "ledger"})
    return out


def to_jpy(policy: dict[str, Any], usd: float) -> float:
    return usd * float(policy["usd_jpy_rate"])


def summary(root: Path | None = None, *, month: str | None = None, series: str | None = None,
            episode: int | None = None) -> dict[str, Any]:
    policy, ledger = load(root)
    month = month or current_month(policy.get("timezone", "Asia/Tokyo"))
    rows = spends(root)
    m_usd = sum(r["usd"] for r in rows if r["month"] == month)
    e_usd = sum(r["usd"] for r in rows if series and r.get("series") == series and r.get("episode") == episode)
    return {"month": month, "month_spent_jpy": to_jpy(policy, m_usd), "month_spent_usd": m_usd,
            "monthly_cap_jpy": policy["monthly_cap_jpy"],
            "month_remaining_jpy": policy["monthly_cap_jpy"] - to_jpy(policy, m_usd),
            "episode_spent_jpy": to_jpy(policy, e_usd), "per_episode_cap_jpy": policy["per_episode_cap_jpy"],
            "deposits": ledger.get("deposits", []), "planned_deposits": ledger.get("planned_deposits", []),
            "rate": policy["usd_jpy_rate"]}


def check(estimate_usd: float, *, series: str, episode: int, root: Path | None = None,
          allow_over_episode: bool = False) -> dict[str, Any]:
    """これから使う見積もり額が上限内か。超えるなら BudgetError（送らない）。"""
    policy, _ = load(root)
    s = summary(root, series=series, episode=episode)
    add = to_jpy(policy, estimate_usd)
    if s["month_spent_jpy"] + add > policy["monthly_cap_jpy"] + 1e-9:
        raise BudgetError(f"月の上限 {policy['monthly_cap_jpy']}円を超えるため送りません"
                          f"（今月の消費 {s['month_spent_jpy']:.0f}円＋見積もり {add:.0f}円）")
    if s["episode_spent_jpy"] + add > policy["per_episode_cap_jpy"] + 1e-9 and not allow_over_episode:
        raise BudgetError(f"1本の上限 {policy['per_episode_cap_jpy']}円（原則）を超えます"
                          f"（この話の消費 {s['episode_spent_jpy']:.0f}円＋見積もり {add:.0f}円）。"
                          "超えて進める場合は利用者の明示の指示が必要です")
    return {**s, "estimate_jpy": add}
