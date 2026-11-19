"""生成素材（画像）の生成記録・再利用判定・生成処理。

保存場所（パスはすべて連載ディレクトリ series/<id>/ からの相対パス）:
    references/                         人物・場所の基準画像（Git 管理）
    media/epNNN/records.json            生成記録（Git 管理）
    media/epNNN/images/<slot>-<key>.png 生成した画像（Git 管理外。永続保存は asset_storage）

生成条件（指示文・除外指示・モデル・サイズ・参照画像の内容）から条件キーを作り、
同じ条件で完成済みかつ正常に読み込める画像があれば再利用する。条件が変わった画像は「更新が必要」とし、
以前の画像はファイル名が別なので残る。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import os
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .integrations import AssetStore, ImageGenerator, NotConfiguredError
from .render import PRODUCTION_SCHEMA_VERSION, build_production
from .series import Series
from .state import load_episodes
from .storage import atomic_write_json, canonical_json, content_hash, exclusive_lock, read_json

RECORDS_SCHEMA_VERSION = 1
MAX_FAILED_PER_SLOT = 5
ASPECT_TOLERANCE = 0.01
EXT = {"png": ".png", "jpeg": ".jpg", "webp": ".webp"}


class InvalidImageError(ValueError):
    pass


class GenerationError(RuntimeError):
    pass


# ---------------------------------------------------------------- 画像の検査


def inspect_image(path: Path) -> dict[str, Any]:
    """画像として読み込めるか検査し、形式と縦横の大きさを返す（標準ライブラリのみ）。

    PNG はチャンクの CRC と画素データの展開まで確認する。JPEG / WebP は構造（先頭・終端・大きさ）を確認する。
    """
    try:
        data = Path(path).read_bytes()
    except OSError as e:
        raise InvalidImageError(f"読み込めません: {e}") from e
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        w, h = _inspect_png(data)
        fmt = "png"
    elif data.startswith(b"\xff\xd8"):
        w, h = _inspect_jpeg(data)
        fmt = "jpeg"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        w, h = _inspect_webp(data)
        fmt = "webp"
    else:
        raise InvalidImageError("PNG / JPEG / WebP のいずれでもありません")
    if w <= 0 or h <= 0:
        raise InvalidImageError("画像の大きさが不正です")
    return {"format": fmt, "width": w, "height": h, "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest()}


def _inspect_png(data: bytes) -> tuple[int, int]:
    pos, ihdr, idat, seen_end = 8, None, [], False
    while pos + 12 <= len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        ctype = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        crc_bytes = data[pos + 8 + length:pos + 12 + length]
        if len(body) != length or len(crc_bytes) != 4:
            raise InvalidImageError("PNG が途中で切れています")
        if zlib.crc32(ctype + body) & 0xFFFFFFFF != struct.unpack(">I", crc_bytes)[0]:
            raise InvalidImageError(f"PNG の {ctype!r} チャンクが壊れています")
        if ihdr is None and ctype != b"IHDR":
            raise InvalidImageError("PNG の先頭に IHDR がありません")
        if ctype == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", body)
        elif ctype == b"IDAT":
            idat.append(body)
        elif ctype == b"IEND":
            seen_end = True
            break
        pos += 12 + length
    if not ihdr or not idat or not seen_end:
        raise InvalidImageError("PNG に必要なチャンク（IHDR / IDAT / IEND）がそろっていません")
    w, h, depth, ctype_, _, _, interlace = ihdr
    try:
        raw = zlib.decompress(b"".join(idat))
    except zlib.error as e:
        raise InvalidImageError(f"PNG の画素データを展開できません: {e}") from e
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(ctype_)
    if channels is None:
        raise InvalidImageError("PNG の色形式が不正です")
    if interlace == 0:
        row = (w * channels * depth + 7) // 8
        if len(raw) != h * (row + 1):
            raise InvalidImageError("PNG の画素データの量が大きさと一致しません")
        if any(raw[i * (row + 1)] > 4 for i in range(h)):
            raise InvalidImageError("PNG の画素データが壊れています")
    return w, h


def _inspect_jpeg(data: bytes) -> tuple[int, int]:
    if not data.rstrip(b"\x00").endswith(b"\xff\xd9"):
        raise InvalidImageError("JPEG の終端がありません（途中で切れています）")
    pos, size = 2, None
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            raise InvalidImageError("JPEG の構造が壊れています")
        marker = data[pos + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        (seg,) = struct.unpack(">H", data[pos + 2:pos + 4])
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h, w = struct.unpack(">HH", data[pos + 5:pos + 9])
            size = (w, h)
        if marker == 0xDA:
            break
        pos += 2 + seg
    if size is None:
        raise InvalidImageError("JPEG の大きさが読み取れません")
    return size


def _inspect_webp(data: bytes) -> tuple[int, int]:
    (riff,) = struct.unpack("<I", data[4:8])
    if riff + 8 != len(data):
        raise InvalidImageError("WebP のサイズが一致しません（途中で切れています）")
    chunk = data[12:16]
    b = data[20:]
    if chunk == b"VP8X":
        return 1 + int.from_bytes(b[4:7], "little"), 1 + int.from_bytes(b[7:10], "little")
    if chunk == b"VP8L" and b[:1] == b"\x2f":
        bits = int.from_bytes(b[1:5], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8 " and b[3:6] == b"\x9d\x01\x2a":
        w, h = struct.unpack("<HH", b[6:10])
        return w & 0x3FFF, h & 0x3FFF
    raise InvalidImageError("WebP の形式が読み取れません")


def check_aspect(info: dict[str, Any], resolution: str | None) -> None:
    if not resolution:
        return
    rw, rh = (int(x) for x in resolution.lower().split("x"))
    want, got = rw / rh, info["width"] / info["height"]
    if abs(got - want) / want > ASPECT_TOLERANCE:
        raise InvalidImageError(f"縦横比が指定と違います（{info['width']}x{info['height']}、指定 {resolution}）")


# ---------------------------------------------------------------- 生成条件


_HASH_CACHE: dict[tuple[str, int, int], str] = {}


def _sha256_file(path: Path) -> str:
    st = os.stat(path)
    ck = (str(path), st.st_mtime_ns, st.st_size)
    if ck in _HASH_CACHE:
        return _HASH_CACHE[ck]
    _HASH_CACHE[ck] = _sha256_file_uncached(path)
    return _HASH_CACHE[ck]


def _sha256_file_uncached(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def model_descriptor(gen_cfg: dict[str, Any] | None) -> dict[str, Any]:
    cfg = gen_cfg or {}
    return {"provider": cfg.get("provider"), "model": cfg.get("model"), "params": cfg.get("params") or {}}


_READABLE_CACHE: dict[tuple[str, int, int], bool] = {}


def _reference_readable(p: Path) -> bool:
    st = os.stat(p)
    ck = (str(p), st.st_mtime_ns, st.st_size)
    if ck not in _READABLE_CACHE:
        try:
            inspect_image(p)
            _READABLE_CACHE[ck] = True
        except InvalidImageError:
            _READABLE_CACHE[ck] = False
    return _READABLE_CACHE[ck]


def image_conditions(series: Series, spec: dict[str, Any], model: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """生成条件と、見つからない参照画像の一覧を返す。"""
    refs, missing = [], []
    for r in spec.get("references", []):
        if not r.get("path"):  # 生成済み画像の参照で、まだ完成していない
            missing.append(f"生成済み画像 {r.get('slot')}（未完成）")
            refs.append({"subject": r["subject"], "view": r.get("view", ""), "slot": r.get("slot"), "sha256": None})
            continue
        p = series.resolve(r["path"])
        ok = p.is_file() and _reference_readable(p)
        if ok:
            refs.append({"subject": r["subject"], "view": r.get("view", ""), "path": r["path"],
                         "sha256": _sha256_file(p)})
        else:
            missing.append(r["path"])
            refs.append({"subject": r["subject"], "view": r.get("view", ""), "path": r["path"], "sha256": None})
    cond = {
        "kind": "image",
        "prompt_sha256": hashlib.sha256(spec["prompt"].encode("utf-8")).hexdigest(),
        "negative_prompt_sha256": hashlib.sha256(spec.get("negative_prompt", "").encode("utf-8")).hexdigest(),
        "model": model,
        "aspect_ratio": spec.get("aspect_ratio"),
        "resolution": spec.get("resolution"),
        "references": refs,
    }
    return cond, missing


def seed_for(key: str) -> int:
    """条件キーから決まる固定シード（同じ条件なら同じ値）。"""
    return int(key.split(":", 1)[1][:8], 16) % (2 ** 31)


# ---------------------------------------------------------------- 記録


def load_records(series: Series, n: int) -> dict[str, Any]:
    p = series.media_records_path(n)
    if p.is_file():
        rec = read_json(p)
    else:
        rec = {}
    rec.setdefault("schema_version", RECORDS_SCHEMA_VERSION)
    rec.setdefault("_note", "生成記録（自動生成）。パスは連載ディレクトリからの相対パス。")
    rec.setdefault("episode_number", n)
    rec.setdefault("slots", {})
    return rec


def save_records(series: Series, n: int, rec: dict[str, Any]) -> None:
    atomic_write_json(series.media_records_path(n), rec)


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def _verify_local(series: Series, version: dict[str, Any], resolution: str | None) -> str | None:
    """完成記録のファイルが正常に読み込め、記録と同じ内容か。問題があれば理由を返す。"""
    try:
        p = series.resolve(version["file"])
    except ValueError as e:
        return str(e)
    if not p.is_file():
        return "ファイルがありません"
    try:
        info = inspect_image(p)
        check_aspect(info, resolution)
    except InvalidImageError as e:
        return f"画像として読み込めません: {e}"
    if info["sha256"] != version.get("sha256"):
        return "記録と内容が違います"
    return None


# ---------------------------------------------------------------- 計画


@dataclass
class SlotPlan:
    cut_number: int
    slot: str
    key: str
    action: str  # reuse / restore / generate / update / blocked
    reason: str
    spec: dict[str, Any]
    conditions: dict[str, Any]
    references: list[dict[str, Any]]
    missing_references: list[str]
    output_dir: str
    current: dict[str, Any] | None = None  # 再利用・復元する完成記録
    previous: list[str] = field(default_factory=list)  # 以前の条件で作られた画像（保持）

    @property
    def planned_file(self) -> str:
        if self.current:
            return self.current["file"]
        return f"{self.output_dir}/{self.slot}-{self.key.split(':', 1)[1][:16]}.<拡張子>"

    @property
    def persisted(self) -> bool:
        return bool(self.current and (self.current.get("storage") or {}).get("status") == "persisted")


ACTION_LABEL = {
    "reuse": "再利用（生成済み・検証OK）",
    "restore": "永続保存から復元して再利用",
    "generate": "生成が必要（未生成）",
    "update": "更新が必要（生成条件が変わった。以前の画像は保持）",
    "regenerate": "生成が必要（記録はあるがファイルが失われた・壊れている）",
    "blocked": "生成できない（参照画像が見つからない）",
}


def current_production(series: Series, n: int) -> tuple[dict[str, Any], list[str]]:
    """生成に使う production を episode.json と現在の series.json から組み立てる。

    保存済み production.json が旧形式・古い内容でも動くよう、ファイルの内容には依存しない。
    """
    eps = {e["episode_number"]: e for e in load_episodes(series)}
    if n not in eps:
        raise GenerationError(f"第{n}話は登録されていません")
    prod = build_production(series, eps[n])
    notes = []
    saved = series.episode_dir(n) / "production.json"
    if saved.is_file():
        old = read_json(saved)
        if old.get("schema_version") != PRODUCTION_SCHEMA_VERSION:
            notes.append("保存済みの production.json は旧形式です（python -m storyengine render で更新できます）。"
                         "生成には episode.json と series.json から組み立てた最新の指示を使います。")
        elif canonical_json(old) != canonical_json(prod):
            notes.append("保存済みの production.json が series.json の変更前の内容です（render で更新できます）。")
    legacy = series.episode_dir(n) / "assets"
    if legacy.is_dir() and any(legacy.iterdir()):
        notes.append(f"旧形式の素材フォルダ {series.rel(legacy)}/ があります。生成記録がないため再利用せず、削除もしません。")
    return prod, notes


def plan_images(series: Series, n: int, *, cuts: list[int] | None = None,
                store: AssetStore | None = None) -> tuple[list[SlotPlan], list[str]]:
    prod, notes = current_production(series, n)
    model = model_descriptor(series.config.get("generation", {}).get("image"))
    rec = load_records(series, n)
    plans = []
    done: dict[str, SlotPlan] = {}  # 前のカットの計画（生成済み画像を参照するため、全カットを順に計算する）
    for cut in prod["cuts"]:
        spec = dict(cut["image"])
        spec["references"] = [_resolve_generated(r, done) for r in spec.get("references", [])]
        cond, missing = image_conditions(series, spec, model)
        key = content_hash(cond)
        slot = spec["slot"]
        versions = rec["slots"].get(slot, {}).get("versions", [])
        complete = [v for v in versions if v.get("status") == "complete"]
        same = [v for v in complete if v["key"] == key]
        previous = [v["file"] for v in complete if v["key"] != key]
        p = SlotPlan(cut["cut_number"], slot, key, "generate", "", spec, cond, spec.get("references", []),
                     missing, spec["output_dir"], previous=previous)
        if missing:
            p.action, p.reason = "blocked", "見つからない・読み込めない参照画像: " + ", ".join(missing)
        elif same:
            v = same[-1]
            problem = _verify_local(series, v, spec.get("resolution"))
            p.current = v
            if problem is None:
                p.action, p.reason = "reuse", ""
            elif store and (v.get("storage") or {}).get("uri"):
                p.action, p.reason = "restore", problem
            else:
                p.action, p.reason = "regenerate", problem
        elif previous:
            p.action = "update"
        done[slot] = p
        if not cuts or cut["cut_number"] in cuts:
            plans.append(p)
    return plans, notes


def _resolve_generated(ref: dict[str, Any], done: dict[str, "SlotPlan"]) -> dict[str, Any]:
    """continuity_references の参照先を、そのカットの現在の完成画像（再利用できるもの）に解決する。"""
    if ref.get("kind") != "generated":
        return ref
    prev = done.get(ref["slot"])
    path = prev.current["file"] if prev and prev.action == "reuse" and prev.current else None
    return dict(ref, path=path)


# ---------------------------------------------------------------- 実行


@dataclass
class RunResult:
    plans: list[SlotPlan]
    notes: list[str]
    done: list[int] = field(default_factory=list)
    failed: list[tuple[int, str]] = field(default_factory=list)

    @property
    def all_complete(self) -> bool:
        return not self.failed and all(p.action in ("reuse", "restore") or p.cut_number in self.done
                                       for p in self.plans)


def _append_version(rec: dict[str, Any], slot: str, version: dict[str, Any]) -> None:
    entry = rec["slots"].setdefault(slot, {"kind": "image", "versions": []})
    entry["versions"].append(version)
    failed = [v for v in entry["versions"] if v["status"] == "failed"]
    for old in failed[:-MAX_FAILED_PER_SLOT]:
        entry["versions"].remove(old)


def _persist(series: Series, store: AssetStore | None, path: Path, n: int) -> dict[str, Any]:
    if store is None:
        return {"status": "local_only", "note": "永続保存先が未設定です（クラウド環境の終了で失われます）"}
    try:
        uri = store.upload(path, f"{series.id}/{series.rel(path)}")
    except Exception as e:  # noqa: BLE001 - 保存失敗は記録して続ける
        return {"status": "upload_failed", "error": str(e)[:500]}
    return {"status": "persisted", "provider": store.provider, "uri": uri, "at": _now()}


def run_images(series: Series, n: int, generator: ImageGenerator | None, *, store: AssetStore | None = None,
               cuts: list[int] | None = None) -> RunResult:
    """各カットを順に処理する。1カットごとに記録を保存するので、途中で失敗しても再実行で続きから再開できる。"""
    with exclusive_lock(series.lock_path):
        plans, notes = plan_images(series, n, cuts=cuts, store=store)
        result = RunResult(plans, notes)
        for first in plans:
            # 前のカットをこの実行で生成した場合も参照できるよう、カットごとに計画し直す
            p = plan_images(series, n, cuts=[first.cut_number], store=store)[0][0]
            rec = load_records(series, n)
            if p.action == "reuse":
                if store is not None and not p.persisted:
                    _retry_upload(series, store, p, rec, n)
                    save_records(series, n, rec)
                continue
            if p.action == "blocked":
                result.failed.append((p.cut_number, p.reason))
                break
            if p.action == "restore":
                if _restore(series, store, p):
                    result.done.append(p.cut_number)
                    continue
            if generator is None:
                result.failed.append((p.cut_number, "画像生成サービスが未接続です（series.json の generation.image.provider）"))
                break
            if generator.placeholder:
                raise NotConfiguredError("プレースホルダー（ダミー）の生成器の出力は完成素材として登録できません")
            ok, err = _generate_one(series, n, generator, store, p, rec)
            save_records(series, n, rec)  # 1カットごとに保存（再開できるように）
            if ok:
                result.done.append(p.cut_number)
            else:
                result.failed.append((p.cut_number, err))
                break  # 費用を無駄にしないよう、失敗したら止める
        return result


def _retry_upload(series: Series, store: AssetStore, p: SlotPlan, rec: dict[str, Any], n: int) -> None:
    for v in rec["slots"][p.slot]["versions"]:
        if v.get("status") == "complete" and v["key"] == p.key and v.get("file") == p.current["file"]:
            v["storage"] = _persist(series, store, series.resolve(v["file"]), n)
            p.current = v


def _restore(series: Series, store: AssetStore | None, p: SlotPlan) -> bool:
    dest = series.resolve(p.current["file"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".tmp-{dest.name}")
    try:
        store.download(p.current["storage"]["uri"], tmp)
        info = inspect_image(tmp)
        if info["sha256"] != p.current["sha256"]:
            raise InvalidImageError("復元したファイルが記録と一致しません")
        os.replace(tmp, dest)
        return True
    except Exception:  # noqa: BLE001 - 復元できなければ生成し直す
        tmp.unlink(missing_ok=True)
        return False


def _generate_one(series: Series, n: int, gen: ImageGenerator, store: AssetStore | None,
                  p: SlotPlan, rec: dict[str, Any]) -> tuple[bool, str]:
    out_dir = series.resolve(p.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / f".tmp-{p.slot}-{os.getpid()}.part"
    spec = dict(p.spec)
    spec["references"] = [dict(r, abs_path=str(series.resolve(r["path"]))) for r in p.references]
    seed = seed_for(p.key) if gen.supports_seed else None
    if seed is not None:
        spec["seed"] = seed
    base = {"key": p.key, "cut_number": p.cut_number, "conditions": p.conditions,
            "seed": seed, "created_at": _now()}
    gen.last_usage = gen.last_request = None
    try:
        gen.generate(spec, tmp)
        info = inspect_image(tmp)
        check_aspect(info, spec.get("resolution"))
    except Exception as e:  # noqa: BLE001 - 失敗は記録して完成品として扱わない
        tmp.unlink(missing_ok=True)
        _append_version(rec, p.slot, dict(base, **_usage_fields(gen), status="failed",
                                          error=f"{type(e).__name__}: {e}"[:500]))
        return False, str(e)
    base.update(_usage_fields(gen))
    final = out_dir / f"{p.slot}-{p.key.split(':', 1)[1][:16]}{EXT[info['format']]}"
    os.replace(tmp, final)
    version = dict(base, status="complete", file=series.rel(final), format=info["format"],
                   width=info["width"], height=info["height"], bytes=info["bytes"], sha256=info["sha256"])
    version["storage"] = _persist(series, store, final, n)
    _append_version(rec, p.slot, version)
    return True, ""


def _usage_fields(gen: ImageGenerator) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if gen.last_request:
        out["request"] = gen.last_request
    if gen.last_usage:
        out["usage"] = gen.last_usage
        cost = gen.cost_from_usage(gen.last_usage)
        if cost is not None:
            out["cost_usd"] = cost
    return out


def persist_all(series: Series, store: AssetStore, episodes: list[int]) -> list[tuple[str, str, str]]:
    """完成済みでまだ永続保存されていない画像（以前の版も含む）を保存先へ送る。結果は (ファイル, 状態, 詳細)。"""
    results = []
    with exclusive_lock(series.lock_path):
        for n in episodes:
            if not series.media_records_path(n).is_file():
                continue
            rec = load_records(series, n)
            for slot in rec["slots"].values():
                for v in slot["versions"]:
                    if v.get("status") != "complete" or (v.get("storage") or {}).get("status") == "persisted":
                        continue
                    problem = _verify_local(series, v, None)
                    if problem:
                        results.append((v["file"], "skipped", problem))
                        continue
                    v["storage"] = _persist(series, store, series.resolve(v["file"]), n)
                    results.append((v["file"], v["storage"]["status"], v["storage"].get("uri") or v["storage"].get("error", "")))
                    save_records(series, n, rec)
    return results


def storage_summary(plans: list[SlotPlan], store: AssetStore | None) -> str:
    total = len(plans)
    complete = sum(1 for p in plans if p.action in ("reuse", "restore"))
    persisted = sum(1 for p in plans if p.action in ("reuse", "restore") and p.persisted)
    if store is None:
        return (f"完成 {complete}/{total} カット。永続保存先が未設定のため、保存完了ではありません"
                "（クラウド環境が終了すると画像は失われます）。")
    if complete == total and persisted == total:
        return f"完成 {complete}/{total} カット。すべて永続保存済み（保存完了）。"
    return f"完成 {complete}/{total} カット、永続保存済み {persisted}/{total} カット。保存完了ではありません。"
