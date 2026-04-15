"""
Kalshi Bot Dashboard — Flask backend
Reads from trades_momentum.db (read-only) and serves live stats to the browser.
"""

import sqlite3
import json
import os
import sys
import time
from datetime import datetime, timezone, date
from flask import Flask, render_template, Response, jsonify

app = Flask(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
DB_PATH              = os.path.expanduser("~/kalshi_momentum/trades_momentum.db")
POSITION_FILE        = os.path.expanduser("~/kalshi_momentum/current_position.json")
STARTING_CAPITAL     = 50.00   # fallback only — real balance fetched from Kalshi API
PAPER_MODE           = 0       # 0 = live trades, 1 = paper trades
SSE_INTERVAL         = 12      # seconds between SSE pushes

# ── Kalshi API balance ─────────────────────────────────────────────────────────
# Reuse the bot's kalshi_client so we don't duplicate auth logic
sys.path.insert(0, os.path.expanduser("~/kalshi_momentum"))
_kalshi_balance_cache = {"value": None, "ts": 0}

def get_live_balance() -> float:
    """Fetch real Kalshi account balance, cached for 30 seconds."""
    now = time.time()
    if now - _kalshi_balance_cache["ts"] < 30 and _kalshi_balance_cache["value"] is not None:
        return _kalshi_balance_cache["value"]
    try:
        import kalshi_client
        resp = kalshi_client.get_balance()
        cents = resp.get("balance", 0)
        bal = cents / 100.0
        _kalshi_balance_cache["value"] = bal
        _kalshi_balance_cache["ts"] = now
        return bal
    except Exception:
        # Fall back to DB calculation if API fails
        return None


# ── Database helpers ───────────────────────────────────────────────────────────

def get_db():
    """Open DB in read-only mode — never blocks the bot's writes."""
    uri = f"file:{DB_PATH}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


def get_stats() -> dict:
    try:
        con = get_db()
        cur = con.cursor()

        # All-time totals
        row = cur.execute("""
            SELECT
                COUNT(*)                                        AS trade_count,
                ROUND(SUM(pnl), 2)                              AS total_pnl,
                ROUND(AVG(CASE WHEN pnl > 0 THEN pnl END), 3)  AS avg_win,
                ROUND(AVG(CASE WHEN pnl < 0 THEN pnl END), 3)  AS avg_loss,
                ROUND(MAX(pnl), 3)                              AS best_trade,
                ROUND(MIN(pnl), 3)                              AS worst_trade,
                ROUND(SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END), 2) AS gross_wins,
                ROUND(ABS(SUM(CASE WHEN pnl < 0 THEN pnl ELSE 0 END)), 2) AS gross_losses,
                COUNT(CASE WHEN pnl > 0 THEN 1 END)            AS win_count
            FROM trades WHERE paper=?
        """, (PAPER_MODE,)).fetchone()

        trade_count  = row["trade_count"] or 0
        total_pnl    = row["total_pnl"] or 0.0
        avg_win      = row["avg_win"] or 0.0
        avg_loss     = row["avg_loss"] or 0.0
        best_trade   = row["best_trade"] or 0.0
        worst_trade  = row["worst_trade"] or 0.0
        gross_wins   = row["gross_wins"] or 0.0
        gross_losses = row["gross_losses"] or 0.0
        win_count    = row["win_count"] or 0

        win_rate      = round(win_count / trade_count * 100, 1) if trade_count else 0.0
        profit_factor = round(gross_wins / gross_losses, 2) if gross_losses else 0.0
        live_bal  = get_live_balance()
        portfolio = live_bal if live_bal is not None else round(STARTING_CAPITAL + total_pnl, 2)

        # Today's P&L
        today_row = cur.execute("""
            SELECT ROUND(SUM(pnl), 2) AS today_pnl, COUNT(*) AS today_trades
            FROM trades
            WHERE paper=? AND DATE(exit_time) = DATE('now')
        """, (PAPER_MODE,)).fetchone()
        today_pnl    = today_row["today_pnl"] or 0.0
        today_trades = today_row["today_trades"] or 0

        # Exit reason breakdown
        reasons = cur.execute("""
            SELECT exit_reason,
                   COUNT(*) AS cnt,
                   ROUND(SUM(pnl), 2) AS total,
                   ROUND(AVG(pnl), 3) AS avg
            FROM trades WHERE paper=?
            GROUP BY exit_reason
            ORDER BY cnt DESC
        """, (PAPER_MODE,)).fetchall()

        con.close()

        return {
            "trade_count":   trade_count,
            "total_pnl":     total_pnl,
            "avg_win":       avg_win,
            "avg_loss":      avg_loss,
            "best_trade":    best_trade,
            "worst_trade":   worst_trade,
            "gross_wins":    gross_wins,
            "gross_losses":  gross_losses,
            "win_rate":      win_rate,
            "profit_factor": profit_factor,
            "portfolio":     portfolio,
            "today_pnl":     today_pnl,
            "today_trades":  today_trades,
            "exit_reasons":  [dict(r) for r in reasons],
        }
    except Exception as e:
        return {"error": str(e)}


def get_recent_trades(limit: int = 20) -> list:
    try:
        con = get_db()
        rows = con.execute("""
            SELECT id, ticker, side, entry_price, exit_price,
                   contracts, pnl, exit_time, exit_reason, imbalance
            FROM trades
            WHERE paper=?
            ORDER BY exit_time DESC
            LIMIT ?
        """, (PAPER_MODE, limit)).fetchall()
        con.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def get_equity_curve() -> list:
    try:
        con = get_db()
        rows = con.execute("""
            SELECT exit_time, pnl
            FROM trades
            WHERE paper=?
            ORDER BY exit_time ASC
            LIMIT 500
        """, (PAPER_MODE,)).fetchall()
        con.close()

        cumulative = STARTING_CAPITAL
        result = [{"time": "Start", "value": STARTING_CAPITAL}]
        for r in rows:
            cumulative += r["pnl"]
            result.append({
                "time":  r["exit_time"][:16].replace("T", " ") if r["exit_time"] else "",
                "value": round(cumulative, 2)
            })
        return result
    except Exception:
        return []


def get_current_position() -> dict | None:
    """Read sidecar JSON file written by the bot."""
    try:
        if not os.path.exists(POSITION_FILE):
            return None
        with open(POSITION_FILE) as f:
            data = json.load(f)
        if not data or not data.get("ticker"):
            return None
        return data
    except Exception:
        return None


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/stats")
def api_stats():
    return jsonify(get_stats())


@app.route("/api/trades")
def api_trades():
    return jsonify(get_recent_trades())


@app.route("/api/equity")
def api_equity():
    return jsonify(get_equity_curve())


@app.route("/api/position")
def api_position():
    return jsonify(get_current_position())


@app.route("/stream")
def stream():
    """Server-Sent Events — pushes all dashboard data every SSE_INTERVAL seconds."""
    def event_generator():
        while True:
            try:
                payload = {
                    "stats":    get_stats(),
                    "trades":   get_recent_trades(20),
                    "equity":   get_equity_curve(),
                    "position": get_current_position(),
                    "ts":       datetime.now(timezone.utc).isoformat(),
                }
                yield f"data: {json.dumps(payload)}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
            time.sleep(SSE_INTERVAL)

    return Response(
        event_generator(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True, debug=False)
