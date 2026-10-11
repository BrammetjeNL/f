import sqlite3
import os
import json
import threading
from typing import Optional, Dict, Any, List

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "database.db")
GAMEMODES_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gamemodes.json")

db_lock = threading.Lock()

# Set WAL mode once at startup
try:
    _c = sqlite3.connect(DB_PATH, timeout=10.0)
    _c.execute("PRAGMA journal_mode=WAL;")
    _c.execute("PRAGMA busy_timeout=10000;")
    _c.close()
except Exception:
    pass

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    # Load gamemodes to create columns in updated_players
    gamemodes = []
    if os.path.exists(GAMEMODES_JSON):
        try:
            with open(GAMEMODES_JSON, "r", encoding="utf-8") as f:
                config = json.load(f)
                gamemodes = [gm["id"] for gm in config if "id" in gm]
        except Exception as e:
            print(f"Error loading gamemodes.json in db_manager: {e}")

    with db_lock:
        conn = get_db()
        cursor = conn.cursor()

        # 1. verify table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS verify (
            discord_id TEXT PRIMARY KEY,
            uuid TEXT,
            username TEXT
        )
        """)

        # 2. updated_players table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS updated_players (
            uuid TEXT PRIMARY KEY,
            username TEXT,
            overall REAL DEFAULT 0
        )
        """)

        # Add missing gamemode columns dynamically
        cursor.execute("PRAGMA table_info(updated_players)")
        existing_cols = [row[1] for row in cursor.fetchall()]
        for gm in gamemodes:
            if gm not in existing_cols:
                cursor.execute(f"ALTER TABLE updated_players ADD COLUMN {gm} TEXT DEFAULT ''")
            peak_col = f"{gm}_peak"
            if peak_col not in existing_cols:
                cursor.execute(f"ALTER TABLE updated_players ADD COLUMN {peak_col} TEXT DEFAULT ''")

        # 3. state_data table (for bot persistent state)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS state_data (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """)

        # 4. updated_cooldowns table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS updated_cooldowns (
            discord_id TEXT PRIMARY KEY,
            cooldowns TEXT
        )
        """)

        # 5. tester_stats table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS tester_stats (
            discord_id TEXT PRIMARY KEY,
            username TEXT,
            all_time_tests INTEGER DEFAULT 0,
            monthly_tests TEXT
        )
        """)

        # 6. tickets table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            channel_id TEXT PRIMARY KEY,
            tester_id TEXT,
            testee_id TEXT,
            gamemode TEXT,
            created_at TEXT
        )
        """)

        # 7. tournaments table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS tournaments (
            id TEXT PRIMARY KEY,
            name TEXT,
            status TEXT,
            size INTEGER,
            system TEXT,
            current_round INTEGER,
            total_rounds INTEGER,
            created_at TEXT
        )
        """)

        # 8. tournament_participants table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS tournament_participants (
            id TEXT PRIMARY KEY,
            tournament_id TEXT,
            player_id TEXT,
            username TEXT,
            seed INTEGER,
            checked_in INTEGER DEFAULT 0
        )
        """)

        # 9. tournament_matches table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS tournament_matches (
            id TEXT PRIMARY KEY,
            tournament_id TEXT,
            round INTEGER,
            match_number INTEGER,
            player1_id TEXT,
            player2_id TEXT,
            winner_id TEXT,
            status TEXT,
            channel_id TEXT,
            score TEXT,
            next_match_id TEXT,
            is_loser_bracket INTEGER DEFAULT 0
        )
        """)

        conn.commit()
        conn.close()

def migrate_from_firestore(firestore_db):
    """One-time migration from Firestore to SQLite"""
    conn = get_db()
    cursor = conn.cursor()
    
    # Check if already migrated
    cursor.execute("SELECT value FROM state_data WHERE key = 'firestore_migrated'")
    row = cursor.fetchone()
    if row and row['value'] == 'true':
        conn.close()
        print("✅ Firestore to SQLite migration already completed in the past.")
        return
        
    print("🚀 Starting Firestore to SQLite migration...")
    
    try:
        # Migrate verify
        print("Migrating 'verify' collection...")
        verify_docs = firestore_db.collection('verify').stream()
        for doc in verify_docs:
            data = doc.to_dict()
            discord_id = data.get('discord')
            if discord_id:
                cursor.execute(
                    "INSERT OR REPLACE INTO verify (discord_id, uuid, username) VALUES (?, ?, ?)",
                    (str(discord_id), data.get('uuid'), data.get('username'))
                )
                
        # Migrate updated_players
        print("Migrating 'updated_players' collection...")
        player_docs = firestore_db.collection('updated_players').stream()
        for doc in player_docs:
            data = doc.to_dict()
            uuid = data.get('uuid')
            if uuid:
                username = data.get('username', '')
                overall = data.get('overall', 0)
                cursor.execute(
                    "INSERT OR REPLACE INTO updated_players (uuid, username, overall) VALUES (?, ?, ?)",
                    (uuid, username, overall)
                )
                # Update gamemode columns
                for k, v in data.items():
                    if k not in ['uuid', 'username', 'overall']:
                        try:
                            cursor.execute(f"UPDATE updated_players SET {k} = ? WHERE uuid = ?", (v, uuid))
                        except Exception:
                            pass

        # Migrate state_data
        print("Migrating 'data/messages_and_channels' document...")
        state_doc = firestore_db.collection('data').document('messages_and_channels').get()
        if state_doc.exists:
            state_dict = state_doc.to_dict()
            for k, v in state_dict.items():
                cursor.execute(
                    "INSERT OR REPLACE INTO state_data (key, value) VALUES (?, ?)",
                    (k, json.dumps(v))
                )

        # Migrate updated_cooldowns
        print("Migrating 'updated_cooldowns' collection...")
        cooldown_docs = firestore_db.collection('updated_cooldowns').stream()
        for doc in cooldown_docs:
            data = doc.to_dict()
            discord_id = doc.id
            cursor.execute(
                "INSERT OR REPLACE INTO updated_cooldowns (discord_id, cooldowns) VALUES (?, ?)",
                (discord_id, json.dumps(data))
            )

        # Migrate tester_stats
        print("Migrating 'tester_stats' collection...")
        stats_docs = firestore_db.collection('tester_stats').stream()
        for doc in stats_docs:
            data = doc.to_dict()
            discord_id = data.get('discord_id') or doc.id
            monthly_tests = data.get('monthly_tests', {})
            cursor.execute(
                "INSERT OR REPLACE INTO tester_stats (discord_id, username, all_time_tests, monthly_tests) VALUES (?, ?, ?, ?)",
                (str(discord_id), data.get('username', ''), data.get('all_time_tests', 0), json.dumps(monthly_tests))
            )

        # Migrate tickets
        print("Migrating 'tickets' collection...")
        ticket_docs = firestore_db.collection('tickets').stream()
        for doc in ticket_docs:
            data = doc.to_dict()
            channel_id = data.get('channel_id') or doc.id
            created_at_val = data.get('created_at')
            created_at_str = created_at_val.isoformat() if hasattr(created_at_val, 'isoformat') else str(created_at_val)
            cursor.execute(
                "INSERT OR REPLACE INTO tickets (channel_id, tester_id, testee_id, gamemode, created_at) VALUES (?, ?, ?, ?, ?)",
                (str(channel_id), str(data.get('tester_id', '')), str(data.get('testee_id', '')), data.get('gamemode', ''), created_at_str)
            )

        # Migrate tournaments
        print("Migrating 'tournaments' collection...")
        tourney_docs = firestore_db.collection('tournaments').stream()
        for doc in tourney_docs:
            data = doc.to_dict()
            t_id = doc.id
            cursor.execute(
                "INSERT OR REPLACE INTO tournaments (id, name, status, size, system, current_round, total_rounds, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (t_id, data.get('name', ''), data.get('status', ''), data.get('size', 0), data.get('system', ''), data.get('current_round', 0), data.get('total_rounds', 0), str(data.get('created_at', '')))
            )

        # Migrate tournament_participants
        print("Migrating 'tournament_participants' collection...")
        part_docs = firestore_db.collection('tournament_participants').stream()
        for doc in part_docs:
            data = doc.to_dict()
            p_id = doc.id
            cursor.execute(
                "INSERT OR REPLACE INTO tournament_participants (id, tournament_id, player_id, username, seed, checked_in) VALUES (?, ?, ?, ?, ?, ?)",
                (p_id, data.get('tournament_id', ''), data.get('player_id', ''), data.get('username', ''), data.get('seed', 0), 1 if data.get('checked_in', False) else 0)
            )

        # Migrate tournament_matches
        print("Migrating 'tournament_matches' collection...")
        match_docs = firestore_db.collection('tournament_matches').stream()
        for doc in match_docs:
            data = doc.to_dict()
            m_id = doc.id
            cursor.execute(
                "INSERT OR REPLACE INTO tournament_matches (id, tournament_id, round, match_number, player1_id, player2_id, winner_id, status, channel_id, score, next_match_id, is_loser_bracket) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (m_id, data.get('tournament_id', ''), data.get('round', 1), data.get('match_number', 1), data.get('player1_id', ''), data.get('player2_id', ''), data.get('winner_id', ''), data.get('status', ''), str(data.get('channel_id', '')), json.dumps(data.get('score', [])), data.get('next_match_id', ''), 1 if data.get('is_loser_bracket', False) else 0)
            )

        # Mark as migrated
        cursor.execute("INSERT OR REPLACE INTO state_data (key, value) VALUES ('firestore_migrated', 'true')")
        conn.commit()
        print("✅ Firestore to SQLite migration completed successfully!")
    except Exception as e:
        print(f"⚠️ Warning: Firestore migration failed or skipped (Firestore might be offline): {e}")
    finally:
        conn.close()

# Initialize tables on import
init_db()
