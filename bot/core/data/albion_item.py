"""
albion_item.py — Truy cập dữ liệu item trang bị Albion (blob tnc_albion_item_v1.json).

Dùng chung cho /iteminfo (cog) và chat_ai hook. Tránh load blob nặng mỗi tin nhắn:
lazy-load + TTL cache 6h. Không crash khi Supabase lỗi → trả []/"".

Schema blob (v2, từ scripts/build_albion_item_db.py):
  {meta, items: {
    "T4_2H_AXE": {unique_name, name, name_vi, slottype, tier, branch, subcat,
                  ref_base, stats, enchant, spells:{active:[{id,slot,tag,name_en,
                  name_vi,desc_en,desc_vi}], passive:[...], passive_count}}
  }}
"""
from __future__ import annotations

import logging
import time
import unicodedata

from ..storage import load_json

logger = logging.getLogger("bot.albion_item")

INDEX_KEY = "tnc_albion_item_v1.json"
CACHE_TTL = 6 * 3600  # 6h: làm mới khi game update / force

_SLOT_LABELS = {
    "mainhand": "Vũ khí (tay chính)",
    "offhand": "Tay phụ",
    "head": "Mũ",
    "armor": "Giáp",
    "shoes": "Giày",
    "cape": "Áo choàng",
    "bag": "Túi",
}
_SLOT_LETTERS = {"1": "Q", "2": "W", "3": "E", "": "R"}

_index: dict | None = None
_index_ts: float = 0.0


def load_items(force: bool = False) -> dict | None:
    """Load + cache blob. Trả {"items", "by_name"} hoặc None nếu lỗi."""
    global _index, _index_ts
    now = time.time()
    if force or _index is None or now - _index_ts > CACHE_TTL:
        try:
            data = load_json(INDEX_KEY, {}) or {}
            items = data.get("items", {}) if isinstance(data, dict) else {}
            _index = {
                "items": items,
                "by_name": {k.lower(): uid for uid, it in items.items() for k in (it.get("name", ""), it.get("name_vi", "")) if k},
            }
            _index_ts = now
            logger.info("Đã load %d items Albion vào cache.", len(items))
        except Exception as exc:  # noqa: BLE001
            logger.warning("load_items thất bại: %s", exc)
            _index = None
    return _index


def _norm(s: str) -> str:
    """Bỏ dấu + lowercase (NFKD), giống chat_ai._norm_text."""
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)).lower()


def search_items(query: str, limit: int = 3) -> list[tuple[str, dict]]:
    """Tìm item theo tên EN/VI/id.

    Scoring (giảm dần ưu tiên):
      100+ = tên norm trùng chính xác
      50   = query 1 từ, tên có chứa nguyên từ đó (substring per token)
      10/ từ = mỗi token query đều substring trong tên (từ ghép kiểu 'greataxe')
      1/ từ = token-overlap
    Trả list [(uid, item)] giảm dần theo độ khớp, tối đa limit.
    """
    idx = load_items()
    if not idx:
        return []
    q_raw = (query or "").strip().lower()
    q = _norm(query).strip()
    if not q:
        return []
    q_nospace = q.replace(" ", "")
    q_words = [w for w in q.split() if w]
    scored: list[tuple[int, str, dict]] = []
    for uid, it in idx["items"].items():
        name_n = _norm(it.get("name", ""))
        namevi_n = _norm(it.get("name_vi", ""))
        name_raw = (it.get("name", "") or "").lower()
        hay = f"{name_n} {namevi_n} {uid}"
        hay_nospace = hay.replace(" ", "")
        if name_n == q or namevi_n == q or uid.lower() == q_nospace:
            score = 100
        elif len(q_words) == 1:
            # query 1 từ: substring trong tên (bắt 'axe'→'greataxe')
            score = 50 if q_words[0] in hay_nospace else 0
            # tiếng Việt: ưu tiên tên thật có chứa từ gốc (giữ dấu)
            if score and q_raw in name_raw:
                score = 60
        else:
            # multi-word: đếm token nào substring trong tên
            score = 10 * sum(1 for w in q_words if w in hay_nospace)
        if score:
            scored.append((score, uid, it))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [(uid, it) for _, uid, it in scored[:limit]]


def _vi(it, s, key):
    """name_vi nếu có, fallback name_en."""
    v = (s or {}).get(f"{key}_vi") or ""
    return v or (s or {}).get(f"{key}_en") or ""


DISCORD_MSG_LIMIT = 2000   # giới hạn tin nhắn Discord (1 tin nội dung)


def _soft_trim(s: str, limit: int) -> str:
    """Rút gọn chuỗi an toàn: không quá `limit` ký tự, không chẻ giữa dòng/emoji/
    ký tự tổ hợp (tiếng Việt) / markdown `**`/`*` hở. Trả "" nếu limit <= 0.
    Cắt lùi về ranh giới dòng → ranh giới từ → né combining/emoji/markdown → thêm "…".
    """
    if limit <= 0:
        return ""
    if len(s) <= limit:
        return s
    cut = limit
    # 1) mô tả nhiều câu (có \n): cắt ở RANH GIỚI CUỐI CÂU (giữ \n trước đó) để
    #    không chẻ giữa câu và không làm dấu `*` mở ở đầu dòng bị hở.
    np = s.rfind("\n", 0, cut)
    if np > 0:
        cut = np + 1          # giữ nguyên phần câu trước dấu \n (câu đầy đủ)
    else:
        # 2) ranh giới từ / cuối câu
        for sep in (" ", ". ", ", "):
            i = s.rfind(sep, 0, cut)
            if i > 0:
                cut = i + 1
                break
    # 3) không chẻ ký tự tổ hợp (ấ, ế...) — lùi qua combining
    while cut > 0 and unicodedata.combining(s[cut - 1]):
        cut -= 1
    # 4) không chẻ cụm emoji/ZWJ/variation selector
    while cut > 0 and (unicodedata.category(s[cut - 1]) in ("So", "Mn") or s[cut - 1] == "‍"):
        cut -= 1
    # 5) không chẻ markdown ** / * : nếu cắt giữa cặp mở-đóng → lùi về token mở
    for token in ("**", "*"):
        opener = s.rfind(token, 0, cut)
        if opener >= 0:
            closer = s.find(token, opener + len(token), cut)
            if closer == -1 or closer + len(token) > cut:
                cut = opener
    head = s[:cut].rstrip(" ")
    return (head + "…") if head else "…"


_counters = {"seq": 0}

def _next_idx() -> int:
    _counters["seq"] += 1
    return _counters["seq"]


class _Atom:
    """1 đơn vị render: group header hoặc skill (name bắt buộc, desc co giãn)."""
    __slots__ = ("kind", "letter", "id", "name_line", "desc", "prio")

    def __init__(self, kind: str, letter: str, name_line: str, desc: str = "",
                 prio: int = 0):
        self.kind = kind            # "group" | "active" | "passive"
        self.letter = letter        # "Q"..."R" (group), "" với skill
        self.id = _next_idx()
        self.name_line = name_line
        self.desc = desc
        self.prio = prio            # ưu tiên mô tả: active trước (1), passive sau (2)


def _build_atoms(uid: str, item: dict) -> tuple[list[str], list[_Atom]]:
    """Tách block render thành: (hdr — các dòng header/stats/enchant, atoms — nhóm skill)."""
    hdr: list[str] = []
    name = item.get("name_vi") or item.get("name") or uid
    name_en = item.get("name") or ""
    hdr.append(f"**{name}**")
    if name_en and name_en != name:
        hdr.append(f"*{name_en}*")
    hdr.append(f"`{uid}`")

    slot = _SLOT_LABELS.get(item.get("slottype", ""), item.get("slottype", ""))
    tier = item.get("tier", "")
    hdr.append(f"🛡️ {slot} | Cấp {tier}" + (f" | Nhánh: {item.get('branch','')}" if item.get("branch") else ""))

    stats = item.get("stats", {}) or {}
    parts = []
    for label, k in (("IP", "itempower"), ("ATK", "attackdamage"), ("HP", "hitpointsmax"),
                     ("Def", "physicalarmor"), ("Mag", "magicresistance"),
                     ("Năng", "energymax"), ("Move", "movespeed")):
        if stats.get(k):
            parts.append(f"{label} {stats[k]}")
    if parts:
        hdr.append("📊 " + " | ".join(parts))

    ench = item.get("enchant", {}) or {}
    if ench:
        ip0 = stats.get("itempower", "")
        ench_strs = []
        for lvl in ("1", "2", "3"):
            k = f"{uid}@{lvl}"
            e = ench.get(k) or ench.get(k.split('@')[0] + '@' + lvl)
            if not e:
                continue
            ip = e.get("itempower", "")
            if ip and ip != ip0:
                ench_strs.append(f"@lv{lvl}: IP {ip}")
        if ench_strs:
            hdr.append("✨ " + " | ".join(ench_strs))
    hdr.append("")

    spells = item.get("spells", {}) or {}
    atoms: list[_Atom] = []
    grouped: dict[str, list] = {"Q": [], "W": [], "E": [], "R": []}
    for s in spells.get("active", []) or []:
        if s.get("remove"):
            continue
        letter = _SLOT_LETTERS.get(str(s.get("slot", "")), "R")
        grouped.setdefault(letter, []).append(s)
    for letter in ("Q", "W", "E", "R"):
        sl = grouped.get(letter) or []
        if not sl:
            continue
        atoms.append(_Atom("group", letter, f"**{letter}**"))
        for s in sl:
            nm = _vi(item, s, "name")
            tag = f" ({s.get('tag','')})" if s.get("tag") else ""
            atoms.append(_Atom("active", "", f"• {nm}{tag}",
                               _vi(item, s, "desc"), prio=1))
    pas = spells.get("passive", []) or []
    if pas:
        atoms.append(_Atom("group", "Passive", f"**Passive ({len(pas)})**"))
        for s in pas:
            nm = _vi(item, s, "name")
            atoms.append(_Atom("passive", "", f"• {nm}",
                               _vi(item, s, "desc"), prio=2))
    return hdr, atoms


def _render(hdr: list[str], atoms: list[_Atom], alloc: dict[int, int]) -> str:
    """Render atoms theo phân bổ độ dài mô tả `alloc[id]`. Phân bổ 0 → chỉ tên/header.

    Mô tả nhiều câu được wrap MỖI câu thành 1 dòng `   *...*` riêng → cân bằng markdown,
    không bao giờ có dòng mang dấu `*` mở hở.
    """
    lines = list(hdr)
    for a in atoms:
        if a.kind == "group":
            lines.append(a.name_line)
        else:
            lines.append(a.name_line)
            limit = alloc.get(a.id, 0)
            if limit and a.desc:
                d = _soft_trim(a.desc, limit)
                if d:
                    for sub in d.split("\n"):
                        if sub.strip():
                            lines.append(f"   *{sub.strip()}*")
    return "\n".join(lines)


def _fit_budget(hdr: list[str], atoms: list[_Atom], max_desc: int, max_chars: int) -> str:
    """Phân bổ budget cho mô tả: Active ưu tiên hơn Passive (per user).

    Tên skill + group header + header item LUÔN đủ. Mô tả cấp theo budget; nếu vẫn vượt,
    bỏ dần dòng MÔ TẢ từ cuối (passive trước, active sau) — KHÔNG bao giờ đụng tên/header.
    """
    floor = _render(hdr, atoms, {})
    budget = max_chars - len(floor)
    alloc: dict[int, int] = {}
    # Active (prio 1) cấp trước, theo thứ tự render (Q→R)
    for a in atoms:
        if a.kind != "active" or not a.desc:
            continue
        take = min(len(a.desc), max_desc, budget) if budget > 0 else 0
        alloc[a.id] = take
        budget -= take
    # Passive sau (chỉ cấp nếu còn budget)
    for a in atoms:
        if a.kind != "passive" or not a.desc:
            continue
        take = min(len(a.desc), max_desc, budget) if budget > 0 else 0
        alloc[a.id] = take
        budget -= take
    text = _render(hdr, atoms, alloc)
    if len(text) <= max_chars:
        return text
    # Vượt budget (chênh do wrapper `   *...*` / "…"): bỏ dần DÒNG MÔ TẢ cuối
    # (passive trước theo thứ tự render) tới khi đủ ngắn. Dòng tên/header giữ nguyên.
    lines = text.splitlines()
    while len("\n".join(lines)) > max_chars:
        desc_idx = [i for i, l in enumerate(lines) if l.startswith("   *")]
        if not desc_idx:
            break
        lines.pop(desc_idx[-1])
    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text
    return text[:max_chars - 3].rstrip("\n") + "\n…"


def format_item_full(uid: str, item: dict, max_desc: int = 1200, max_chars: int = DISCORD_MSG_LIMIT) -> str:
    """Render block text tiếng Việt đầy đủ (tên/tier/slot/branch/enchant/skills).

    - `max_desc`: giới hạn trên cho MỖI mô tả skill.
    - `max_chars`: tổng budget output (Discord 2000 mặc định; chat hook truyền cap prompt).
    Item ngắn (full <= max_chars) → giữ trọn; item dài → ưu tiên Active trước (per user),
    Passive ít nhất còn tên. Không bao giờ cắt giữa dòng/markdown/emoji.
    """
    if not item:
        return f"⚠ Không có dữ liệu item `{uid}`."
    hdr, atoms = _build_atoms(uid, item)
    full_alloc = {a.id: min(len(a.desc), max_desc) for a in atoms if a.desc}
    full = _render(hdr, atoms, full_alloc)
    if len(full) <= max_chars:
        return full
    return _fit_budget(hdr, atoms, max_desc, max_chars)


def format_item_compact(uid: str, item: dict) -> str:
    """Bản gọn cho chat hook (ít dòng, tiết kiệm prompt token)."""
    if not item:
        return ""
    name = item.get("name_vi") or item.get("name") or uid
    line = f"- {name} (`{uid}`) | {_SLOT_LABELS.get(item.get('slottype',''), '')} Cấp {item.get('tier','')}"
    spells = item.get("spells", {}) or {}
    act = [s for s in spells.get("active", []) if not s.get("remove")]
    pas = spells.get("passive", []) or []
    skill_parts = []
    for s in act:
        nm = _vi(item, s, "name")
        skill_parts.append(nm)
    for s in pas:
        skill_parts.append(_vi(item, s, "name"))
    if skill_parts:
        line += " | " + ", ".join(skill_parts)
    return line
