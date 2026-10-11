import sys
import io

# Force stdout/stderr to use UTF-8 encoding to avoid encoding errors
if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, 'reconfigure'):
    try:
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

from flask import Flask, jsonify, request
from flask_cors import CORS
import sqlite3
import os
import json
from dotenv import load_dotenv

load_dotenv()

import logging
logging.getLogger('waitress.queue').setLevel(logging.ERROR)

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*", "allow_headers": "*"}})

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "database.db")

# Set WAL mode once at startup
try:
    _c = sqlite3.connect(DB_PATH, timeout=10.0)
    _c.execute("PRAGMA journal_mode=WAL;")
    _c.execute("PRAGMA busy_timeout=10000;")
    _c.close()
except Exception:
    pass

def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn

@app.route('/api/tournaments', methods=['GET'])
def get_tournaments():
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tournaments")
        rows = cursor.fetchall()
        return jsonify([dict(row) for row in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            try: conn.close()
            except Exception: pass

@app.route('/api/tournaments/<t_id>', methods=['GET'])
def get_tournament(t_id):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tournaments WHERE id = ?", (t_id,))
        row = cursor.fetchone()
        if row:
            return jsonify(dict(row))
        return jsonify({"error": "Tournament not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            try: conn.close()
            except Exception: pass

@app.route('/api/tournaments/<t_id>/bracket', methods=['GET'])
def get_bracket(t_id):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tournament_matches WHERE tournament_id = ?", (t_id,))
        rows = cursor.fetchall()
        
        matches = []
        for row in rows:
            m_dict = dict(row)
            if 'score' in m_dict and m_dict['score']:
                try:
                    m_dict['score'] = json.loads(m_dict['score'])
                except:
                    pass
            m_dict['is_loser_bracket'] = bool(m_dict['is_loser_bracket'])
            matches.append(m_dict)
            
        return jsonify({"tournamentId": t_id, "matches": matches})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            try: conn.close()
            except Exception: pass

@app.route('/api/tournaments/<t_id>/matches', methods=['GET'])
def get_matches(t_id):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tournament_matches WHERE tournament_id = ?", (t_id,))
        rows = cursor.fetchall()
        
        matches = []
        for row in rows:
            m_dict = dict(row)
            if 'score' in m_dict and m_dict['score']:
                try:
                    m_dict['score'] = json.loads(m_dict['score'])
                except:
                    pass
            m_dict['is_loser_bracket'] = bool(m_dict['is_loser_bracket'])
            matches.append(m_dict)
            
        return jsonify(matches)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            try: conn.close()
            except Exception: pass

@app.route('/api/tournaments/<t_id>/participants', methods=['GET'])
def get_participants(t_id):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tournament_participants WHERE tournament_id = ?", (t_id,))
        rows = cursor.fetchall()
        
        participants = []
        for row in rows:
            p_dict = dict(row)
            p_dict['checked_in'] = bool(p_dict['checked_in'])
            participants.append(p_dict)
            
        return jsonify(participants)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            try: conn.close()
            except Exception: pass

@app.route('/api/players/<player_id>/tournaments', methods=['GET'])
def get_player_tournaments(player_id):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tournament_participants WHERE player_id = ?", (player_id,))
        rows = cursor.fetchall()
        
        participants = []
        for row in rows:
            p_dict = dict(row)
            p_dict['checked_in'] = bool(p_dict['checked_in'])
            participants.append(p_dict)
            
        return jsonify(participants)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            try: conn.close()
            except Exception: pass

@app.route('/api/players', methods=['GET'])
def get_players():
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM updated_players ORDER BY overall DESC")
        rows = cursor.fetchall()
        
        players = []
        for row in rows:
            p_dict = dict(row)
            p_dict['id'] = p_dict['uuid']
            players.append(p_dict)
        return jsonify(players)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            try: conn.close()
            except Exception: pass

@app.route('/api/players/search', methods=['GET'])
def search_player():
    username = request.args.get('username', '')
    if not username:
        return jsonify({"error": "No username query provided"}), 400
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM updated_players WHERE username LIKE ? LIMIT 1", (username,))
        row = cursor.fetchone()
        if row:
            p_dict = dict(row)
            p_dict['id'] = p_dict['uuid']
            
            # Calculate 1-based rank by overall score descending
            cursor.execute(
                "SELECT COUNT(*) FROM updated_players WHERE overall > ? OR (overall = ? AND uuid < ?)",
                (p_dict['overall'], p_dict['overall'], p_dict['uuid'])
            )
            rank = cursor.fetchone()[0] + 1
            p_dict['overall'] = rank
            
            return jsonify(p_dict)
        return jsonify({"error": "Player not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            try: conn.close()
            except Exception: pass

@app.route('/api/profile/<username>', methods=['GET'])
def get_profile(username):
    if not username:
        return jsonify({"error": "Username is required"}), 400
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Look up player case-insensitively using LIKE on username or uuid
        cursor.execute("SELECT * FROM updated_players WHERE username LIKE ? OR uuid LIKE ? LIMIT 1", (username, username))
        row = cursor.fetchone()
        
        if not row:
            return jsonify({"error": "Player not found"}), 404
            
        player_data = dict(row)
        
        # Parse gamemodes
        gamemodes = [
            "diamace", "sword", "diamondsmp", "mace", "spearmace", "axe",
            "smp", "nethop", "diapot", "uhc", "ogv", "creeper", "speed", "vanilla", "cart"
        ]
        
        # Helper to get points for tier
        def get_points_for_tier(tier):
            tier_points = {
                "HT1": 60, "LT1": 45,
                "HT2": 30, "LT2": 20,
                "HT3": 10, "LT3": 6,
                "HT4": 4,  "LT4": 3,
                "HT5": 2,  "LT5": 1
            }
            return tier_points.get(tier.upper(), 0) if tier else 0

        # Helper to parse tier string (e.g. "HT3" -> tier=3, pos=0)
        import re
        def parse_tier(tier_str):
            if not tier_str:
                return None
            match = re.match(r"^(HT|LT)([1-5])$", tier_str, re.IGNORECASE)
            if not match:
                return None
            return {
                "tier": int(match.group(2)),
                "pos": 0 if match.group(1).upper() == "HT" else 1
            }

        # Calculate points (using peak tier, or current tier if peak is not set)
        calculated_cart = 0
        calculated_pvp = 0
        
        cart_val = player_data.get("cart")
        if cart_val:
            cart_peak = player_data.get("cart_peak") or cart_val
            calculated_cart += get_points_for_tier(cart_peak)
            
        for mode in gamemodes:
            if mode != "cart":
                current_tier = player_data.get(mode)
                if current_tier:
                    peak_tier = player_data.get(f"{mode}_peak") or current_tier
                    calculated_pvp += get_points_for_tier(peak_tier)
                    
        total_points = calculated_cart + calculated_pvp
        
        # Build rankings
        rankings = {}
        for mode in gamemodes:
            current_tier_str = player_data.get(mode)
            peak_tier_str = player_data.get(f"{mode}_peak")
            
            if current_tier_str:
                current_parsed = parse_tier(current_tier_str)
                peak_parsed = parse_tier(peak_tier_str) or current_parsed
                if current_parsed:
                    attained_ts = "0"
                    try:
                        cursor.execute("SELECT created_at FROM tickets WHERE (testee_id = ? OR testee_id = ?) AND gamemode = ? ORDER BY created_at DESC LIMIT 1", (player_data.get("uuid", ""), player_data.get("username", ""), mode))
                        t_row = cursor.fetchone()
                        if t_row and t_row[0]:
                            attained_ts = str(int(t_row[0]))
                    except Exception:
                        pass
                    if attained_ts == "0":
                        import time
                        attained_ts = str(int(time.time()))

                    rankings[mode] = {
                        "tier": current_parsed["tier"],
                        "pos": current_parsed["pos"],
                        "peak_tier": peak_parsed["tier"],
                        "peak_pos": peak_parsed["pos"],
                        "attained": attained_ts,
                        "retired": "false"
                    }
                    
        # Calculate the player's leaderboard rank (1-based, sorted by overall DESC)
        raw_overall = player_data.get("overall") or 0
        cursor.execute(
            "SELECT COUNT(*) FROM updated_players WHERE overall > ? OR (overall = ? AND uuid < ?)",
            (raw_overall, raw_overall, player_data.get("uuid", ""))
        )
        leaderboard_rank = cursor.fetchone()[0] + 1

        response_data = {
          "name": player_data.get("username"),
          "region": "EU",
          "points": total_points,
          "overall": leaderboard_rank,
          "rankings": rankings
        }
        
        return jsonify(response_data)
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            try: conn.close()
            except Exception: pass




@app.route('/api/admin/set_peak', methods=['POST'])
def admin_set_peak():
    conn = None
    try:
        data = request.get_json(force=True)
        username = data.get('username')
        gamemode = data.get('gamemode')
        peak_tier = data.get('peak_tier')
        if not username or not gamemode or not peak_tier:
            return jsonify({"error": "Missing parameters"}), 400
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(f"UPDATE updated_players SET {gamemode.lower()}_peak = ? WHERE username LIKE ?", (peak_tier.upper(), f"%{username}%"))
        conn.commit()
        updated = cursor.rowcount
        return jsonify({"success": True, "updated": updated})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            try: conn.close()
            except Exception: pass


if __name__ == '__main__':
    port = int(os.environ.get('PORT', os.environ.get('SERVER_PORT', 5095)))

    # Start LocalTunnel using raw Python sockets (no external tools needed)
    # Implements the localtunnel protocol directly
    try:
        import socket as _socket
        import threading as _threading
        import urllib.request as _urllib

        def _tunnel_slot(ip, srv_port, local_port):
            """One tunnel slot: connects to localtunnel server, waits for request, proxies to Flask"""
            fail_count = 0
            while fail_count < 3:
                srv = None
                loc = None
                try:
                    srv = _socket.create_connection((ip, srv_port), timeout=15)
                    srv.settimeout(300)
                    chunk = srv.recv(65536)
                    if not chunk:
                        raise ConnectionError("empty")
                    loc = _socket.create_connection(('127.0.0.1', local_port), timeout=5)
                    loc.sendall(chunk)
                    fail_count = 0
                    done = _threading.Event()
                    def _pipe(src, dst):
                        try:
                            while not done.is_set():
                                src.settimeout(30)
                                data = src.recv(65536)
                                if not data:
                                    break
                                dst.sendall(data)
                        except Exception:
                            pass
                        finally:
                            done.set()
                    t1 = _threading.Thread(target=_pipe, args=(srv, loc), daemon=True)
                    t2 = _threading.Thread(target=_pipe, args=(loc, srv), daemon=True)
                    t1.start(); t2.start()
                    t1.join(); t2.join()
                except _socket.timeout:
                    fail_count += 1
                except Exception:
                    fail_count += 1
                    import time; time.sleep(1)
                finally:
                    for s in [srv, loc]:
                        if s:
                            try: s.close()
                            except: pass

        def _update_gist(tunnel_url):
            """Update GitHub Gist with current tunnel URL so Vercel can discover it."""
            try:
                github_token = os.environ.get('GITHUB_TOKEN', '')
                gist_id = os.environ.get('GITHUB_GIST_ID', '37cf458e0886fb731a8f0521d0a6544f')
                if not github_token:
                    print("[gist] No GITHUB_TOKEN set, skipping gist update")
                    return
                payload = json.dumps({
                    "files": {
                        "gistfile1.txt": {
                            "content": json.dumps({"url": tunnel_url})
                        }
                    }
                }).encode()
                req = _urllib.Request(
                    f"https://api.github.com/gists/{gist_id}",
                    data=payload,
                    method='PATCH',
                    headers={
                        "Authorization": f"token {github_token}",
                        "Accept": "application/vnd.github.v3+json",
                        "Content-Type": "application/json",
                        "User-Agent": "nl-tiers-bot/1.0"
                    }
                )
                with _urllib.urlopen(req, timeout=10) as r:
                    r.read()
                print(f"✅ Updated Gist with tunnel URL: {tunnel_url}")
            except Exception as e:
                print(f"[gist] Failed to update gist: {e}")

        def _run_localtunnel(subdomain, local_port):
            import time
            while True:
                try:
                    req = _urllib.Request(
                        f"https://localtunnel.me/?new=1&subdomain={subdomain}",
                        headers={"Accept": "application/json", "User-Agent": "python/1.0"}
                    )
                    with _urllib.urlopen(req, timeout=15) as r:
                        raw = r.read()
                    info = json.loads(raw)
                    turl  = info.get("url", "")
                    tip   = info.get("ip", "localtunnel.me")
                    tport = int(info.get("port", 0))
                    nconn = min(int(info.get("max_conn_count", 5)), 5)
                    print(f"✅ LocalTunnel active: {turl}  (server: {tip}:{tport}  slots: {nconn})")
                    # Update GitHub Gist so Vercel proxy knows current URL
                    _threading.Thread(target=_update_gist, args=(turl,), daemon=True).start()
                    slots = [
                        _threading.Thread(target=_tunnel_slot, args=(tip, tport, local_port), daemon=True)
                        for _ in range(nconn)
                    ]
                    for s in slots: s.start()
                    for s in slots: s.join()
                    print("[localtunnel] Tunnel slots disconnected, reconnecting...")
                except Exception as e:
                    print(f"[localtunnel] Error: {e} — retrying in 5s...")
                    time.sleep(5)








        _t = _threading.Thread(target=_run_localtunnel, args=("nl-tiers-db-api-prod", 5095), daemon=True)
        _t.start()
    except Exception as e:
        print(f"⚠️ Failed to start tunnel: {e}")

    try:
        from waitress import serve
        print(f"🚀 Starting Waitress WSGI server on port {port} (threads=8, connection_limit=100, channel_timeout=5)...")
        serve(app, host='0.0.0.0', port=port, threads=8, connection_limit=100, channel_timeout=5, cleanup_interval=5)
    except ImportError:
        print(f"⚠️ Waitress not installed, starting Flask fallback on port {port}...")
        app.run(host='0.0.0.0', port=port, threaded=True)


