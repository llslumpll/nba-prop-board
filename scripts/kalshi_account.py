"""The user's own Kalshi trades for My Bets (Nov 19 2026), read with a READ-ONLY Kalshi API key.

Secrets (GitHub repository secrets, never in the repo):
  KALSHI_KEY_ID       the key's ID
  KALSHI_PRIVATE_KEY  the private key file's text (-----BEGIN RSA PRIVATE KEY----- ...)
Kalshi keys can be RSA or Ed25519 (both handled). The key only needs the "read" scope: it can't place orders or move money.

Every build pulls fills (GET /portfolio/fills) and settlements (GET /portfolio/settlements) since START and turns
each market into one bet:
  - fills are netted in YES terms (buy YES / sell NO = +YES; buy NO / sell YES = -YES), cash = -contracts x YES price
  - P/L = cash - fees + net YES contracts x settlement value (1 if YES won, 0 if NO won; scalar uses `value`)
  - staked = what the bought side cost (contracts x that side's price + fees); units = dollars / 100
Titles come from the public market endpoint. data/kalshi_account.json keeps the last good copy (units only).
"""
from __future__ import annotations

import base64
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "kalshi_account.json"
BASES = ("https://api.elections.kalshi.com/trade-api/v2", "https://external-api.kalshi.com/trade-api/v2")
START = "2026-11-19"          # the user's fresh start: nothing before this counts
UNIT = 100.0                  # 1U = $100
ET = ZoneInfo("America/New_York")


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _start_ts() -> int:
    return int(datetime.fromisoformat(START).replace(tzinfo=ZoneInfo("America/Chicago")).timestamp())


class Client:
    def __init__(self, key_id: str, pem: str):
        from cryptography.hazmat.primitives import serialization
        self.key_id = key_id.strip()
        pem = pem.strip().replace("\\n", "\n")
        self.key = serialization.load_pem_private_key(pem.encode(), password=None)
        self.base = BASES[0]

    def _sign(self, method: str, path: str) -> dict:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        ts = str(int(time.time() * 1000))
        msg = (ts + method + "/trade-api/v2" + path.split("?")[0]).encode()
        from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
        if isinstance(self.key, ed25519.Ed25519PrivateKey):     # newer Kalshi keys
            sig = self.key.sign(msg)
        elif isinstance(self.key, rsa.RSAPrivateKey):
            sig = self.key.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                                hashes.SHA256())
        else:
            raise RuntimeError(f"unsupported key type {type(self.key).__name__}")
        return {"KALSHI-ACCESS-KEY": self.key_id, "KALSHI-ACCESS-TIMESTAMP": ts,
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()}

    def get(self, path: str, params: dict | None = None) -> dict:
        last = None
        for base in [self.base] + [b for b in BASES if b != self.base]:
            try:
                r = requests.get(base + path, params=params, headers=self._sign("GET", path), timeout=20)
            except requests.RequestException as e:
                last = e
                continue
            if r.status_code == 404 and base != BASES[-1]:
                last = RuntimeError(f"404 at {base}")
                continue
            if r.status_code in (401, 403):
                raise RuntimeError(f"Kalshi refused the key ({r.status_code}): {r.text[:120]}")
            r.raise_for_status()
            self.base = base
            return r.json()
        raise RuntimeError(f"Kalshi unreachable: {last}")

    def pages(self, path: str, key: str, params: dict) -> list:
        out, cursor = [], ""
        for _ in range(50):
            p = dict(params, limit=1000)
            if cursor:
                p["cursor"] = cursor
            d = self.get(path, p)
            out += d.get(key) or []
            cursor = d.get("cursor") or ""
            if not cursor:
                break
        return out


def _title(ticker: str, cache: dict) -> dict:
    if ticker in cache:
        return cache[ticker]
    info = {"title": ticker}
    try:
        r = requests.get(f"{BASES[0]}/markets/{ticker}", timeout=15)
        if r.ok:
            m = r.json().get("market") or {}
            info = {"title": m.get("title") or ticker, "yes": m.get("yes_sub_title") or "", "no": m.get("no_sub_title") or "",
                    "event": m.get("event_ticker") or ""}
    except requests.RequestException:
        pass
    cache[ticker] = info
    return info


def _kind(ticker: str) -> str:
    t = ticker.upper()
    if t.startswith("KXMVE") or "MULTIGAME" in t or "PARLAY" in t:
        return "parlay"
    if "GAME" in t or "SPREAD" in t or "TOTAL" in t:
        return "game"
    return "prop"


def _am(p: float) -> str:
    if not 0 < p < 1:
        return ""
    return f"-{round(100 * p / (1 - p))}" if p >= 0.5 else f"+{round(100 * (1 - p) / p)}"


def build(fills: list, settlements: list, titles: dict) -> list[dict]:
    """One My Bets row per market traded since START."""
    by = {}
    for f in fills:
        t = f.get("ticker") or f.get("market_ticker")
        if not t:
            continue
        n = _f(f.get("count_fp") if f.get("count_fp") is not None else f.get("count"))
        yp = _f(f.get("yes_price_dollars")) if f.get("yes_price_dollars") is not None else _f(f.get("yes_price")) / 100
        np_ = _f(f.get("no_price_dollars")) if f.get("no_price_dollars") is not None else _f(f.get("no_price")) / 100
        if f.get("action") in ("buy", "sell") and f.get("side") in ("yes", "no"):
            yes = (f["side"] == "yes") == (f["action"] == "buy")
            bought = f["action"] == "buy"
        else:
            yes = (f.get("outcome_side") or "yes") == "yes"
            bought = True
        b = by.setdefault(t, {"net": 0.0, "cash": 0.0, "fee": 0.0, "cost": 0.0, "n_yes": 0.0, "n_no": 0.0, "first": None})
        b["net"] += n if yes else -n
        b["cash"] += -n * yp if yes else n * yp
        b["fee"] += _f(f.get("fee_cost"))
        if bought:
            b["cost"] += n * (yp if yes else np_)
            b["n_yes" if yes else "n_no"] += n
        ct = f.get("created_time") or ""
        if ct and (b["first"] is None or ct < b["first"]):
            b["first"] = ct
    sett = {s.get("ticker"): s for s in settlements}
    rows = []
    for t, b in by.items():
        info = _title(t, titles)
        side = "YES" if b["n_yes"] >= b["n_no"] else "NO"
        contracts = b["n_yes"] if side == "YES" else b["n_no"]
        staked = b["cost"] + b["fee"]
        s = sett.get(t)
        result, pl = "pending", None
        if s:
            mr = s.get("market_result")
            val = 1.0 if mr == "yes" else 0.0 if mr == "no" else (_f(s.get("value")) / 100 if s.get("value") is not None else None)
            if val is not None:
                pl = b["cash"] - b["fee"] + b["net"] * val
                result = "hit" if pl > 0.005 else ("miss" if pl < -0.005 else "push")
        try:
            d = datetime.fromisoformat(b["first"].replace("Z", "+00:00")).astimezone(ET).date().isoformat()
        except Exception:  # noqa: BLE001
            d = START
        pick = info.get("yes") if side == "YES" else (f"Not {info.get('yes')}" if info.get("yes") else info.get("no"))
        label = info.get("title") or t
        if pick and pick.lower() not in label.lower():
            label = f"{label}: {pick}"
        avg = (b["cost"] + b["fee"]) / contracts if contracts else 0
        rows.append({"src": "kalshi", "ticker": t, "date": d, "date_iso": d, "bet": label, "label": label, "stat": "",
                     "side": side, "line": "", "book": "Kalshi", "price": _am(avg), "kind": _kind(t),
                     "units": f"{staked / UNIT:.2f}", "units_f": round(staked / UNIT, 2),
                     "payout": round(1 / avg, 4) if 0 < avg < 1 else None, "result": result,
                     "pl": round(pl / UNIT, 2) if pl is not None else 0.0, "row": 0})
    rows.sort(key=lambda r: (r["date_iso"], r["ticker"]))
    for i, r in enumerate(rows, 1):
        r["row"] = 2000 + i
    return rows


def run() -> tuple[list[dict], str | None]:
    """(rows, error). No secrets = ([], 'not set up'). On an error the last good copy is returned."""
    key_id, pem = os.environ.get("KALSHI_KEY_ID", ""), os.environ.get("KALSHI_PRIVATE_KEY", "")
    if not key_id.strip() or "PRIVATE KEY" not in pem:
        return [], "not set up"
    try:
        c = Client(key_id, pem)
        st = _start_ts()
        fills = [f for f in c.pages("/portfolio/fills", "fills", {"min_ts": st})
                 if (f.get("created_time") or "9") >= datetime.fromtimestamp(st, timezone.utc).strftime("%Y-%m-%dT%H:%M")]
        sett = c.pages("/portfolio/settlements", "settlements", {"min_ts": st})
        try:
            titles = json.loads(OUT.read_text()).get("titles", {})
        except Exception:  # noqa: BLE001
            titles = {}
        rows = build(fills, sett, titles)
        OUT.write_text(json.dumps({"at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "start": START,
                                   "fills": len(fills), "rows": rows, "titles": titles}, indent=1))
        return rows, None
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"[:200]
        try:
            return json.loads(OUT.read_text()).get("rows", []), err
        except Exception:  # noqa: BLE001
            return [], err
