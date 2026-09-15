"""Server-side arbitrage check — run by GitHub Actions every 15 minutes (see .github/workflows/monitor.yml).

Reads monitor_config.json, fetches live spot, computes fair vs market forward for each pair with the
same engine as the website (fx_arbitrage.py), writes signals.json (+ signals_history.json), and sends a
Telegram message when an opportunity appears and quotes_are_real is true.

Environment (optional): TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.
Run locally:  python monitor_check.py
"""
import json, os, sys, urllib.request, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fx_arbitrage as fx  # noqa: E402

CFG = json.load(open(os.path.join(HERE, "monitor_config.json"), encoding="utf-8"))


def fetch_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "arbitrage-monitor/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def fetch_spot():
    for url in CFG["spot_sources"]:
        try:
            j = fetch_json(url)
            rates = j.get("rates") or j.get("conversion_rates")
            if rates and "EUR" in rates:
                when = j.get("date") or j.get("time_last_update_utc") or dt.datetime.utcnow().isoformat() + "Z"
                return {k: float(rates[k]) for k in ("EUR", "GBP", "JPY", "INR") if k in rates}, str(when), url.split("/")[2]
        except Exception as e:  # noqa: BLE001
            print("spot source failed:", url, e)
    return None, None, None


def spot_for(pair, usd_base):
    b, q = pair.split("/")
    return (1.0 / usd_base[b]) if q == "USD" else usd_base[q]


def main():
    usd_base, when, source = fetch_spot()
    live = usd_base is not None
    if not live:
        usd_base, when, source = CFG["sample_spot_usd_base"], "sample", "sample"
    rows, alerts = [], []
    for p in CFG["pairs"]:
        b, q = p["pair"].split("/")
        S = spot_for(p["pair"], usd_base)
        rb, rq = CFG["rates"][b], CFG["rates"][q]
        m = fx.MarketInputs(p["pair"], CFG["tenor_days"], S, S + p["forward_points"], rb["rate"] / 100, rq["rate"] / 100, rb["basis"], rq["basis"])
        cc = CFG["costs"].get(p.get("cost_profile", "bank"), CFG["costs"]["bank"])
        c = fx.CostInputs(cc["ss"], cc["fs"], cc["bs"], cc["ds"], cc["mp"], cc["mf"], cc["fee"])
        par = fx.analyse_parity(m)
        d = par.direction if par.direction != "none" else "borrow_quote"
        pnl = fx.trade_pnl(m, c, d, 1.0)
        basis = m.basis_quote if d == "borrow_quote" else m.basis_base
        net_bp = fx.annualise_bps(pnl["net"], m.days, basis)
        opp = net_bp > CFG["alert_threshold_bp"] and not par.flags
        row = dict(pair=p["pair"], spot=S, forward_points=p["forward_points"], market_forward=m.fwd_mid, fair_forward=par.f_fair,
                   gap_bp=par.mispricing_bps_ann, net_bp=net_bp, direction=d if abs(par.mispricing_bps_ann) > 0.01 else "none",
                   opportunity=opp, flags=par.flags)
        rows.append(row)
        if opp:
            alerts.append(row)

    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    # Keep the repo quiet: rewrite signals.json only when the alert set changes or once an hour.
    try:
        old = json.load(open(os.path.join(HERE, "signals.json")))
        old_alerts = sorted(a["pair"] for a in old.get("alerts", []))
        age_min = (dt.datetime.fromisoformat(now) - dt.datetime.fromisoformat(old["checked_at"])).total_seconds() / 60
        if old_alerts == sorted(a["pair"] for a in alerts) and age_min < 55 and old.get("spot_live") == live:
            print(f"no change in alerts and last write {age_min:.0f} min ago — not rewriting signals.json")
            return notify(alerts, source, when)
    except Exception:  # noqa: BLE001
        pass
    out = dict(checked_at=now, spot_live=live, spot_source=source, spot_as_of=when, spot_usd_base=usd_base,
               quotes_are_real=CFG["quotes_are_real"], quotes_as_of=CFG["quotes_as_of"],
               threshold_bp=CFG["alert_threshold_bp"], rows=rows, alerts=alerts)
    json.dump(out, open(os.path.join(HERE, "signals.json"), "w"), indent=1)

    hist_path = os.path.join(HERE, "signals_history.json")
    try:
        hist = json.load(open(hist_path))
    except Exception:  # noqa: BLE001
        hist = []
    hist.append(dict(t=now, live=live, net_bp={r["pair"]: round(r["net_bp"], 3) for r in rows}, alerts=[a["pair"] for a in alerts]))
    json.dump(hist[-400:], open(hist_path, "w"))

    print(f"{now}  spot={'live' if live else 'SAMPLE'} ({source})")
    for r in rows:
        print(f"  {r['pair']:8s} spot {r['spot']:.5f} fair {r['fair_forward']:.5f} mkt {r['market_forward']:.5f} gap {r['gap_bp']:+.2f} bp net {r['net_bp']:+.2f} bp {'OPPORTUNITY' if r['opportunity'] else ''} {'FLAG' if r['flags'] else ''}")

    notify(alerts, source, when)


def notify(alerts, source, when):
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if alerts and CFG["quotes_are_real"] and tok and chat:
        lines = ["FX arbitrage alert"] + [
            f"{a['pair']}: {a['net_bp']:+.2f} bp/yr after costs — " +
            (f"borrow {a['pair'].split('/')[1]}, sell {a['pair'].split('/')[0]} fwd" if a["direction"] == "borrow_quote" else f"borrow {a['pair'].split('/')[0]}, buy {a['pair'].split('/')[0]} fwd") +
            f" (spot {a['spot']:.5g}, gap {a['gap_bp']:+.2f} bp)" for a in alerts
        ] + [f"quotes as of {CFG['quotes_as_of']} · spot {source} {when}"]
        body = json.dumps({"chat_id": chat, "text": "\n".join(lines)}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{tok}/sendMessage", data=body, headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=15)
            print("telegram sent")
        except Exception as e:  # noqa: BLE001
            print("telegram failed:", e)
    elif alerts and not CFG["quotes_are_real"]:
        print("opportunities found but quotes_are_real is false — no notification sent (sample quotes)")


if __name__ == "__main__":
    main()
