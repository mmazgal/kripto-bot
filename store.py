"""Kalıcı veri katmanı.

SUPABASE_URL + SUPABASE_KEY (service_role) tanımlıysa Supabase REST API kullanılır,
değilse yerel 'store.json' dosyası (geliştirme/test için).

Filtreler: {"chat_id": 5}  -> eşittir;  {"due_at": ("lte", "2026-...")} -> operatör.
"""
import data  # noqa: F401  (SSL ayarları; requests'ten önce)

import json
import os
import threading
from pathlib import Path

import requests

_lock = threading.Lock()


def _env(k):
    return os.environ.get(k, "").strip()


def using_supabase() -> bool:
    return bool(_env("SUPABASE_URL") and _env("SUPABASE_KEY"))


# ---------------- Supabase ----------------
def _sb_headers(extra=None):
    key = _env("SUPABASE_KEY")
    h = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json",
         "Prefer": "return=representation"}
    if extra:
        h.update(extra)
    return h


def _sb_url(table):
    return _env("SUPABASE_URL").rstrip("/") + "/rest/v1/" + table


def _sb_params(filters):
    p = {}
    for k, v in (filters or {}).items():
        op, val = v if isinstance(v, tuple) else ("eq", v)
        if isinstance(val, bool):
            val = str(val).lower()
        p[k] = f"{op}.{val}"
    return p


def _sb(method, table, filters=None, body=None, extra_headers=None, extra_params=None):
    params = _sb_params(filters)
    params.update(extra_params or {})
    r = requests.request(method, _sb_url(table), params=params, json=body,
                         headers=_sb_headers(extra_headers), timeout=15)
    if r.status_code >= 400:
        raise RuntimeError(f"Supabase {method} {table}: {r.status_code} {r.text[:200]}")
    return r.json() if r.text else []


# ---------------- Yerel JSON ----------------
_FILE = Path(__file__).with_name("store.json")
_OPS = {"eq": lambda a, b: a == b, "neq": lambda a, b: a != b, "lt": lambda a, b: a < b,
        "lte": lambda a, b: a <= b, "gt": lambda a, b: a > b, "gte": lambda a, b: a >= b}
_PKEYS = {"settings": ("chat_id",), "watchlist": ("chat_id", "item"),
          "predictions": ("item", "horizon_days", "created_date"), "notified": ("key",),
          "paper_state": ("market",), "paper_positions": ("market", "item")}
_NO_ID = {"settings", "watchlist", "notified", "paper_state", "paper_positions"}


def _load():
    return json.loads(_FILE.read_text(encoding="utf-8")) if _FILE.exists() else {}


def _save(db):
    _FILE.write_text(json.dumps(db, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


def _match(row, filters):
    for k, v in (filters or {}).items():
        op, val = v if isinstance(v, tuple) else ("eq", v)
        if k not in row or row[k] is None or not _OPS[op](row[k], val):
            return False
    return True


# ---------------- Ortak API ----------------
def select(table, filters=None, order=None):
    if using_supabase():
        # Supabase tek istekte en fazla 1000 satır döndürür; hepsini sayfa sayfa al
        order = order or (None if table in _NO_ID else "id.asc")
        rows, page = [], 1000
        while True:
            chunk = _sb("GET", table, filters, extra_params={
                "select": "*", "limit": page, "offset": len(rows), **({"order": order} if order else {})})
            rows += chunk
            if len(chunk) < page:
                return rows
    with _lock:
        rows = [r for r in _load().get(table, []) if _match(r, filters)]
    if order:
        col, _, direction = order.partition(".")
        rows.sort(key=lambda r: r.get(col) or 0, reverse=direction == "desc")
    return rows


def insert(table, row, upsert=False):
    if using_supabase():
        headers = {"Prefer": "return=representation,resolution=merge-duplicates"} if upsert else None
        params = {"on_conflict": ",".join(_PKEYS[table])} if upsert and table in _PKEYS else None
        res = _sb("POST", table, body=row, extra_headers=headers, extra_params=params)
        return res[0] if res else row
    with _lock:
        db = _load()
        rows = db.setdefault(table, [])
        keys = _PKEYS.get(table)
        if keys:
            existing = next((r for r in rows if all(r.get(k) == row.get(k) for k in keys)), None)
            if existing is not None:
                if upsert:
                    existing.update(row)
                    _save(db)
                    return existing
                raise RuntimeError("duplicate")
        if "id" not in row and table not in _NO_ID:
            row = {"id": max([r.get("id", 0) for r in rows] + [0]) + 1, **row}
        rows.append(row)
        _save(db)
        return row


def update(table, values, filters):
    if using_supabase():
        return _sb("PATCH", table, filters, body=values)
    with _lock:
        db = _load()
        out = []
        for r in db.get(table, []):
            if _match(r, filters):
                r.update(values)
                out.append(r)
        _save(db)
        return out


def delete(table, filters):
    if using_supabase():
        return _sb("DELETE", table, filters)
    with _lock:
        db = _load()
        keep, gone = [], []
        for r in db.get(table, []):
            (gone if _match(r, filters) else keep).append(r)
        db[table] = keep
        _save(db)
        return gone
