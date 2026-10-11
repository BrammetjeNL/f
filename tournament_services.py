import math
import random
import json
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
import db_manager
from tournament_models import Tournament, Participant, Match

def get_seeded_order(num_players: int) -> List[int]:
    """Generate the standard tournament seeding order for a power of 2."""
    order = [1]
    while len(order) < num_players:
        next_order = []
        target = len(order) * 2 + 1
        for seed in order:
            next_order.append(seed)
            next_order.append(target - seed)
        order = next_order
    return order

class TournamentService:
    @staticmethod
    async def get_player_rating(uuid: str, gamemode: str) -> int:
        """Get player rating/score for a specific gamemode from updated_players."""
        try:
            conn = db_manager.get_db()
            cursor = conn.cursor()
            cursor.execute("SELECT overall FROM updated_players WHERE uuid = ?", (uuid,))
            row = cursor.fetchone()
            conn.close()
            if row:
                return row['overall']
            return 0
        except Exception:
            return 0

    @staticmethod
    async def create_tournament(tournament: Tournament) -> None:
        try:
            conn = db_manager.get_db()
            cursor = conn.cursor()
            t_data = tournament.to_dict()
            columns = ", ".join(t_data.keys())
            placeholders = ", ".join(["?" for _ in t_data])
            params = list(t_data.values())
            with db_manager.db_lock:
                cursor.execute(f"INSERT OR REPLACE INTO tournaments ({columns}) VALUES ({placeholders})", params)
                conn.commit()
            conn.close()
        except Exception as e:
            print(f"Error creating tournament: {e}")

    @staticmethod
    async def get_tournament(t_id: str) -> Optional[Tournament]:
        try:
            conn = db_manager.get_db()
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM tournaments WHERE id = ?", (t_id,))
            row = cursor.fetchone()
            conn.close()
            if row:
                return Tournament.from_dict(dict(row))
            return None
        except Exception as e:
            print(f"Error getting tournament: {e}")
            return None

    @staticmethod
    async def add_participant(participant: Participant) -> None:
        try:
            conn = db_manager.get_db()
            cursor = conn.cursor()
            p_data = participant.to_dict()
            # checked_in in participant.to_dict is bool, convert to int for SQL
            p_data['checked_in'] = 1 if p_data.get('checked_in', False) else 0
            columns = ", ".join(p_data.keys())
            placeholders = ", ".join(["?" for _ in p_data])
            params = list(p_data.values())
            with db_manager.db_lock:
                cursor.execute(f"INSERT OR REPLACE INTO tournament_participants ({columns}) VALUES ({placeholders})", params)
                conn.commit()
            conn.close()
        except Exception as e:
            print(f"Error adding participant: {e}")

    @staticmethod
    async def get_participants(t_id: str) -> List[Participant]:
        try:
            conn = db_manager.get_db()
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM tournament_participants WHERE tournament_id = ?", (t_id,))
            rows = cursor.fetchall()
            conn.close()
            
            participants = []
            for row in rows:
                p_dict = dict(row)
                p_dict['checked_in'] = bool(p_dict['checked_in'])
                participants.append(Participant.from_dict(p_dict))
            return participants
        except Exception as e:
            print(f"Error getting participants: {e}")
            return []

    @staticmethod
    async def generate_bracket(t_id: str, seed_type: str = "seeded") -> List[Match]:
        tournament = await TournamentService.get_tournament(t_id)
        if not tournament:
            raise ValueError("Tournament not found")

        participants = await TournamentService.get_participants(t_id)
        # Only keep checked-in participants if status is CHECK_IN
        checked_in = [p for p in participants if p.status == "CHECKED_IN"]
        
        if len(checked_in) < 2:
            raise ValueError("Not enough participants checked in to generate bracket.")

        # Seeding
        if seed_type == "seeded":
            ratings = []
            for p in checked_in:
                rating = await TournamentService.get_player_rating(p.minecraft_uuid, tournament.gamemode)
                ratings.append((p, rating))
            ratings.sort(key=lambda x: x[1], reverse=True)
            sorted_participants = [x[0] for x in ratings]
        else:
            sorted_participants = checked_in.copy()
            random.shuffle(sorted_participants)

        conn = db_manager.get_db()
        cursor = conn.cursor()
        
        # Assign seed numbers
        with db_manager.db_lock:
            for idx, p in enumerate(sorted_participants):
                p.seed = idx + 1
                cursor.execute(
                    "UPDATE tournament_participants SET seed = ? WHERE tournament_id = ? AND player_id = ?",
                    (p.seed, t_id, p.player_id)
                )
            conn.commit()

        num_players = len(sorted_participants)
        next_power = 2 ** math.ceil(math.log2(num_players))
        
        seed_order = get_seeded_order(next_power)
        participant_map = {p.seed: p for p in sorted_participants}
        
        matches = []
        round_matches_count = next_power // 2
        
        with db_manager.db_lock:
            for i in range(round_matches_count):
                seed1 = seed_order[2 * i]
                seed2 = seed_order[2 * i + 1]
                
                p1 = participant_map.get(seed1)
                p2 = participant_map.get(seed2)
                
                match_id = f"{t_id}:1:{i+1}"
                
                if not p1 and not p2:
                    continue

                match = Match(
                    id=match_id,
                    tournament_id=t_id,
                    round=1,
                    player1_id=p1.player_id if p1 else None,
                    player2_id=p2.player_id if p2 else None,
                    player1_username=p1.minecraft_username if p1 else "BYE",
                    player2_username=p2.minecraft_username if p2 else "BYE",
                    status="WAITING_READY"
                )
                
                if not p1 or not p2:
                    match.status = "FINISHED"
                    match.winner_id = p1.player_id if p1 else p2.player_id

                matches.append(match)
                m_data = match.to_dict()
                columns = ", ".join(m_data.keys())
                placeholders = ", ".join(["?" for _ in m_data])
                params = list(m_data.values())
                cursor.execute(f"INSERT OR REPLACE INTO tournament_matches ({columns}) VALUES ({placeholders})", params)
            
            # Update tournament state
            cursor.execute(
                "UPDATE tournaments SET status = ?, started_at = ? WHERE id = ?",
                ('ACTIVE', datetime.now(timezone.utc).isoformat(), t_id)
            )
            conn.commit()
            
        conn.close()
        return matches

    @staticmethod
    async def get_match(match_id: str) -> Optional[Match]:
        try:
            conn = db_manager.get_db()
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM tournament_matches WHERE id = ?", (match_id,))
            row = cursor.fetchone()
            conn.close()
            if row:
                m_dict = dict(row)
                if 'score' in m_dict and m_dict['score']:
                    m_dict['score'] = json.loads(m_dict['score'])
                m_dict['is_loser_bracket'] = bool(m_dict['is_loser_bracket'])
                return Match.from_dict(m_dict)
            return None
        except Exception as e:
            print(f"Error getting match: {e}")
            return None

    @staticmethod
    async def update_match(match: Match) -> None:
        try:
            conn = db_manager.get_db()
            cursor = conn.cursor()
            m_data = match.to_dict()
            if 'score' in m_data:
                m_data['score'] = json.dumps(m_data['score'])
            m_data['is_loser_bracket'] = 1 if m_data.get('is_loser_bracket', False) else 0
            columns = ", ".join(m_data.keys())
            placeholders = ", ".join(["?" for _ in m_data])
            params = list(m_data.values())
            with db_manager.db_lock:
                cursor.execute(f"INSERT OR REPLACE INTO tournament_matches ({columns}) VALUES ({placeholders})", params)
                conn.commit()
            conn.close()
        except Exception as e:
            print(f"Error updating match: {e}")

    @staticmethod
    async def get_active_matches(t_id: str) -> List[Match]:
        try:
            conn = db_manager.get_db()
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM tournament_matches WHERE tournament_id = ?", (t_id,))
            rows = cursor.fetchall()
            conn.close()
            
            matches = []
            for row in rows:
                m_dict = dict(row)
                if 'score' in m_dict and m_dict['score']:
                    m_dict['score'] = json.loads(m_dict['score'])
                m_dict['is_loser_bracket'] = bool(m_dict['is_loser_bracket'])
                matches.append(Match.from_dict(m_dict))
            return matches
        except Exception as e:
            print(f"Error getting active matches: {e}")
            return []

    @staticmethod
    async def advance_tournament_bracket(t_id: str) -> None:
        """Scan active matches, progress winners, and generate next round matches if possible."""
        matches = await TournamentService.get_active_matches(t_id)
        tournament = await TournamentService.get_tournament(t_id)
        if not tournament:
            return

        current_round = tournament.current_round
        round_matches = [m for m in matches if m.round == current_round]
        
        if all(m.status == "FINISHED" for m in round_matches):
            next_round = current_round + 1
            winners = [m.winner_id for m in sorted(round_matches, key=lambda x: int(x.id.split(':')[-1])) if m.winner_id]
            
            conn = db_manager.get_db()
            cursor = conn.cursor()
            
            if len(winners) == 1:
                # Tournament finished!
                with db_manager.db_lock:
                    cursor.execute(
                        "UPDATE tournaments SET status = ?, ended_at = ? WHERE id = ?",
                        ('FINISHED', datetime.now(timezone.utc).isoformat(), t_id)
                    )
                    conn.commit()
                conn.close()
                winner_id = winners[0]
                await TournamentService.update_winner_stats(winner_id, t_id)
                return

            # Generate next round matches
            next_matches_count = len(winners) // 2
            with db_manager.db_lock:
                for i in range(next_matches_count):
                    p1_id = winners[2 * i]
                    p2_id = winners[2 * i + 1]
                    
                    p1_username = await TournamentService.get_minecraft_username(p1_id)
                    p2_username = await TournamentService.get_minecraft_username(p2_id)
                    
                    match_id = f"{t_id}:{next_round}:{i+1}"
                    match = Match(
                        id=match_id,
                        tournament_id=t_id,
                        round=next_round,
                        player1_id=p1_id,
                        player2_id=p2_id,
                        player1_username=p1_username,
                        player2_username=p2_username,
                        status="WAITING_READY"
                    )
                    m_data = match.to_dict()
                    if 'score' in m_data:
                        m_data['score'] = json.dumps(m_data['score'])
                    columns = ", ".join(m_data.keys())
                    placeholders = ", ".join(["?" for _ in m_data])
                    params = list(m_data.values())
                    cursor.execute(f"INSERT OR REPLACE INTO tournament_matches ({columns}) VALUES ({placeholders})", params)
                
                cursor.execute(
                    "UPDATE tournaments SET current_round = ? WHERE id = ?",
                    (next_round, t_id)
                )
                conn.commit()
                
            conn.close()

    @staticmethod
    async def get_minecraft_username(discord_id: str) -> str:
        try:
            conn = db_manager.get_db()
            cursor = conn.cursor()
            cursor.execute("SELECT username FROM verify WHERE discord_id = ?", (str(discord_id),))
            row = cursor.fetchone()
            conn.close()
            if row:
                return row['username']
            return "Unknown"
        except Exception:
            return "Unknown"

    @staticmethod
    async def update_winner_stats(player_id: str, t_id: str) -> None:
        """Update profile statistics for the tournament winner in updated_players."""
        try:
            conn = db_manager.get_db()
            cursor = conn.cursor()
            cursor.execute("SELECT uuid FROM verify WHERE discord_id = ?", (str(player_id),))
            row = cursor.fetchone()
            
            if not row:
                conn.close()
                return
            uuid = row['uuid']

            cursor.execute("SELECT * FROM updated_players WHERE uuid = ?", (uuid,))
            player_row = cursor.fetchone()
            if player_row:
                data = dict(player_row)
                data['tournament_wins'] = data.get('tournament_wins', 0) + 1
                data['tournament_finals'] = data.get('tournament_finals', 0) + 1
                data['tournament_streak'] = data.get('tournament_streak', 0) + 1
                
                # Check if these columns exist in SQLite, add them if missing
                cursor.execute("PRAGMA table_info(updated_players)")
                existing_cols = [r[1] for r in cursor.fetchall()]
                for col in ['tournament_wins', 'tournament_finals', 'tournament_streak']:
                    if col not in existing_cols:
                        cursor.execute(f"ALTER TABLE updated_players ADD COLUMN {col} INTEGER DEFAULT 0")
                
                set_clause = ", ".join([f"{k} = ?" for k in data.keys() if k != 'uuid'])
                params = [data[k] for k in data.keys() if k != 'uuid'] + [uuid]
                
                with db_manager.db_lock:
                    cursor.execute(f"UPDATE updated_players SET {set_clause} WHERE uuid = ?", params)
                    conn.commit()
            conn.close()
        except Exception as e:
            print(f"Error updating winner stats: {e}")
