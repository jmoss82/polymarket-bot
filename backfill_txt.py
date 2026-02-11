"""One-time backfill of paper_log.txt from paper_log.jsonl"""
import json
from paper_trader import fmt_et, TRADES_TXT

with open("data/paper_log.jsonl") as f:
    events = [json.loads(line) for line in f]

with open(TRADES_TXT, "w") as out:
    for e in events:
        ts = fmt_et(e["ts"])
        t = e["type"]
        if t == "interval_start":
            slug = e.get("slug", "")
            bank = e.get("bankroll", 0)
            out.write(f"\n[{ts}] === New Interval: {slug} | Bankroll: ${bank:.2f} ===\n")
        elif t == "open_price":
            out.write(f"[{ts}] Open: ${e.get('price', 0):,.2f}\n")
        elif t == "entry":
            side = e.get("side", "")
            strength = e.get("strength", "")
            ep = e.get("entry_price", 0)
            btc = e.get("btc_at_entry", 0)
            move = e.get("move_at_entry", 0)
            edge = e.get("edge", 0)
            elapsed = e.get("elapsed", 0)
            out.write(f"[{ts}] ENTRY: {strength} {side} @ {ep:.3f} | BTC ${btc:,.2f} ({move:+.3f}%) | Edge: {edge:+.3f} | {elapsed:.0f}s in\n")
        elif t == "resolve":
            result = "WIN" if e.get("won") else "LOSS"
            bo = e.get("btc_open", 0)
            bc = e.get("btc_close", 0)
            pnl = e.get("pnl", 0)
            bank = e.get("bankroll", 0)
            out.write(f"[{ts}] {result}: BTC ${bo:,.2f} -> ${bc:,.2f} | P&L ${pnl:+.2f} | Bankroll: ${bank:.2f}\n")

print("Done - wrote", TRADES_TXT)
