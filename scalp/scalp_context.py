#!/usr/bin/env python3
"""Pre-trade context check for 1-minute small-cap long scalps.

For each ticker it gathers market status, news, SEC filings, gap and liquidity,
then gives a verdict: GO, CAUTION or SKIP. It sends the result to Telegram and
saves it to the scalp-context branch so the Telegram bot can attach it to
breakout alerts.

Daily data plus the latest pre/post-market price. No 1-minute signals here.
"""
import base64
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

PT = ZoneInfo("America/Los_Angeles")
SEC_UA = {"User-Agent": "scalp-context sarin8n-rgb@users.noreply.github.com"}
CTX_BRANCH = "scalp-context"
CTX_FILE = "scalp_context.json"
KEEP_HOURS = 18  # verdicts older than this are dropped from the saved file

# Prospectus forms mean shares are being sold now. Registration forms mean they can be.
SALE_FORMS = ("424B",)
SHELF_FORMS = ("S-1", "S-3", "F-1", "F-3")
SALE_SKIP_DAYS = 7
FILING_LOOKBACK_DAYS = 30
THIN_DOLLAR_VOL = 1_000_000  # average $ traded per day below this = thin
BIG_GAP_PCT = 20.0
OFFERING_WORDS = re.compile(
    r"\b(public offering|registered direct|private placement|at-the-market|"
    r"pricing of|prices? \$?[\d.]+ ?(million|m)? ?(offering|public)|warrants?|"
    r"dilut\w+|reverse (stock )?split|shelf registration|delist\w*)\b",
    re.I,
)


def log(msg):
    print(msg, flush=True)


def clean_tickers(raw):
    out = []
    for t in re.split(r"[\s,]+", raw or ""):
        t = re.sub(r"[^A-Z0-9.\-]", "", t.upper())
        if 1 <= len(t) <= 12 and t not in out:
            out.append(t)
    return out


# ---------------------------------------------------------------- price data

def daily_stats(hist):
    """hist: DataFrame of daily bars (Open/High/Low/Close/Volume), oldest first."""
    h = hist.dropna(subset=["Close"])
    if len(h) < 2:
        return None
    last, prev = h.iloc[-1], h.iloc[-2]
    vol20 = h["Volume"].iloc[-21:-1].mean() if len(h) > 5 else float("nan")
    dollar5 = (h["Close"] * h["Volume"]).iloc[-5:].mean()
    ma20 = h["Close"].iloc[-20:].mean() if len(h) >= 20 else float("nan")
    return {
        "date": str(h.index[-1])[:10],
        "close": float(last["Close"]),
        "prev_close": float(prev["Close"]),
        "day_chg_pct": (float(last["Close"]) / float(prev["Close"]) - 1) * 100,
        "rel_vol": float(last["Volume"]) / float(vol20) if vol20 and vol20 == vol20 and vol20 > 0 else None,
        "dollar_vol": float(dollar5) if dollar5 == dollar5 else None,
        "ma20": float(ma20) if ma20 == ma20 else None,
    }


def live_price(tk):
    """Latest trade including pre/post market, or None."""
    try:
        m = tk.history(period="1d", interval="1m", prepost=True)
        m = m.dropna(subset=["Close"])
        if len(m):
            return float(m["Close"].iloc[-1])
    except Exception as e:  # noqa: BLE001
        log(f"  live price failed: {e}")
    return None


def fetch_prices(yf, ticker):
    tk = yf.Ticker(ticker)
    stats = None
    try:
        stats = daily_stats(tk.history(period="3mo", interval="1d", auto_adjust=False))
    except Exception as e:  # noqa: BLE001
        log(f"  daily bars failed: {e}")
    if not stats:
        return None, tk
    lp = live_price(tk)
    stats["live"] = lp
    # Gap = latest price vs the last regular close. If no newer trade, 0.
    stats["gap_pct"] = (lp / stats["close"] - 1) * 100 if lp else None
    return stats, tk


def market_status(yf):
    rows, above = [], 0
    spy_live_chg = None
    for sym in ("SPY", "QQQ", "IWM"):
        stats, _ = fetch_prices(yf, sym)
        if not stats:
            continue
        is_above = stats["ma20"] is not None and stats["close"] > stats["ma20"]
        above += 1 if is_above else 0
        chg = stats["gap_pct"] if stats["gap_pct"] is not None else 0.0
        if sym == "SPY":
            spy_live_chg = chg
        rows.append({"sym": sym, "above_ma20": is_above, "day_chg_pct": stats["day_chg_pct"], "live_chg_pct": chg})
    if not rows:
        return {"state": "UNKNOWN", "text": "Market data unavailable", "rows": []}
    n = len(rows)
    live = spy_live_chg if spy_live_chg is not None else 0.0
    if live <= -0.7 or (above <= n // 3 and live < 0):
        state = "DOWN"
    elif above >= (2 * n + 2) // 3 and live > -0.3:
        state = "UP"
    else:
        state = "MIXED"
    text = f"{above}/{n} indexes above 20-day avg, SPY {live:+.1f}% vs last close"
    return {"state": state, "text": text, "rows": rows}


# ---------------------------------------------------------------- news + filings

def fetch_news(tk, days=3, limit=8):
    """Recent headlines from Yahoo. Returns [{'title','date'}], newest first."""
    out = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        items = tk.news or []
    except Exception as e:  # noqa: BLE001
        log(f"  news failed: {e}")
        return out
    for it in items:
        c = it.get("content") if isinstance(it.get("content"), dict) else it
        title = (c.get("title") or "").strip()
        when = None
        if c.get("pubDate"):
            try:
                when = datetime.fromisoformat(str(c["pubDate"]).replace("Z", "+00:00"))
            except ValueError:
                when = None
        elif c.get("providerPublishTime"):
            when = datetime.fromtimestamp(int(c["providerPublishTime"]), tz=timezone.utc)
        if not title or (when and when < cutoff):
            continue
        out.append({"title": title[:200], "date": when.strftime("%b %d") if when else ""})
    return out[:limit]


_CIK_MAP = None


def sec_cik(ticker):
    global _CIK_MAP
    if _CIK_MAP is None:
        try:
            r = requests.get("https://www.sec.gov/files/company_tickers.json", headers=SEC_UA, timeout=20)
            r.raise_for_status()
            _CIK_MAP = {v["ticker"].upper(): int(v["cik_str"]) for v in r.json().values()}
        except Exception as e:  # noqa: BLE001
            log(f"  SEC ticker map failed: {e}")
            _CIK_MAP = {}
    return _CIK_MAP.get(ticker.upper().replace(".", "-")) or _CIK_MAP.get(ticker.upper())


def fetch_filings(ticker, today=None):
    """Offering-related SEC filings in the lookback window.
    Returns None if SEC can't be checked, else [{'form','date','days_ago','kind'}]."""
    cik = sec_cik(ticker)
    if not cik:
        return None
    try:
        r = requests.get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", headers=SEC_UA, timeout=20)
        r.raise_for_status()
        recent = r.json()["filings"]["recent"]
    except Exception as e:  # noqa: BLE001
        log(f"  SEC filings failed: {e}")
        return None
    return classify_filings(recent.get("form", []), recent.get("filingDate", []), today)


def classify_filings(forms, dates, today=None):
    today = today or datetime.now(PT).date()
    out = []
    for form, d in zip(forms, dates):
        try:
            days = (today - datetime.strptime(d, "%Y-%m-%d").date()).days
        except ValueError:
            continue
        if days < 0 or days > FILING_LOOKBACK_DAYS:
            continue
        f = form.upper()
        if f.startswith(SALE_FORMS):
            kind = "sale"
        elif f.split("/")[0] in SHELF_FORMS:
            kind = "shelf"
        else:
            continue
        out.append({"form": form, "date": d, "days_ago": days, "kind": kind})
    return sorted(out, key=lambda x: x["days_ago"])


# ---------------------------------------------------------------- LLM news read

def read_news_llm(ticker, headlines):
    """Ask Gemini to classify the headlines. Returns {'catalyst','summary'} or None."""
    key = os.getenv("GEMINI_API_KEY")
    if not key or not headlines:
        return None
    prompt = (
        f"You classify news for a day trader looking at a LONG scalp in {ticker} today.\n"
        "Headlines (newest first):\n"
        + "\n".join(f"- [{h['date']}] {h['title']}" for h in headlines)
        + "\n\nReply with JSON only: "
        '{"catalyst": "positive" | "negative" | "none", "offering": true | false, "summary": "<max 15 words>"}\n'
        "positive = fresh company-specific good news likely to bring buyers today "
        "(earnings beat, contract, approval, partnership, upgrade).\n"
        "negative = bad company-specific news (miss, downgrade, investigation, delisting notice, failed trial).\n"
        "none = no company-specific news, old news, or generic market or sector articles.\n"
        "offering = true if any headline is about selling shares, a public or direct offering, "
        "warrants, dilution or a reverse split."
    )
    models = [os.getenv("GEMINI_MODEL") or "gemini-3.6-flash", "gemini-3.5-flash-lite"]
    for model in dict.fromkeys(models):
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": key, "content-type": "application/json"},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
                },
                timeout=40,
            )
            if r.status_code != 200:
                log(f"  Gemini {model}: HTTP {r.status_code} {r.text[:150]}")
                continue
            text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            data = json.loads(text)
            cat = str(data.get("catalyst", "")).lower()
            if cat not in ("positive", "negative", "none"):
                continue
            return {
                "catalyst": cat,
                "offering": bool(data.get("offering")),
                "summary": str(data.get("summary", "")).strip()[:140],
            }
        except Exception as e:  # noqa: BLE001
            log(f"  Gemini {model} failed: {e}")
    return None


def read_news(ticker, headlines):
    if not headlines:
        return {"catalyst": "none", "offering": False, "summary": "No headlines in the last 3 days", "by": "none"}
    kw = any(OFFERING_WORDS.search(h["title"]) for h in headlines)
    llm = read_news_llm(ticker, headlines)
    if llm:
        llm["offering"] = llm["offering"] or kw
        llm["by"] = "ai"
        return llm
    return {"catalyst": "unknown", "offering": kw, "summary": headlines[0]["title"][:140], "by": "headline"}


# ---------------------------------------------------------------- verdict

def decide(stats, news, filings, market):
    """Returns (verdict, [reasons]). First SKIP rule wins, then CAUTION flags, else GO."""
    skip, caution, good = [], [], []

    sales = [f for f in (filings or []) if f["kind"] == "sale"]
    shelves = [f for f in (filings or []) if f["kind"] == "shelf"]
    if sales and sales[0]["days_ago"] <= SALE_SKIP_DAYS:
        skip.append(f"share sale filed {sales[0]['days_ago']}d ago ({sales[0]['form']})")
    elif sales:
        caution.append(f"share sale filed {sales[0]['days_ago']}d ago ({sales[0]['form']})")
    if shelves:
        caution.append(f"can sell shares: {shelves[0]['form']} filed {shelves[0]['days_ago']}d ago")
    if filings is None:
        caution.append("SEC filings not checked")

    if news["offering"]:
        skip.append("offering or dilution in the news")
    if news["catalyst"] == "negative":
        skip.append("negative news")
    elif news["catalyst"] == "positive":
        good.append("positive news")
    elif news["catalyst"] == "unknown":
        caution.append("news not classified")

    if not stats:
        caution.append("no price data")
    else:
        if stats["dollar_vol"] is not None and stats["dollar_vol"] < THIN_DOLLAR_VOL:
            skip.append(f"thin: ${stats['dollar_vol'] / 1e6:.1f}M traded per day")
        if stats["gap_pct"] is not None and abs(stats["gap_pct"]) >= BIG_GAP_PCT:
            caution.append(f"big gap {stats['gap_pct']:+.0f}%")
        if stats["rel_vol"] is not None and stats["rel_vol"] >= 2:
            good.append(f"volume {stats['rel_vol']:.1f}x normal")

    if market["state"] == "DOWN":
        caution.append("market down")
    elif market["state"] == "UNKNOWN":
        caution.append("market not checked")

    if skip:
        return "SKIP", skip + caution
    if caution:
        return "CAUTION", caution + good
    if good:
        return "GO", good + ([f"market {market['state'].lower()}"])
    return "CAUTION", ["no catalyst and normal volume"]


# ---------------------------------------------------------------- output

ICON = {"GO": "🟢", "CAUTION": "🟡", "SKIP": "🔴", "UP": "🟢", "MIXED": "🟡", "DOWN": "🔴", "UNKNOWN": "⚪"}


def money(n):
    if n is None:
        return "n/a"
    return f"${n / 1e6:.1f}M" if n >= 1e6 else f"${n / 1e3:.0f}K"


def build_message(results, market, now=None):
    now = now or datetime.now(PT)
    lines = [
        f"🚦 CONTEXT CHECK  {now.strftime('%a %b %d, %I:%M %p').replace(' 0', ' ')} PT",
        f"Market: {ICON[market['state']]} {market['state']} ({market['text']})",
    ]
    for r in results:
        s, n = r["stats"], r["news"]
        lines += ["", f"{ICON[r['verdict']]} {r['ticker']}  {r['verdict']}", "Why: " + "; ".join(r["reasons"])]
        lines.append("News: " + n["summary"])
        if s:
            gap = f"{s['gap_pct']:+.1f}%" if s["gap_pct"] is not None else "n/a"
            rv = f"{s['rel_vol']:.1f}x" if s["rel_vol"] is not None else "n/a"
            price = s["live"] or s["close"]
            lines.append(f"Price {price:.2f} | Gap {gap} | Vol {rv} normal | {money(s['dollar_vol'])}/day")
        f = r["filings"]
        if f is None:
            lines.append("Filings: not checked")
        elif f:
            lines.append("Filings: " + ", ".join(f"{x['form']} {x['date'][5:]}" for x in f[:3]))
        else:
            lines.append("Filings: no offering filings in 30 days")
    lines += ["", "Context only. Your chart gives the entry."]
    return "\n".join(lines)


def send_telegram(text):
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log("Telegram secrets not set, skipping send")
        return False
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat, "text": text[:4000], "disable_web_page_preview": True},
        timeout=20,
    )
    log(f"Telegram: HTTP {r.status_code}")
    return r.ok


def merge_context(old, results, market, now_ts):
    """Keep recent verdicts for other tickers, replace the ones just checked."""
    tickers = {}
    for t, v in ((old or {}).get("tickers") or {}).items():
        if isinstance(v, dict) and now_ts - int(v.get("checked", 0)) < KEEP_HOURS * 3600:
            tickers[t] = v
    for r in results:
        tickers[r["ticker"]] = {
            "verdict": r["verdict"],
            "why": "; ".join(r["reasons"])[:200],
            "news": r["news"]["summary"][:140],
            "checked": now_ts,
        }
    return {"updated": now_ts, "market": {"state": market["state"], "text": market["text"]}, "tickers": tickers}


def save_context(results, market):
    """Write the verdicts to CTX_FILE on the CTX_BRANCH branch through the GitHub API."""
    token, repo = os.getenv("GITHUB_TOKEN"), os.getenv("GITHUB_REPOSITORY")
    if not token or not repo:
        log("No GITHUB_TOKEN/GITHUB_REPOSITORY, not saving context")
        return False
    api = f"https://api.github.com/repos/{repo}"
    hdr = {"authorization": f"Bearer {token}", "accept": "application/vnd.github+json"}
    # Make sure the branch exists.
    if requests.get(f"{api}/git/ref/heads/{CTX_BRANCH}", headers=hdr, timeout=20).status_code == 404:
        sha = os.getenv("GITHUB_SHA")
        r = requests.post(f"{api}/git/refs", headers=hdr, json={"ref": f"refs/heads/{CTX_BRANCH}", "sha": sha}, timeout=20)
        log(f"Create branch: HTTP {r.status_code}")
    old, file_sha = None, None
    r = requests.get(f"{api}/contents/{CTX_FILE}", headers=hdr, params={"ref": CTX_BRANCH}, timeout=20)
    if r.status_code == 200:
        file_sha = r.json().get("sha")
        try:
            old = json.loads(base64.b64decode(r.json()["content"]))
        except Exception:  # noqa: BLE001
            old = None
    data = merge_context(old, results, market, int(time.time()))
    body = {
        "message": "Update scalp context",
        "branch": CTX_BRANCH,
        "content": base64.b64encode(json.dumps(data, indent=1).encode()).decode(),
    }
    if file_sha:
        body["sha"] = file_sha
    r = requests.put(f"{api}/contents/{CTX_FILE}", headers=hdr, json=body, timeout=20)
    log(f"Save context: HTTP {r.status_code} {'' if r.ok else r.text[:200]}")
    return r.ok


def main():
    tickers = clean_tickers(os.getenv("INPUT_STOCKS") or " ".join(sys.argv[1:]))[:15]
    if not tickers:
        log("No tickers given")
        return 1
    import yfinance as yf

    log(f"Checking: {', '.join(tickers)}")
    market = market_status(yf)
    log(f"Market: {market['state']} - {market['text']}")
    results = []
    for t in tickers:
        log(f"[{t}]")
        stats, tk = fetch_prices(yf, t)
        headlines = fetch_news(tk)
        news = read_news(t, headlines)
        filings = fetch_filings(t)
        verdict, reasons = decide(stats, news, filings, market)
        log(f"  {verdict}: {'; '.join(reasons)} | headlines={len(headlines)} news_by={news['by']}")
        results.append({"ticker": t, "verdict": verdict, "reasons": reasons, "stats": stats, "news": news, "filings": filings})
    msg = build_message(results, market)
    log("\n" + msg + "\n")
    sent = send_telegram(msg)
    saved = save_context(results, market)
    return 0 if (sent or saved) else 1


if __name__ == "__main__":
    sys.exit(main())
