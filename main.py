from discord.ext import tasks
import sys
import io

# Force stdout/stderr to use UTF-8 encoding to avoid Windows/console encoding errors
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

import discord
from discord import app_commands
from discord.ext import commands, tasks
import asyncio
import json
import os
from typing import Optional
from datetime import datetime, timedelta, timezone
import firebase_admin
from firebase_admin import credentials, firestore
import requests
from dotenv import load_dotenv
import db_manager
import subprocess

# Load environment variables from .env file
load_dotenv()

TESTER_ROLE_NAME = "Verified Tester"
PERSIST_EVERY = 5
STAFF_ROLE_ID = 1498060823833346127
RESTRICTED_ROLE_ID = 1498060823711711403
STRAFFEN_CHANNEL_ID = 1536499801431085187

DISCORD_HELPER_ROLE_ID = 1498060823845797921
VERIFIED_TESTER_ROLE_ID = 1498060823833346122
FORCEEVAL_ROLE_IDS = [
    1498060823845797924, 1498060823845797926, 1498060823845797927, 
    1498060823845797928, 1498060823845797929, 1498060823862444122
]
EVAL_CATEGORIES = {
    "HT3": 1498060826521895092,
    "LT2": 1498060826865565806,
    "HT2": 1498060826865565806,
}

# Gamemodes (ids + display names loaded from gamemodes.json)
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_GAMEMODES_JSON = os.path.join(_BASE_DIR, "gamemodes.json")


def load_gamemodes_config() -> tuple[list[str], dict[str, str]]:
    """Load gamemode ids and displayName labels from gamemodes.json."""
    try:
        with open(_GAMEMODES_JSON, encoding="utf-8") as f:
            data = json.load(f)
    except OSError as e:
        raise RuntimeError(
            f"Could not read gamemodes config at {_GAMEMODES_JSON}: {e}"
        ) from e
    if not isinstance(data, list):
        raise ValueError("gamemodes.json must be a JSON array of {id, displayName} objects")
    ids: list[str] = []
    labels: dict[str, str] = {}
    for item in data:
        if not isinstance(item, dict) or "id" not in item or "displayName" not in item:
            raise ValueError("Each gamemode in gamemodes.json must have 'id' and 'displayName'")
        gid = str(item["id"]).strip().lower()
        ids.append(gid)
        labels[gid] = str(item["displayName"]).strip()
    if not ids:
        raise ValueError("gamemodes.json must contain at least one gamemode")
    return ids, labels


GAMEMODES, GAMEMODE_LABELS = load_gamemodes_config()


def gamemode_label(gamemode: str) -> str:
    """User-facing display name for a gamemode id (from gamemodes.json)."""
    return GAMEMODE_LABELS.get(gamemode.lower(), gamemode)

# Tier system
TIER_ROLES = [
    "LT5", "HT5", "LT4", "HT4", "LT3", "HT3", "LT2", "HT2", "LT1", "HT1"
]

# Tier points for overall calculation
TIER_POINTS = {
    "HT1": 60, "LT1": 45,
    "HT2": 30, "LT2": 20,
    "HT3": 10, "LT3": 6,
    "HT4": 4, "LT4": 3,
    "HT5": 2, "LT5": 1
}

# Colors
QUEUE_EMBED_COLOR = 0x5865F2
TICKET_EMBED_COLOR = 0x2ECC71

# Results channel
def _parse_results_channel_id() -> int:
    raw = os.environ.get("RESULTS_CHANNEL_ID", "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return 1536499642647191723


RESULTS_CHANNEL_ID: int = _parse_results_channel_id()

# Queue limit per gamemode
QUEUE_LIMIT = 20

# Only this Discord server may use the bot (slash commands + buttons/menus). Set env SERVER_ID to the guild id.
# If 0 or unset, all servers are allowed (not recommended for production).
def _parse_server_id() -> int:
    raw = os.environ.get("SERVER_ID", "").strip()
    if not raw:
        return 1498060823711711394
    try:
        return int(raw)
    except ValueError:
        return 1498060823711711394


SERVER_ID: int = _parse_server_id()

# Auto-created category names
WAITLISTS_CATEGORY_NAME = "Waitlists"

# Initialize Firebase
# Download your service account key from Firebase Console and save as 'serviceAccountKey.json'
if not firebase_admin._apps:
    try:
        if os.path.exists('serviceAccountKey.json'):
            cred = credentials.Certificate('serviceAccountKey.json')
        else:
            # Alternative: use application default credentials
            cred = credentials.ApplicationDefault()
        firebase_admin.initialize_app(cred)
    except Exception as e:
        print(f"Warning: Firebase not initialized: {e}")
        print("Download service account key from Firebase Console and save as 'serviceAccountKey.json'")

db = firestore.client()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True


async def push_app_commands(client: commands.Bot):
    """Register slash commands with Discord.

    Commands defined with @bot.tree.command are stored as *global* in the library.
    To publish them to one guild instantly, we must copy_global_to(guild) first,
    then sync(guild=...). Calling sync(guild=) alone uploads an empty list → 0 commands.

    We also clear global commands when SERVER_ID is set to avoid duplicates
    (commands showing up twice: once globally, once for the guild).
    """
    try:
        if SERVER_ID:
            guild_obj = discord.Object(id=SERVER_ID)
            client.tree.copy_global_to(guild=guild_obj)
            synced = await client.tree.sync(guild=guild_obj)
            print(
                f"✅ Guild-synced {len(synced)} slash command(s) to guild {SERVER_ID} (instant)!"
            )

            # Clear any previously registered global commands to prevent duplicates
            client.tree.clear_commands(guild=None)
            await client.tree.sync()
            print("🧹 Cleared global commands (prevents duplicate commands in Discord)")
        else:
            synced = await client.tree.sync()
            print(f"✅ Synced {len(synced)} global slash command(s)!")
        for cmd in synced:
            print(f"  - /{cmd.name}")
        return synced
    except Exception as e:
        print(f"❌ Failed to sync commands: {e}")
        raise


class TierListBot(commands.Bot):
    async def setup_hook(self):
        await super().setup_hook()
        print(f"📌 SERVER_ID for slash sync = {SERVER_ID} (must match your Discord server id)")
        
        # Register tournament command group
        from tournament import TournamentGroup
        self.tree.add_command(TournamentGroup())
        
        await push_app_commands(self)
        
        # Register persistent views
        self.add_view(SetupButtonView())
        for key in GAMEMODES:
            self.add_view(JoinQueueView(key))

        # Register persistent views for active tournaments/matches
        try:
            from tournament_views import TournamentRegistrationView, MatchTicketView
            from tournament import TournamentCheckInView
            
            # Retrieve all active tournaments from SQLite
            conn = db_manager.get_db()
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM tournaments WHERE status IN ('REGISTRATION_OPEN', 'CHECK_IN', 'ACTIVE')")
            active_tournaments = cursor.fetchall()
            for row in active_tournaments:
                t_id = row['id']
                self.add_view(TournamentRegistrationView(t_id))
                self.add_view(TournamentCheckInView(t_id))
                
            # Retrieve all active matches
            cursor.execute("SELECT id FROM tournament_matches WHERE status IN ('WAITING_READY', 'ACTIVE', 'DISPUTED')")
            active_matches = cursor.fetchall()
            for row in active_matches:
                m_id = row['id']
                self.add_view(MatchTicketView(m_id))
            conn.close()
                
            print("✅ Registered all persistent tournament/match views")
        except Exception as e:
            print(f"Error registering persistent tournament views in setup_hook: {e}")


bot = TierListBot(command_prefix="!", intents=intents)




async def global_guild_interaction_check(interaction: discord.Interaction) -> bool:
    """Restrict interactions to SERVER_ID when it is set (non-zero)."""
    if SERVER_ID == 0:
        return True
    if interaction.guild_id is not None and interaction.guild_id == SERVER_ID:
        return True
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "❌ This bot only works in the configured server.",
                ephemeral=True,
            )
    except Exception:
        pass
    return False


bot.tree.interaction_check = global_guild_interaction_check


async def safe_followup(interaction: discord.Interaction, content: str = None, **kwargs):
    """Safely sends a response/followup without crashing if the interaction token expired or response message was deleted."""
    try:
        if interaction.response.is_done():
            return await interaction.followup.send(content, **kwargs)
        else:
            return await interaction.response.send_message(content, **kwargs)
    except (discord.NotFound, discord.HTTPException):
        try:
            if interaction.channel and content:
                return await interaction.channel.send(content, **{k: v for k, v in kwargs.items() if k != 'ephemeral'})
        except Exception:
            pass
    except Exception as e:
        print(f"Failed to send safe followup: {e}")


@bot.tree.error
async def on_tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandInvokeError):
        error = error.original
    if isinstance(error, discord.NotFound) and error.code == 10008:
        print(f"⚠️ Interaction expired/404 for command /{interaction.command.name if interaction.command else 'unknown'}")
        return
    print(f"❌ Command error in /{interaction.command.name if interaction.command else 'unknown'}: {error}")


state_lock = asyncio.Lock()
state = {
    "waitlists": {},  # {key: [user_ids]}
    "queues": {},  # {key: [user_ids]}  max 20 each
    "active_testers": {},  # {key: [tester_ids]}
    "queue_messages": {},  # {key: {channel_id, message_id}}
    "ping_messages": {},  # {key: {channel_id, message_id}}
    "leaderboard_message": {}  # {"channel_id": channel_id, "message_id": message_id}
}
open_tickets = {}  # {channel_id: {user_id, gamemode}}
api_server_process = None


def auto_initialize_missing_peaks():
    """Automatically ensure every player has their peak tier initialized from their current tier or history."""
    try:
        conn = db_manager.get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM updated_players")
        rows = cursor.fetchall()
        if not rows:
            conn.close()
            return
        columns = [description[0] for description in cursor.description]
        
        updated_count = 0
        with db_manager.db_lock:
            for row in rows:
                p_dict = dict(zip(columns, row))
                changed = False
                for gm in GAMEMODES:
                    cur_tier = p_dict.get(gm, "") or ""
                    peak_key = f"{gm}_peak"
                    peak_tier = p_dict.get(peak_key, "") or ""
                    
                    if cur_tier and not peak_tier:
                        p_dict[peak_key] = cur_tier
                        changed = True
                    elif cur_tier and peak_tier:
                        if get_tier_points(cur_tier) > get_tier_points(peak_tier):
                            p_dict[peak_key] = cur_tier
                            changed = True
                
                if changed:
                    p_dict['overall'] = calculate_overall_score(p_dict)
                    set_clause = ", ".join([f"{k} = ?" for k in p_dict.keys() if k != 'uuid'])
                    params = [p_dict[k] for k in p_dict.keys() if k != 'uuid'] + [p_dict['uuid']]
                    cursor.execute(f"UPDATE updated_players SET {set_clause} WHERE uuid = ?", params)
                    updated_count += 1
                    
            conn.commit()
        conn.close()
        if updated_count > 0:
            print(f"✅ Automatically initialized missing peak tiers for {updated_count} players in SQLite!")
    except Exception as e:
        print(f"Error auto-initializing peak tiers: {e}")


async def execute_synctiers_logic(guild: discord.Guild) -> int:
    """Sync all members' Discord tier roles to database."""
    try:
        guild_members = guild.members
        member_map = {m.id: m for m in guild_members}
        
        updated = 0
        conn = db_manager.get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT discord_id, uuid, username FROM verify")
        rows = cursor.fetchall()
        
        for row in rows:
            discord_id = row['discord_id']
            uuid = row['uuid']
            username = row['username']
            
            if not discord_id or not uuid or not username:
                continue
                
            member = member_map.get(int(discord_id))
            if not member:
                continue
            
            current_tiers = {}
            for gamemode in GAMEMODES:
                label = format_gamemode_name(gamemode)
                found_tier = ""
                for tier in TIER_ROLES:
                    role_name = f"{tier} {label}"
                    role = discord.utils.find(lambda r: r.name.lower() == role_name.lower(), member.roles)
                    if role:
                        found_tier = tier
                        break
                current_tiers[gamemode] = found_tier
                
            player_data = await get_player_data(uuid)
            if not player_data:
                player_data = {'username': username, 'uuid': uuid, 'overall': 0}
                for g in GAMEMODES:
                    player_data[g] = ""
                    
            changed = False
            for key in list(player_data.keys()):
                if key not in ['uuid', 'username', 'overall'] and key not in GAMEMODES:
                    if player_data[key] != "":
                        player_data[key] = ""
                        changed = True

            for g in GAMEMODES:
                if player_data.get(g, "") != current_tiers[g]:
                    player_data[g] = current_tiers[g]
                    changed = True
            
            new_overall = calculate_overall_score(player_data)
            if player_data.get('overall', 0) != new_overall:
                player_data['overall'] = new_overall
                changed = True
                    
            if changed:
                player_data['username'] = username
                set_clause = ", ".join([f"{k} = ?" for k in player_data.keys() if k != 'uuid'])
                params = [player_data[k] for k in player_data.keys() if k != 'uuid'] + [uuid]
                
                with db_manager.db_lock:
                    cursor.execute(f"UPDATE updated_players SET {set_clause} WHERE uuid = ?", params)
                    conn.commit()
                updated += 1
        conn.close()
        return updated
    except Exception as e:
        print(f"Error in execute_synctiers_logic: {e}")
        return 0


async def execute_syncuuid_logic() -> tuple[int, int]:
    """Sync all Minecraft usernames with Mojang API for any name changes."""
    try:
        conn = db_manager.get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT discord_id, uuid, username FROM verify")
        verify_rows = cursor.fetchall()
        
        checked_count = len(verify_rows)
        updated_count = 0

        from concurrent.futures import ThreadPoolExecutor

        def _check_user(row):
            stored_username = row['username']
            uuid = row['uuid']
            if not uuid:
                return None
            current_username = get_minecraft_username_from_uuid(uuid)
            if current_username and current_username != stored_username:
                return (uuid, current_username, stored_username)
            return None

        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = [loop.run_in_executor(executor, _check_user, row) for row in verify_rows]
            results = await asyncio.gather(*futures)

        with db_manager.db_lock:
            for item in results:
                if item:
                    uuid, current_username, stored_username = item
                    try:
                        cursor.execute("UPDATE verify SET username = ? WHERE uuid = ?", (current_username, uuid))
                        cursor.execute("UPDATE updated_players SET username = ? WHERE uuid = ?", (current_username, uuid))
                        updated_count += 1
                    except Exception as e:
                        print(f"Error updating username for {stored_username}: {e}")
            conn.commit()
        conn.close()
        return checked_count, updated_count
    except Exception as e:
        print(f"Error in execute_syncuuid_logic: {e}")
        return 0, 0


@tasks.loop(hours=6)
async def auto_background_sync_task():
    try:
        guild = bot.get_guild(SERVER_ID)
        if guild:
            print("🔄 [AUTO-SYNC] Starting automatic background synctiers & syncuuid...")
            updated_tiers = await execute_synctiers_logic(guild)
            checked_uuids, updated_uuids = await execute_syncuuid_logic()
            if updated_tiers > 0:
                await update_leaderboard_message(guild)
            print(f"✅ [AUTO-SYNC] Done! Tiers updated: {updated_tiers} | UUIDs checked: {checked_uuids}, updated: {updated_uuids}")
    except Exception as e:
        print(f"❌ [AUTO-SYNC] Background sync error: {e}")

@auto_background_sync_task.before_loop
async def before_auto_background_sync():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    auto_initialize_missing_peaks()
    global api_server_process
    print(f'Logged in as {bot.user} (ID: {bot.user.id})')
    if not auto_background_sync_task.is_running():
        auto_background_sync_task.start()
    try:
        guild = bot.get_guild(SERVER_ID)
        if guild:
            await update_commandlist_message(guild)
    except Exception as e:
        print(f'Error updating commandlist on ready: {e}')
    print('------')
    # Run one-time Firestore migration (will skip if already done)
    # Migration check
    try:
        if not os.path.exists("db_migrated.flag"):
            db_manager.migrate_from_firestore(db)
            with open("db_migrated.flag", "w") as f:
                f.write("1")
    except Exception as e:
        print(f"Migration note: {e}")

    # Start Flask API server in the background automatically (only if not already running)
    if api_server_process is None or api_server_process.poll() is not None:
        try:
            api_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "api_server.py")
            api_server_process = subprocess.Popen([sys.executable, api_path], env=os.environ.copy())
            print("🚀 Started Flask API server in the background!")
        except Exception as e:
            print(f"⚠️ Failed to start Flask API server: {e}")

    load_state()
    if not autosave_task.is_running():
        autosave_task.start()
    if not sync_verify_cache_task.is_running():
        sync_verify_cache_task.start()

    await bot.change_presence(activity=discord.Game(name="Doing Tests 👑"))
    if SERVER_ID:
        g = bot.get_guild(SERVER_ID)
        print(f"✅ Server lock: only guild id {SERVER_ID} allowed ({g.name if g else 'not in cache yet'})")
    else:
        print("⚠️  SERVER_ID not set — bot accepts commands from any server")

    # Restore persistent views
    await restore_persistent_views()
    
    # Update live leaderboard
    if SERVER_ID:
        guild = bot.get_guild(SERVER_ID)
        if guild:
            await update_leaderboard_message(guild)
            await update_suslist_message(guild)
            await update_quota_message(guild)
    
    print("Bot is ready!")


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    """Real-time sync of tier roles from Discord to Firestore when roles change."""
    if before.roles == after.roles:
        return

    # Check if member is verified
    discord_id_str = str(after.id)
    if not await is_discord_verified(discord_id_str):
        return

    uuid = await get_verified_uuid(discord_id_str)
    username = await get_verified_username(discord_id_str)
    if not uuid or not username:
        return

    # Find tier roles of the user
    current_tiers = {}
    for gamemode in GAMEMODES:
        label = format_gamemode_name(gamemode)
        found_tier = ""
        for tier in TIER_ROLES:
            role_name = f"{tier} {label}"
            role = discord.utils.find(lambda r: r.name.lower() == role_name.lower(), after.roles)
            if role:
                found_tier = tier
                break

        current_tiers[gamemode] = found_tier

    player_data = await get_player_data(uuid)
    if not player_data:
        player_data = {
            'username': username,
            'uuid': uuid,
            'overall': 0
        }
        for g in GAMEMODES:
            player_data[g] = ""

    changed = False
    for g in GAMEMODES:
        if player_data.get(g, "") != current_tiers[g]:
            player_data[g] = current_tiers[g]
            changed = True

    if changed:
        player_data['overall'] = calculate_overall_score(player_data)
        player_data['username'] = username
        try:
            db.collection('updated_players').document(uuid).set(player_data)
            print(f"🔄 Real-time synced tiers for {username} ({after.display_name}) based on Discord role change")
        except Exception as e:
            print(f"Error saving updated player tier in on_member_update: {e}")



async def get_or_create_category(guild: discord.Guild, name: str) -> discord.CategoryChannel:
    """Find a category by exact name or create it (requires bot: Manage Channels)."""
    existing = discord.utils.get(guild.categories, name=name)
    if existing:
        return existing
    return await guild.create_category(name)


async def restore_persistent_views():
    """Restore views for persistent messages after bot restart"""
    try:
        # Restore setup message view
        if "setup_message" in state and state["setup_message"]:
            msg_info = state["setup_message"]
            for guild in bot.guilds:
                channel = guild.get_channel(msg_info["channel_id"])
                if channel:
                    try:
                        msg = await channel.fetch_message(msg_info["message_id"])
                        view = SetupButtonView()
                        await msg.edit(view=view)
                        print(f"✅ Restored setup message view in {channel.name}")
                    except Exception as e:
                        print(f"Could not restore setup message: {e}")
                    break
        
        # Restore queue message views
        if "queue_messages" in state:
            for key, msg_info in state["queue_messages"].items():
                for guild in bot.guilds:
                    channel = guild.get_channel(msg_info["channel_id"])
                    if channel:
                        try:
                            msg = await channel.fetch_message(msg_info["message_id"])
                            
                            # Check if queue is active (has active testers)
                            active_testers = state.get("active_testers", {}).get(key, [])
                            
                            # Update the message with the correct embed and view
                            embed = await get_queue_embed_for_key(guild, key)
                            
                            if active_testers and len(active_testers) > 0:
                                # Queue is active, add buttons
                                view = JoinQueueView(key)
                                await msg.edit(embed=embed, view=view)
                                print(f"✅ Restored active queue view for {key} in {channel.name}")
                            else:
                                # Queue is closed, no buttons
                                await msg.edit(embed=embed, view=None)
                                print(f"✅ Restored closed queue view for {key} in {channel.name}")
                        except Exception as e:
                            print(f"Could not restore queue message for {key}: {e}")
                        break
    except Exception as e:
        print(f"Error restoring persistent views: {e}")


STATE_CACHE_FILE = os.path.join(_BASE_DIR, "state_cache.json")
TICKETS_CACHE_FILE = os.path.join(_BASE_DIR, "tickets_cache.json")

def load_state():
    global state, open_tickets
    # Load state from local cache first
    try:
        if os.path.exists(STATE_CACHE_FILE):
            with open(STATE_CACHE_FILE, "r", encoding="utf-8") as f:
                state.update(json.load(f))
            print("✅ Loaded state from local cache")
    except Exception as e:
        print(f"Error loading state from local cache: {e}")

    # Load open_tickets from local cache
    try:
        if os.path.exists(TICKETS_CACHE_FILE):
            with open(TICKETS_CACHE_FILE, "r", encoding="utf-8") as f:
                raw_tickets = json.load(f)
                open_tickets.clear()
                for k, v in raw_tickets.items():
                    open_tickets[int(k)] = v
            print(f"✅ Loaded {len(open_tickets)} open tickets from local cache")
    except Exception as e:
        print(f"Error loading open_tickets from local cache: {e}")

    # Try syncing from SQLite
    try:
        conn = db_manager.get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT key, value FROM state_data")
        rows = cursor.fetchall()
        if rows:
            db_state = {}
            for row in rows:
                try:
                    db_state[row['key']] = json.loads(row['value'])
                except:
                    db_state[row['key']] = row['value']
            state.update(db_state)
            save_state_local()
        conn.close()
    except Exception as e:
        print(f"Error loading state from SQLite: {e}")


def save_state_local():
    try:
        with open(STATE_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=4, ensure_ascii=False)
        
        # Save open_tickets
        serializable_tickets = {str(k): v for k, v in open_tickets.items()}
        with open(TICKETS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(serializable_tickets, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"Error saving state/tickets locally: {e}")


def save_state():
    save_state_local()
    try:
        conn = db_manager.get_db()
        cursor = conn.cursor()
        with db_manager.db_lock:
            for k, v in state.items():
                cursor.execute(
                    "INSERT OR REPLACE INTO state_data (key, value) VALUES (?, ?)",
                    (k, json.dumps(v))
                )
            conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error saving state to SQLite: {e}")


@tasks.loop(seconds=PERSIST_EVERY)
async def autosave_task():
    async with state_lock:
        save_state()


# Helper functions for Minecraft API and Firestore
def get_minecraft_uuid(username: str) -> Optional[dict]:
    """Get Minecraft UUID from username using Mojang API"""
    try:
        response = requests.get(f"https://api.mojang.com/users/profiles/minecraft/{username}", timeout=10)
        if response.status_code == 200:
            data = response.json()
            return {
                "username": data["name"],
                "uuid": data["id"]
            }
        return None
    except Exception:
        return None


def get_minecraft_username_from_uuid(uuid: str) -> Optional[str]:
    """Get current Minecraft username from UUID using Mojang API"""
    try:
        # Format UUID with dashes if not already formatted
        if len(uuid) == 32:
            uuid = f"{uuid[:8]}-{uuid[8:12]}-{uuid[12:16]}-{uuid[16:20]}-{uuid[20:]}"
        
        response = requests.get(f"https://sessionserver.mojang.com/session/minecraft/profile/{uuid.replace('-', '')}", timeout=10)
        if response.status_code == 200:
            data = response.json()
            return data.get("name")
        return None
    except Exception:
        return None


# Helper functions for multi-gamemode queue system
def get_waitlist_key(gamemode: str) -> str:
    """Queue/waitlist state key: lowercase gamemode id."""
    return gamemode.lower().strip()


def parse_waitlist_channel(channel: discord.TextChannel) -> Optional[str]:
    """Parse waitlist channel name. Expected format: waitlist-{gamemode} → returns gamemode id or None."""
    try:
        if getattr(channel, "id", None) == 1542251160541593640:
            return "diamace"
        name = channel.name.lower()
        if not name.startswith("waitlist-"):
            return None
        gamemode = name[9:]
        if gamemode in GAMEMODES:
            return gamemode
        clean_gm = gamemode.replace("-", "").replace("_", "")
        if clean_gm in ("diamace", "diamondmace"):
            return "diamace"
        return None
    except Exception:
        return None


def get_tier_points(tier: str) -> int:
    """Get points for a tier"""
    return TIER_POINTS.get(tier.upper(), 0)


def calculate_overall_score(player_data: dict) -> int:
    """Calculate overall score from player data using peak tiers"""
    total = 0
    for gamemode in GAMEMODES:
        peak_key = f"{gamemode}_peak"
        tier = player_data.get(peak_key) or player_data.get(gamemode, "")
        if tier:
            total += get_tier_points(tier)
    return total


async def get_player_data(uuid: str) -> Optional[dict]:
    """Get player data from updated_players table by UUID"""
    try:
        conn = db_manager.get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM updated_players WHERE uuid = ?", (uuid,))
        row = cursor.fetchone()
        conn.close()
        if row:
            return dict(row)
        return None
    except Exception as e:
        print(f"Error getting player data from SQLite: {e}")
        return None


async def update_player_tier(uuid: str, username: str, gamemode: str, tier: str):
    """Update or create player tier in updated_players table and recalculate overall"""
    try:
        player_data = await get_player_data(uuid)
        if player_data:
            # Retrieve previous current tier BEFORE overwriting
            current_tier = player_data.get(gamemode, "") or ""
            peak_key = f"{gamemode}_peak"
            old_peak = player_data.get(peak_key, "") or ""
            
            # Determine highest peak tier
            best_tier = tier.upper()
            for candidate in [current_tier.upper(), old_peak.upper()]:
                if candidate and get_tier_points(candidate) > get_tier_points(best_tier):
                    best_tier = candidate
            
            player_data[gamemode] = tier.upper()
            player_data[peak_key] = best_tier
                
            player_data['username'] = username
            player_data['overall'] = calculate_overall_score(player_data)
            
            # Update columns
            set_clause = ", ".join([f"{k} = ?" for k in player_data.keys() if k != 'uuid'])
            params = [player_data[k] for k in player_data.keys() if k != 'uuid'] + [uuid]
            
            conn = db_manager.get_db()
            cursor = conn.cursor()
            with db_manager.db_lock:
                cursor.execute(f"UPDATE updated_players SET {set_clause} WHERE uuid = ?", params)
                conn.commit()
            conn.close()
        else:
            player_data = {
                'username': username,
                'uuid': uuid,
                'overall': 0
            }
            for g in GAMEMODES:
                player_data[g] = ""
                player_data[f"{g}_peak"] = ""
            player_data[gamemode] = tier.upper()
            player_data[f"{gamemode}_peak"] = tier.upper()
            player_data['overall'] = calculate_overall_score(player_data)
            
            columns = ", ".join(player_data.keys())
            placeholders = ", ".join(["?" for _ in player_data])
            params = list(player_data.values())
            
            conn = db_manager.get_db()
            cursor = conn.cursor()
            with db_manager.db_lock:
                cursor.execute(f"INSERT INTO updated_players ({columns}) VALUES ({placeholders})", params)
                conn.commit()
            conn.close()
    except Exception as e:
        print(f"Error updating player tier in SQLite: {e}")


def find_user_in_waitlists_or_queues(user_id: int) -> Optional[str]:
    """[DEPRECATED] Find which waitlist or queue a user is in. Returns key or None."""
    for key, users in state.get("waitlists", {}).items():
        if user_id in users:
            return key
    for key, users in state.get("queues", {}).items():
        if user_id in users:
            return key
    return None


def get_user_waitlists_and_queues(user_id: int) -> dict:
    """Find ALL waitlists and queues a user is in. Returns {'waitlists': [keys], 'queues': [keys]}"""
    result = {"waitlists": [], "queues": []}
    for key, users in state.get("waitlists", {}).items():
        if user_id in users:
            result["waitlists"].append(key)
    for key, users in state.get("queues", {}).items():
        if user_id in users:
            result["queues"].append(key)
    return result


async def remove_user_from_all_waitlists_and_queues(guild: discord.Guild, member: discord.Member):
    """Remove a user from all waitlists and queues, and update permissions and queue messages."""
    user_id = member.id
    async with state_lock:
        removed_waitlists = []
        removed_queues = []
        for key, users in state.get("waitlists", {}).items():
            if user_id in users:
                users.remove(user_id)
                removed_waitlists.append(key)
        for key, users in state.get("queues", {}).items():
            if user_id in users:
                users.remove(user_id)
                removed_queues.append(key)
        save_state()

    for key in removed_waitlists:
        channel_name = f"waitlist-{key}"
        waitlist_channel = discord.utils.get(guild.text_channels, name=channel_name)
        if waitlist_channel:
            try:
                await waitlist_channel.set_permissions(member, overwrite=None)
            except Exception as e:
                print(f"Could not remove permissions for {member} in {channel_name}: {e}")

    for key in removed_queues:
        await update_queue_message(guild, key)


def find_tester_active_queue(tester_id: int) -> Optional[str]:
    """Find which queue a tester is actively testing in. Returns key or None"""
    for key, testers in state.get("active_testers", {}).items():
        if tester_id in testers:
            return key
    return None


# Local Verification Cache
VERIFY_CACHE_FILE = os.path.join(_BASE_DIR, "verify_cache.json")
verify_cache = {}

def load_verify_cache():
    global verify_cache
    try:
        if os.path.exists(VERIFY_CACHE_FILE):
            with open(VERIFY_CACHE_FILE, "r", encoding="utf-8") as f:
                verify_cache = json.load(f)
            print(f"✅ Loaded {len(verify_cache)} verified users from local cache")
        else:
            verify_cache = {}
            print("ℹ️ No local verify cache found. Created new.")
    except Exception as e:
        print(f"Error loading verification cache: {e}")
        verify_cache = {}

def save_verify_cache():
    try:
        with open(VERIFY_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(verify_cache, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"Error saving verification cache: {e}")

# Load verify cache at startup
load_verify_cache()


@tasks.loop(hours=1)
async def sync_verify_cache_task():
    """Periodically sync the local cache with the SQLite verify table"""
    try:
        print("🔄 Syncing verification cache with SQLite...")
        conn = db_manager.get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT discord_id, uuid, username FROM verify")
        rows = cursor.fetchall()
        conn.close()
        
        new_cache = {}
        for row in rows:
            new_cache[str(row['discord_id'])] = {
                'uuid': row['uuid'],
                'username': row['username']
            }
        
        # Merge/update cache
        global verify_cache
        verify_cache.update(new_cache)
        save_verify_cache()
        print(f"✅ Verification cache sync complete. Total cached users: {len(verify_cache)}")
    except Exception as e:
        print(f"⚠️ Error syncing verification cache: {e}")


async def is_discord_verified(discord_id: str) -> bool:
    """Check if a Discord user is already verified"""
    discord_id_str = str(discord_id)
    if discord_id_str in verify_cache:
        return True
    try:
        conn = db_manager.get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT uuid, username FROM verify WHERE discord_id = ?", (discord_id_str,))
        row = cursor.fetchone()
        conn.close()
        if row:
            verify_cache[discord_id_str] = {
                'uuid': row['uuid'],
                'username': row['username']
            }
            save_verify_cache()
            return True
        return False
    except Exception as e:
        print(f"Error checking verification in SQLite for {discord_id_str}: {e}")
        return False



async def is_minecraft_verified(uuid: str) -> bool:
    """Check if a Minecraft account is already verified"""
    for d_id, cached in verify_cache.items():
        if cached.get('uuid') == uuid:
            return True
    try:
        conn = db_manager.get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT discord_id, username FROM verify WHERE uuid = ?", (uuid,))
        row = cursor.fetchone()
        conn.close()
        if row:
            verify_cache[str(row['discord_id'])] = {
                'uuid': uuid,
                'username': row['username']
            }
            save_verify_cache()
            return True
        return False
    except Exception as e:
        print(f"Error checking Minecraft verification in SQLite for {uuid}: {e}")
        return False


async def get_verified_username(discord_id: str) -> Optional[str]:
    """Get the verified Minecraft username for a Discord user"""
    discord_id_str = str(discord_id)
    if discord_id_str in verify_cache:
        return verify_cache[discord_id_str].get('username')
    try:
        conn = db_manager.get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT uuid, username FROM verify WHERE discord_id = ?", (discord_id_str,))
        row = cursor.fetchone()
        conn.close()
        if row:
            verify_cache[discord_id_str] = {
                'uuid': row['uuid'],
                'username': row['username']
            }
            save_verify_cache()
            return row['username']
        return None
    except Exception as e:
        print(f"Error getting verified username in SQLite for {discord_id_str}: {e}")
        return None


async def get_verified_uuid(discord_id: str) -> Optional[str]:
    """Get the verified Minecraft UUID for a Discord user"""
    discord_id_str = str(discord_id)
    if discord_id_str in verify_cache:
        return verify_cache[discord_id_str].get('uuid')
    try:
        conn = db_manager.get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT uuid, username FROM verify WHERE discord_id = ?", (discord_id_str,))
        row = cursor.fetchone()
        conn.close()
        if row:
            verify_cache[discord_id_str] = {
                'uuid': row['uuid'],
                'username': row['username']
            }
            save_verify_cache()
            return row['uuid']
        return None
    except Exception as e:
        print(f"Error getting verified UUID in SQLite for {discord_id_str}: {e}")
        return None




async def get_player_tier(username: str) -> Optional[str]:
    """Get the current tier of a player from the 'updated_players' table"""
    try:
        conn = db_manager.get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT overall FROM updated_players WHERE username = ? COLLATE NOCASE LIMIT 1", (username,))
        row = cursor.fetchone()
        conn.close()
        if row:
            return str(row['overall'])
        return None
    except Exception:
        return None


async def get_user_cooldown(discord_id: str, gamemode: str) -> Optional[datetime]:
    """Get the last tested datetime for a user for a specific gamemode from 'updated_cooldowns' table"""
    try:
        conn = db_manager.get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT cooldowns FROM updated_cooldowns WHERE discord_id = ?", (str(discord_id),))
        row = cursor.fetchone()
        conn.close()
        if row:
            data = json.loads(row['cooldowns'])
            last_tested_iso = data.get(gamemode)
            if last_tested_iso:
                dt = datetime.fromisoformat(last_tested_iso)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
        return None
    except Exception:
        return None


async def set_user_cooldown(discord_id: str, display_name: str, gamemode: str, test_date: datetime):
    """Create or update a cooldown document for a user for a specific gamemode"""
    try:
        conn = db_manager.get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT cooldowns FROM updated_cooldowns WHERE discord_id = ?", (str(discord_id),))
        row = cursor.fetchone()
        
        data = {}
        if row:
            data = json.loads(row['cooldowns'])
        
        data['display_name'] = display_name
        data['discord_id'] = str(discord_id)
        data[gamemode] = test_date.isoformat()
        
        with db_manager.db_lock:
            cursor.execute(
                "INSERT OR REPLACE INTO updated_cooldowns (discord_id, cooldowns) VALUES (?, ?)",
                (str(discord_id), json.dumps(data))
            )
            conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error setting cooldown in SQLite: {e}")


async def remove_user_cooldown(discord_id: str, gamemode: str):
    """Remove a specific gamemode cooldown for a user"""
    try:
        conn = db_manager.get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT cooldowns FROM updated_cooldowns WHERE discord_id = ?", (str(discord_id),))
        row = cursor.fetchone()
        
        if row:
            data = json.loads(row['cooldowns'])
            if gamemode in data:
                del data[gamemode]
                with db_manager.db_lock:
                    cursor.execute(
                        "INSERT OR REPLACE INTO updated_cooldowns (discord_id, cooldowns) VALUES (?, ?)",
                        (str(discord_id), json.dumps(data))
                    )
                    conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error removing cooldown in SQLite: {e}")


async def increment_tester_stats(discord_id: str, username: str):
    """Increment all-time and monthly test counts for a tester"""
    try:
        now = datetime.now(timezone.utc)
        month_key = now.strftime("%Y-%m")
        
        conn = db_manager.get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT all_time_tests, monthly_tests FROM tester_stats WHERE discord_id = ?", (str(discord_id),))
        row = cursor.fetchone()
        
        if row:
            all_time = row['all_time_tests'] + 1
            monthly = json.loads(row['monthly_tests'])
            monthly[month_key] = monthly.get(month_key, 0) + 1
        else:
            all_time = 1
            monthly = {month_key: 1}
            
        with db_manager.db_lock:
            cursor.execute(
                "INSERT OR REPLACE INTO tester_stats (discord_id, username, all_time_tests, monthly_tests) VALUES (?, ?, ?, ?)",
                (str(discord_id), username, all_time, json.dumps(monthly))
            )
            conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error incrementing tester stats in SQLite: {e}")


async def get_tester_stats(discord_id: str) -> dict:
    """Get stats for a specific tester"""
    try:
        conn = db_manager.get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tester_stats WHERE discord_id = ?", (str(discord_id),))
        row = cursor.fetchone()
        conn.close()
        if row:
            res = dict(row)
            res['monthly_tests'] = json.loads(res['monthly_tests'])
            return res
        return {}
    except Exception:
        return {}


async def get_tester_leaderboard():
    """Get top 10 all-time and top 10 monthly testers"""
    try:
        now = datetime.now(timezone.utc)
        month_key = now.strftime("%Y-%m")
        
        conn = db_manager.get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tester_stats")
        rows = cursor.fetchall()
        conn.close()
        
        testers = []
        for row in rows:
            t = dict(row)
            t['monthly_tests'] = json.loads(t['monthly_tests'])
            testers.append(t)
            
        # Sort by all time
        all_time_sorted = sorted(testers, key=lambda x: x.get('all_time_tests', 0), reverse=True)[:10]
        
        # Sort by monthly
        monthly_sorted = sorted(testers, key=lambda x: x.get('monthly_tests', {}).get(month_key, 0), reverse=True)[:10]
        monthly_sorted = [t for t in monthly_sorted if t.get('monthly_tests', {}).get(month_key, 0) > 0]
        
        total_monthly = sum(t.get('monthly_tests', {}).get(month_key, 0) for t in testers)
        
        return all_time_sorted, monthly_sorted, total_monthly, month_key
    except Exception as e:
        print(f"Error getting leaderboard: {e}")
        return [], [], 0, ""


async def update_leaderboard_message(guild: discord.Guild):
    """Update the live leaderboard message if it exists"""
    msg_info = state.get("leaderboard_message", {})
    if not msg_info:
        return
        
    channel_id = msg_info.get("channel_id")
    message_id = msg_info.get("message_id")
    if not channel_id or not message_id:
        return
        
    channel = guild.get_channel(channel_id)
    if not channel:
        return
        
    try:
        msg = await channel.fetch_message(message_id)
        
        all_time_sorted, monthly_sorted, total_monthly, month_key = await get_tester_leaderboard()
        
        # Format month name
        try:
            month_obj = datetime.strptime(month_key, "%Y-%m")
            month_name = month_obj.strftime("%B")
        except:
            month_name = month_key
            
        desc = "🏆 **All Time Testing Leaderboard**\n\n"
        for i, t in enumerate(all_time_sorted, 1):
            desc += f"**{i}.** <@{t['discord_id']}> — **{t.get('all_time_tests', 0)}** tests\n"
            
        desc += f"\n🏅 **{month_name} Testing Leaderboard**\n\n"
        for i, t in enumerate(monthly_sorted, 1):
            count = t.get('monthly_tests', {}).get(month_key, 0)
            desc += f"**{i}.** <@{t['discord_id']}> — **{count}** tests\n"
            
        desc += f"\n**Total Tests in {month_name} : {total_monthly}**"
        
        embed = discord.Embed(
            description=desc,
            color=0x2b2d31  # Dark embed color matching screenshot
        )
        embed.set_footer(
            text="NL┃Testing Community", 
            icon_url="https://nltiers.vercel.app/logo.png"
        )
        
        await msg.edit(content="", embed=embed)
    except Exception as e:
        print(f"Error updating leaderboard message: {e}")


async def update_suslist_message(guild: discord.Guild):
    """Update the live sus list message if it exists"""
    msg_info = state.get("suslist_message", {})
    if not msg_info:
        return
        
    try:
        channel = guild.get_channel(msg_info["channel_id"])
        if not channel:
            try:
                channel = await guild.fetch_channel(msg_info["channel_id"])
            except:
                return
                
        msg = await channel.fetch_message(msg_info["message_id"])
        if not msg:
            return
            
        sus_list = state.get("sus_list", {})
        
        embed = discord.Embed(
            title="🚨 Suspected Cheaters List 🚨",
            color=discord.Color.red()
        )
        
        if not sus_list:
            embed.description = "There are currently no suspected cheaters."
        else:
            desc = "The following players are marked as suspected of cheating. They will be screenshared before testing.\n\n"
            for discord_id, info in sus_list.items():
                member_mention = f"<@{discord_id}>"
                reason = info.get("reason", "No reason provided")
                added_by = info.get("added_by", "Unknown")
                desc += f"• {member_mention} - **Reason**: {reason} (Added by: <@{added_by}>)\n"
            embed.description = desc
            
        embed.timestamp = datetime.now(timezone.utc)
        embed.set_footer(
            text="NL┃Testing Community", 
            icon_url="https://nltiers.vercel.app/logo.png"
        )
        
        await msg.edit(content="", embed=embed)
    except Exception as e:
        print(f"Error updating suslist message: {e}")


async def update_quota_message(guild: discord.Guild):
    """Update the live quota list message if it exists"""
    msg_info = state.get("quota_message", {})
    if not msg_info:
        return
        
    try:
        channel = guild.get_channel(msg_info["channel_id"])
        if not channel:
            try:
                channel = await guild.fetch_channel(msg_info["channel_id"])
            except:
                return
                
        msg = await channel.fetch_message(msg_info["message_id"])
        if not msg:
            return
            
        tester_role = discord.utils.get(guild.roles, name=TESTER_ROLE_NAME)
        if not tester_role:
            return

        now = datetime.now(timezone.utc)
        month_key = now.strftime("%Y-%m")
        try:
            month_name = now.strftime("%B")
        except:
            month_name = month_key
            
        server_testers = tester_role.members
        
        # Retrieve tester stats from SQLite
        conn = db_manager.get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT discord_id, monthly_tests FROM tester_stats")
        rows = cursor.fetchall()
        conn.close()
        tester_stats = {}
        for row in rows:
            m_tests = {}
            if row['monthly_tests']:
                try:
                    m_tests = json.loads(row['monthly_tests'])
                except:
                    pass
            tester_stats[row['discord_id']] = m_tests.get(month_key, 0)
            
        achieved = []
        progress = []
        inactive = []
        
        for member in server_testers:
            discord_id = str(member.id)
            tests = tester_stats.get(discord_id, 0)
            
            if tests >= 15:
                achieved.append((member, tests))
            elif tests > 0:
                progress.append((member, tests))
            else:
                inactive.append((member, tests))
                
        achieved.sort(key=lambda x: x[1], reverse=True)
        progress.sort(key=lambda x: x[1], reverse=True)
        
        embed = discord.Embed(
            title=f"📊 Tester Quota Leaderboard ({month_name})",
            description="Lijst van alle testers en hun behaalde tests deze maand. Quota: 15 tests.",
            color=discord.Color.blue()
        )
        
        achieved_desc = ""
        if achieved:
            for m, t in achieved:
                achieved_desc += f"• <@{m.id}> — **{t}/15** tests\n"
        else:
            achieved_desc = "Nog niemand heeft de quota behaald."

        if len(achieved_desc) > 1024:
            achieved_desc = achieved_desc[:1020] + "..."

        embed.add_field(
            name=f"✅ Quota Behaald ({len(achieved)})",
            value=achieved_desc,
            inline=False
        )
        
        progress_desc = ""
        if progress:
            for m, t in progress:
                progress_desc += f"• <@{m.id}> — **{t}/15** tests\n"
        else:
            progress_desc = "Niemand is momenteel bezig."

        if len(progress_desc) > 1024:
            progress_desc = progress_desc[:1020] + "..."

        embed.add_field(
            name=f"⚠️ Bezig met Quota ({len(progress)})",
            value=progress_desc,
            inline=False
        )
        
        inactive_desc = ""
        if inactive:
            for m, t in inactive:
                inactive_desc += f"• <@{m.id}>\n"
        else:
            inactive_desc = "Alle testers hebben minstens 1 test gedaan!"
            
        if len(inactive_desc) > 1024:
            inactive_desc = inactive_desc[:1020] + "..."
            
        embed.add_field(name=f"❌ Inactief (0 tests) ({len(inactive)})", value=inactive_desc, inline=False)
        
        embed.timestamp = datetime.now(timezone.utc)
        embed.set_footer(
            text="NL┃Testing Community", 
            icon_url="https://nltiers.vercel.app/logo.png"
        )
        
        await msg.edit(content="", embed=embed)
    except Exception as e:
        print(f"Error updating quota message: {e}")


class GuardedView(discord.ui.View):
    """Enforces SERVER_ID for button/select interactions (same rules as slash commands)."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await global_guild_interaction_check(interaction)


class SetupButtonView(GuardedView):
    """View with a button to open the waitlist selection"""
    
    def __init__(self):
        super().__init__(timeout=None)
    
    @discord.ui.button(label="Join Waitlist", style=discord.ButtonStyle.primary, emoji="🎮", custom_id="open_waitlist_selection")
    async def open_selection(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Open gamemode selection in an ephemeral message"""
        view = GamemodeSelectView()
        await interaction.response.send_message(
            "**Select a gamemode** from the dropdown to join that waitlist.",
            view=view,
            ephemeral=True
        )


class _WaitlistGamemodeSelect(discord.ui.Select):
    """Gamemode dropdown; options from gamemodes.json. One pick joins the waitlist."""

    def __init__(self):
        super().__init__(
            placeholder="Select a gamemode...",
            options=[
                discord.SelectOption(label=GAMEMODE_LABELS[gid], value=gid)
                for gid in GAMEMODES
            ],
            custom_id="gamemode_select",
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            user = interaction.user
            gamemode = self.values[0]
            key = get_waitlist_key(gamemode)

            restricted_role = interaction.guild.get_role(RESTRICTED_ROLE_ID)
            if restricted_role and restricted_role in user.roles:
                await interaction.followup.send(
                    "❌ Je kunt geen waitlist of queue joinen omdat je momenteel gerestricteerd bent.",
                    ephemeral=True)
                return

            if not await is_discord_verified(str(user.id)):
                await interaction.followup.send(
                    "❌ You must verify your Minecraft account first using `/verify <minecraft_username>`",
                    ephemeral=True)
                return

            tester_role = discord.utils.get(interaction.guild.roles, name=TESTER_ROLE_NAME)
            has_tester_role = tester_role in user.roles if tester_role else False

            last_tested = await get_user_cooldown(str(user.id), gamemode)
            if last_tested and not has_tester_role:
                now = datetime.now(timezone.utc)
                time_since_test = now - last_tested
                cooldown_period = timedelta(weeks=1)

                if time_since_test < cooldown_period:
                    time_remaining = cooldown_period - time_since_test
                    days = time_remaining.days
                    hours, remainder = divmod(time_remaining.seconds, 3600)
                    minutes, seconds = divmod(remainder, 60)

                    time_parts = []
                    if days > 0:
                        time_parts.append(f"{days} day{'s' if days != 1 else ''}")
                    if hours > 0:
                        time_parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
                    if minutes > 0 and days == 0:
                        time_parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")

                    time_str = ", ".join(time_parts) if time_parts else "less than a minute"

                    await interaction.followup.send(
                        f"❌ You are on cooldown for **{gamemode_label(gamemode)}**. You can join this waitlist again in **{time_str}**.",
                        ephemeral=True)
                    return

            user_lists = get_user_waitlists_and_queues(user.id)
            if key in user_lists["waitlists"] or key in user_lists["queues"]:
                await interaction.followup.send(
                    f"❌ You are already in the **{gamemode_label(key)}** waitlist/queue.",
                    ephemeral=True)
                return

            channel_name = f"waitlist-{gamemode}"
            waitlist_channel = discord.utils.get(interaction.guild.text_channels, name=channel_name)

            if not waitlist_channel:
                try:
                    waitlist_category = await get_or_create_category(
                        interaction.guild, WAITLISTS_CATEGORY_NAME
                    )
                    tester_role = discord.utils.get(interaction.guild.roles, name=TESTER_ROLE_NAME)
                    overwrites = {
                        interaction.guild.default_role: discord.PermissionOverwrite(
                            view_channel=False,
                            send_messages=False
                        )
                    }
                    if tester_role:
                        overwrites[tester_role] = discord.PermissionOverwrite(
                            view_channel=False,
                            send_messages=True
                        )

                    waitlist_channel = await interaction.guild.create_text_channel(
                        channel_name,
                        category=waitlist_category,
                        topic=f"Waitlist for {gamemode_label(gamemode)} testing",
                        overwrites=overwrites
                    )
                except Exception as e:
                    await interaction.followup.send(
                        f"❌ Could not create waitlist channel: {e}",
                        ephemeral=True)
                    return
            else:
                try:
                    tester_role = discord.utils.get(interaction.guild.roles, name=TESTER_ROLE_NAME)
                    if tester_role:
                        await waitlist_channel.set_permissions(
                            tester_role,
                            view_channel=False,
                            send_messages=True
                        )
                    await waitlist_channel.set_permissions(
                        interaction.guild.default_role,
                        view_channel=False,
                        send_messages=False
                    )
                except Exception as e:
                    print(f"Could not update channel permissions: {e}")

            try:
                tester_role = discord.utils.get(interaction.guild.roles, name=TESTER_ROLE_NAME)
                has_tester_role = tester_role in user.roles if tester_role else False

                await waitlist_channel.set_permissions(
                    user,
                    view_channel=True,
                    send_messages=has_tester_role
                )
            except Exception as e:
                print(f"Could not set permissions for {user}: {e}")

            async with state_lock:
                if key not in state["waitlists"]:
                    state["waitlists"][key] = []

                state["waitlists"][key].append(user.id)
                save_state()

            await interaction.followup.send(
                f"✅ You have joined the **{gamemode_label(gamemode)}** waitlist!\n"
                f"You can now see {waitlist_channel.mention} where the queue will open.",
                ephemeral=True)

        except Exception as e:
            try:
                await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)
            except Exception:
                pass


class GamemodeSelectView(GuardedView):
    """Single dropdown to pick a gamemode and join its waitlist."""

    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(_WaitlistGamemodeSelect())


# Join Queue Button View for specific queues
class JoinQueueView(GuardedView):
    def __init__(self, key: str):
        super().__init__(timeout=None)
        self.key = key  # gamemode id e.g. "sword"
        # Set unique custom IDs for view persistence
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                if child.custom_id == "join_queue_btn":
                    child.custom_id = f"join_queue_btn:{key}"
                elif child.custom_id == "leave_queue_btn":
                    child.custom_id = f"leave_queue_btn:{key}"

        
    @discord.ui.button(label="Join Queue", style=discord.ButtonStyle.primary, custom_id="join_queue_btn")
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        
        try:
            user_id = interaction.user.id
            
            restricted_role = interaction.guild.get_role(RESTRICTED_ROLE_ID)
            if restricted_role and restricted_role in interaction.user.roles:
                await interaction.followup.send(
                    "❌ Je kunt geen waitlist of queue joinen omdat je momenteel gerestricteerd bent.",
                    ephemeral=True)
                return

            # Check if user is verified
            if not await is_discord_verified(str(user_id)):
                await interaction.followup.send(
                    "❌ You must verify your Minecraft account first using `/verify <minecraft_username>`",
                    ephemeral=True)
                return

            gamemode = self.key
            
            # Check if user is on cooldown for this specific gamemode
            last_tested = await get_user_cooldown(str(user_id), gamemode)
            if last_tested:
                now = datetime.now(timezone.utc)
                time_since_test = now - last_tested
                cooldown_period = timedelta(weeks=1)
                
                if time_since_test < cooldown_period:
                    time_remaining = cooldown_period - time_since_test
                    days = time_remaining.days
                    hours, remainder = divmod(time_remaining.seconds, 3600)
                    minutes, seconds = divmod(remainder, 60)
                    
                    time_parts = []
                    if days > 0:
                        time_parts.append(f"{days} day{'s' if days != 1 else ''}")
                    if hours > 0:
                        time_parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
                    if minutes > 0 and days == 0:
                        time_parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
                    
                    time_str = ", ".join(time_parts) if time_parts else "less than a minute"
                    
                    await interaction.followup.send(
                        f"❌ You are on cooldown for **{gamemode_label(gamemode)}**. You can join this queue again in **{time_str}**.",
                                                    ephemeral=True)
                    return

            # Check if user is already in this queue specifically
            if self.key in state.get("queues", {}) and user_id in state["queues"][self.key]:
                await interaction.followup.send(
                    "❌ You are already in this queue!",
                    ephemeral=True)
                return

            # Check if queue is full
            old_queue = []
            async with state_lock:
                if self.key not in state["queues"]:
                    state["queues"][self.key] = []
                
                queue = state["queues"][self.key]
                old_queue = queue.copy()
                
                if len(queue) >= QUEUE_LIMIT:
                    await interaction.followup.send(
                        f"❌ The queue is full ({QUEUE_LIMIT}/{QUEUE_LIMIT}). Please wait for a spot to open up.",
                        ephemeral=True)
                    return
                
                # Add to queue
                queue.append(user_id)
                position = len(queue)
                save_state()

            await interaction.followup.send(
                f"✅ You have joined the **{self.key}** queue at position **{position}**!",
                ephemeral=True)
            
            # If queue was empty and user is now the only one, notify testers
            if len(old_queue) == 0 and position == 1:
                await notify_testers_first_join(interaction.guild, self.key, interaction.user)
            
            # Update queue embed and notify if first
            await update_queue_message(interaction.guild, self.key, old_queue)
            
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    @discord.ui.button(label="Leave Queue", style=discord.ButtonStyle.danger, custom_id="leave_queue_btn")
    async def leave_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        
        try:
            user_id = interaction.user.id
            
            # Check if user is in this specific queue
            old_queue = []
            async with state_lock:
                if self.key not in state.get("queues", {}) or user_id not in state["queues"][self.key]:
                    await interaction.followup.send(
                        "❌ You are not in this queue. Use `/leave` if you're in a waitlist.",
                                                    ephemeral=True)
                    return
                
                old_queue = state["queues"][self.key].copy()
                state["queues"][self.key].remove(user_id)
                save_state()

            await interaction.followup.send(
                f"✅ You have left the **{self.key}** queue.",
                ephemeral=True)
            
            # Update queue embed and notify if someone moved to first
            await update_queue_message(interaction.guild, self.key, old_queue)
            
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)


async def get_closed_queue_embed(guild: Optional[discord.Guild], key: str) -> discord.Embed:
    """Generate closed queue embed matching PvPTiers style in Dutch"""
    embed = discord.Embed(
        title="Geen Testers Online",
        color=0xED4245
    )
    
    if guild:
        icon_url = guild.icon.url if guild.icon else (bot.user.display_avatar.url if bot.user else None)
        gm_label = gamemode_label(key)
        embed.set_author(name=f"NL Tiers — {gm_label}", icon_url=icon_url)
    elif bot.user:
        embed.set_author(name="NL Tiers", icon_url=bot.user.display_avatar.url)
        
    last_session_ts = state.get("last_testing_sessions", {}).get(key)
    
    desc_lines = [
        "Er zijn op dit moment geen testers beschikbaar.",
        "Je krijgt een ping zodra er een tester beschikbaar is.",
        "Kijk later opnieuw!",
        ""
    ]
    
    if last_session_ts:
        desc_lines.append(f"Laatste testsessie: <t:{int(last_session_ts)}:f>")
    else:
        desc_lines.append("Laatste testsessie: *Geen recente sessie*")
        
    embed.description = "\n".join(desc_lines)
    return embed


async def get_discord_username(guild: discord.Guild, uid: int) -> str:
    """Get the plain text Discord username for a user ID"""
    try:
        member = guild.get_member(uid)
        if member:
            return f"@{member.name}"
        member = await guild.fetch_member(uid)
        return f"@{member.name}"
    except Exception:
        try:
            user = await bot.fetch_user(uid)
            return f"@{user.name}"
        except Exception:
            return f"@{uid}"


async def get_queue_embed_for_key(guild: discord.Guild, key: str) -> discord.Embed:
    """Generate queue embed for a specific gamemode queue"""
    # Check if there are active testers
    active_testers = state.get("active_testers", {}).get(key, [])
    
    # If no active testers, return closed embed
    if not active_testers:
        return await get_closed_queue_embed(guild, key)
    
    embed = discord.Embed(title="Tester(s) Available!", color=QUEUE_EMBED_COLOR)
    
    # Display active testers
    tester_lines = []
    for tester_id in active_testers:
        member = guild.get_member(tester_id)
        if member:
            tester_lines.append(f"{member.mention}")
    
    if tester_lines:
        embed.add_field(name="Active Testers:", value="\n".join(tester_lines), inline=False)

    # Get queue for this key
    queue = state.get("queues", {}).get(key, [])
    
    # Display queue with count
    queue_title = f"Queue ({len(queue)}/{QUEUE_LIMIT}):"

    if not queue:
        embed.add_field(name=queue_title, value="The queue is empty.", inline=False)
    else:
        lines = []
        for pos, uid in enumerate(queue, start=1):
            try:
                member = guild.get_member(int(uid))
                if not member:
                    member = await guild.fetch_member(int(uid))
                display_name = member.display_name
            except Exception:
                display_name = f"User ({uid})"
            lines.append(f"**{pos}.** {display_name}")
        embed.add_field(name=queue_title, value="\n".join(lines), inline=False)

    embed.description = f"**Queue for {key.upper()}**\n\n👍 Click 'Leave Queue' to be removed."
    embed.set_footer(text="Testers: Use /next to open a ticket with the first user in queue.")
    return embed


async def notify_testers_first_join(guild: discord.Guild, key: str, user: discord.Member):
    """Notify all active testers when the first person joins an empty queue"""
    try:
        # Get active testers for this queue
        active_testers = state.get("active_testers", {}).get(key, [])
        
        if not active_testers:
            return
        
        gamemode = key
        if gamemode not in GAMEMODES:
            return

        # DM each active tester
        for tester_id in active_testers:
            tester = guild.get_member(tester_id)
            if tester:
                try:
                    await tester.send(
                        f"🔔 **New player in queue!**\n\n"
                        f"**{user.display_name}** just joined the **{gamemode_label(gamemode)}** queue.\n"
                        f"They are the first and only person waiting!\n\n"
                        f"Use `/next` to open a ticket with them."
                    )
                except (discord.Forbidden, discord.HTTPException):
                    # Tester has DMs disabled or blocked the bot
                    pass
    except Exception as e:
        print(f"Error notifying testers of first join: {e}")


async def notify_first_in_queue_for_key(guild: discord.Guild, key: str, old_queue: list):
    """Notify user if they've moved to position #1 in a specific queue"""
    try:
        new_queue = state.get("queues", {}).get(key, [])
        
        # Check if there's someone at position 1
        if not new_queue:
            return
        
        new_first = new_queue[0]
        
        # Check if this person wasn't at position 1 before (or queue was empty)
        if not old_queue or (old_queue and old_queue[0] != new_first):
            member = guild.get_member(new_first)
            if member:
                try:
                    await member.send(
                        f"You are now **first in the {key.upper()} queue**! 🎉\n"
                        f"A tester will open a ticket with you soon."
                    )
                except (discord.Forbidden, discord.HTTPException):
                    # User has DMs disabled or blocked the bot
                    pass
    except Exception as e:
        print(f"Error notifying first in queue: {e}")


async def update_queue_message(guild: discord.Guild, key: str, old_queue: list = None):
    """Update the queue message for a specific queue"""
    try:
        # Notify first person if queue changed
        if old_queue is not None:
            await notify_first_in_queue_for_key(guild, key, old_queue)
        
        queue_messages = state.get("queue_messages", {})
        if key not in queue_messages:
            return
        
        msg_info = queue_messages[key]
        channel = guild.get_channel(msg_info["channel_id"])
        if not channel:
            return
        
        msg = await channel.fetch_message(msg_info["message_id"])
        embed = await get_queue_embed_for_key(guild, key)
        
        # Check if queue is active (has active testers)
        active_testers = state.get("active_testers", {}).get(key, [])
        if active_testers and len(active_testers) > 0:
            # Queue is active, include buttons
            view = JoinQueueView(key)
            await msg.edit(embed=embed, view=view)
        else:
            # Queue is closed, remove all components
            await msg.edit(embed=embed, view=None)
        
    except Exception as e:
        print(f"Error updating queue message for {key}: {e}")


# OLD QUEUE FUNCTIONS (Replaced by multi-queue system)
# These are kept for reference but no longer used


async def save_ticket_to_db(channel_id: int, tester_id: int, testee_id: int, gamemode: str):
    """Save ticket information to SQLite"""
    try:
        conn = db_manager.get_db()
        cursor = conn.cursor()
        with db_manager.db_lock:
            cursor.execute(
                "INSERT OR REPLACE INTO tickets (channel_id, tester_id, testee_id, gamemode, created_at) VALUES (?, ?, ?, ?, ?)",
                (str(channel_id), str(tester_id), str(testee_id), gamemode, datetime.now(timezone.utc).isoformat())
            )
            conn.commit()
        conn.close()
        print(f"💾 Saved ticket {channel_id} to SQLite: {gamemode}")
    except Exception as e:
        print(f"Error saving ticket to SQLite: {e}")


async def load_ticket_from_db(channel_id: int) -> Optional[dict]:
    """Load ticket information from SQLite"""
    try:
        conn = db_manager.get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tickets WHERE channel_id = ?", (str(channel_id),))
        row = cursor.fetchone()
        conn.close()
        if row:
            res = dict(row)
            res['channel_id'] = int(res['channel_id'])
            res['tester_id'] = int(res['tester_id'])
            res['testee_id'] = int(res['testee_id'])
            return res
        return None
    except Exception as e:
        print(f"Error loading ticket from SQLite: {e}")
        return None


async def delete_ticket_from_db(channel_id: int):
    """Delete ticket information from SQLite"""
    try:
        conn = db_manager.get_db()
        cursor = conn.cursor()
        with db_manager.db_lock:
            cursor.execute("DELETE FROM tickets WHERE channel_id = ?", (str(channel_id),))
            conn.commit()
        conn.close()
        print(f"🗑️ Deleted ticket {channel_id} from SQLite")
    except Exception as e:
        print(f"Error deleting ticket from SQLite: {e}")


async def grant_tester_access_to_open_tickets(
    guild: discord.Guild, gamemode_key: str, tester_member: discord.Member
) -> None:
    """Give a tester view/send access to all open tickets for this gamemode (e.g. after /start)."""
    for cid, info in list(open_tickets.items()):
        g = info.get("gamemode")
        if not g or get_waitlist_key(g) != gamemode_key:
            continue
        ch = guild.get_channel(cid)
        if not ch or not isinstance(ch, discord.TextChannel):
            continue
        try:
            await ch.set_permissions(
                tester_member,
                read_messages=True,
                send_messages=True,
                reason="Active tester joined via /start",
            )
        except Exception as e:
            print(f"Could not grant ticket access in {cid}: {e}")


async def revoke_tester_access_from_open_tickets(
    guild: discord.Guild, gamemode_key: str, tester_member: discord.Member
) -> None:
    """Remove a tester's ticket overwrites when they leave a co-testing session (/stop)."""
    for cid, info in list(open_tickets.items()):
        g = info.get("gamemode")
        if not g or get_waitlist_key(g) != gamemode_key:
            continue
        ch = guild.get_channel(cid)
        if not ch or not isinstance(ch, discord.TextChannel):
            continue
        try:
            await ch.set_permissions(
                tester_member,
                overwrite=None,
                reason="Tester left /stop",
            )
        except Exception as e:
            print(f"Could not revoke ticket access in {cid}: {e}")


async def create_ticket_channel(
        guild: discord.Guild, tester: discord.Member,
        target: discord.Member, gamemode: str, username: str) -> Optional[discord.TextChannel]:
    try:
        category = await get_or_create_category(guild, gamemode_label(gamemode))
        overwrites = {
            guild.default_role:
            discord.PermissionOverwrite(read_messages=False),
            tester:
            discord.PermissionOverwrite(read_messages=True,
                                        send_messages=True),
            target:
            discord.PermissionOverwrite(read_messages=True,
                                        send_messages=True),
            guild.me:
            discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }
        staff_role = guild.get_role(STAFF_ROLE_ID)
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(
                read_messages=True,
                send_messages=True,
            )
        wl_key = get_waitlist_key(gamemode)
        for aid in state.get("active_testers", {}).get(wl_key, []):
            am = guild.get_member(aid)
            if am:
                overwrites[am] = discord.PermissionOverwrite(
                    read_messages=True,
                    send_messages=True,
                )
        name = f"ticket-{gamemode}-{target.display_name}"[:90]
        channel = await guild.create_text_channel(name,
                                                  overwrites=overwrites,
                                                  category=category)
        
        # Get previous rank
        previous_rank = "Unranked"
        uuid = await get_verified_uuid(str(target.id))
        if uuid:
            player_data = await get_player_data(uuid)
            if player_data:
                previous_rank = player_data.get(gamemode, "")
                if not previous_rank:
                    previous_rank = "Unranked"

        # Get preferred server
        preferred_server = state.get("preferences", {}).get(str(target.id), "Unknown")

        embed = discord.Embed(title=f"{gamemode_label(gamemode)} Testing Ticket",
                              color=TICKET_EMBED_COLOR)
        
        avatar_url = target.display_avatar.url if target.display_avatar else None
        embed.set_author(name=f"{target.display_name}'s Information", icon_url=avatar_url)
        
        embed.add_field(name="User", value=target.mention if target else "Unknown", inline=False)
        embed.add_field(name="Server", value=preferred_server if preferred_server else "Unknown", inline=False)
        embed.add_field(name="Username", value=username if username else "Unknown", inline=False)
        embed.add_field(name="Previous Rank", value=previous_rank if previous_rank else "Unranked", inline=False)
        
        # Ping both tester and testee outside the embed
        await channel.send(f"{tester.mention} {target.mention}", embed=embed)
        
        # Check for suspect alert
        target_id_str = str(target.id)
        sus_list = state.get("sus_list", {})
        if target_id_str in sus_list:
            sus_channel = guild.get_channel(1511794133466087475)
            if not sus_channel:
                try:
                    sus_channel = await guild.fetch_channel(1511794133466087475)
                except:
                    pass
            if sus_channel:
                await sus_channel.send(f"<@&1498060823845797920> 🚨 **SUSPECT ALERT** 🚨\nSpeler {target.mention} (Ticket: {channel.mention}) gaat testen in {gamemode}. Ga naar het ticket en screenshare deze persoon!")
        
        # Save ticket to Firestore
        await save_ticket_to_db(channel.id, tester.id, target.id, gamemode)
        
        return channel
    except Exception as e:
        print(f"Error creating ticket channel: {e}")
        return None


async def assign_tier_role(guild: discord.Guild, member: discord.Member,
                           tier_role_name: str):
    try:
        roles_to_remove = [
            r for r in member.roles if r.name.upper() in TIER_ROLES
        ]
        for r in roles_to_remove:
            await member.remove_roles(r, reason="Reassigning tier")
        role = discord.utils.get(guild.roles, name=tier_role_name.upper())
        if role:
            await member.add_roles(role, reason="Assigning tier")
    except Exception:
        pass


def format_gamemode_name(gamemode: str) -> str:
    """Format gamemode name for role display (uses displayName from gamemodes.json)."""
    name = gamemode_label(gamemode)
    if name.lower() == "dia pot":
        return "Pot"
    if gamemode.lower() in ("diamace", "diamondmace", "diamond mace"):
        return "DiaMace"
    return name



async def assign_gamemode_tier_role(guild: discord.Guild, member: discord.Member,
                                     tier: str, gamemode: str):
    """Assign a gamemode-specific tier role (e.g., 'LT5 Sword', 'HT4 NethOP')"""
    try:
        tier = tier.upper()
        formatted_gamemode = format_gamemode_name(gamemode)
        new_role_name = f"{tier} {formatted_gamemode}"
        
        # Remove old tier roles for this specific gamemode
        # Pattern: any tier + this gamemode (e.g., "LT4 Sword", "HT3 Sword")
        roles_to_remove = []
        for role in member.roles:
            for t in TIER_ROLES:
                if role.name.upper() == f"{t} {formatted_gamemode}".upper():
                    roles_to_remove.append(role)
                    break
        
        for role in roles_to_remove:
            await member.remove_roles(role, reason=f"Reassigning tier for {formatted_gamemode}")

        
        # Find or create the new role (case-insensitive to match beautifully colored existing roles)
        role = discord.utils.find(lambda r: r.name.lower() == new_role_name.lower(), guild.roles)
        if not role:
            # Create the role if it doesn't exist
            role = await guild.create_role(
                name=new_role_name,
                reason=f"Auto-created for tier assignment"
            )

        
        # Assign the new role
        await member.add_roles(role, reason=f"Assigned {tier} tier for {formatted_gamemode}")
        
    except Exception as e:
        print(f"Error assigning gamemode tier role: {e}")


async def handle_command_error(interaction: discord.Interaction, e: Exception):
    try:
        await safe_followup(interaction, f"Error: {e}", ephemeral=True)
    except Exception:
        pass


@app_commands.default_permissions(manage_messages=True)
@bot.tree.command(name="next",
                  description="Open ticket with next user in queue")
async def next_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        tester_role = discord.utils.get(interaction.guild.roles,
                                        name=TESTER_ROLE_NAME)
        if not tester_role or tester_role not in interaction.user.roles:
            await safe_followup(interaction, "❌ You need the Tester role to use this command.",
                                ephemeral=True)
            return

        # First check if we're in a ticket channel (so tester doesn't have to navigate to waitlist)
        ticket_info = open_tickets.get(interaction.channel.id)
        if not ticket_info:
            ticket_info = await load_ticket_from_db(interaction.channel.id)

        if ticket_info:
            # We're inside a ticket channel — get gamemode from ticket
            gamemode = ticket_info.get("gamemode")
            if not gamemode:
                await safe_followup(interaction,
                    "❌ Could not determine gamemode from this ticket.",
                    ephemeral=True)
                return
        else:
            # Fallback: must be used in a waitlist channel
            gamemode = parse_waitlist_channel(interaction.channel)
            if not gamemode:
                await safe_followup(interaction,
                    "❌ This command must be used in a waitlist channel (`waitlist-{gamemode}`) or inside a ticket channel.",
                    ephemeral=True)
                return

        key = get_waitlist_key(gamemode)

        # Get user from queue
        old_queue = []
        async with state_lock:
            if key not in state["queues"] or not state["queues"][key]:
                await safe_followup(interaction, f"❌ Queue is empty for **{gamemode_label(gamemode)}**.",
                                    ephemeral=True)
                return

            old_queue = state["queues"][key].copy()
            uid_to_test = state["queues"][key][0]
            state["queues"][key].remove(uid_to_test)
            save_state()

        target = interaction.guild.get_member(uid_to_test)
        if not target:
            await safe_followup(interaction, "❌ User not found in the server.",
                                ephemeral=True)
            return

        # Get verified username
        username = await get_verified_username(str(uid_to_test))
        if not username:
            username = target.display_name

        # Create ticket
        ticket_channel = await create_ticket_channel(interaction.guild,
                                                     interaction.user, target, gamemode, username)
        if not ticket_channel:
            # Restore queue if creation failed (e.g. Discord API 503)
            async with state_lock:
                state["queues"][key] = old_queue
                save_state()
            await update_queue_message(interaction.guild, key, old_queue)
            await safe_followup(interaction, "❌ Could not create ticket channel (Discord API error). Queue restored.",
                                ephemeral=True)
            return

        # Send ping message
        await ticket_channel.send(f"{target.mention}")

        # Store ticket info
        open_tickets[ticket_channel.id] = {
            "user_id": target.id,
            "gamemode": gamemode,
        }

        # Set cooldown for this gamemode
        await set_user_cooldown(str(uid_to_test), target.display_name, gamemode, datetime.now(timezone.utc))
        
        # Update queue message
        await update_queue_message(interaction.guild, key, old_queue)

        await safe_followup(interaction,
            f"✅ Ticket opened: {ticket_channel.mention}", ephemeral=True)

    except Exception as e:
        print(f"❌ Error in /next: {e}")
        await safe_followup(interaction, f"❌ Error: {e}", ephemeral=True)


@app_commands.default_permissions(manage_messages=True)
@bot.tree.command(name="skip",
                  description="Skip the first person and open ticket with the second person in queue")
async def skip_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        tester_role = discord.utils.get(interaction.guild.roles,
                                        name=TESTER_ROLE_NAME)
        if not tester_role or tester_role not in interaction.user.roles:
            await safe_followup(interaction, "❌ You need the Tester role to use this command.",
                                ephemeral=True)
            return

        gamemode = parse_waitlist_channel(interaction.channel)
        if not gamemode:
            await safe_followup(interaction,
                "❌ This command must be used in a waitlist channel (format: `waitlist-{gamemode}`).",
                ephemeral=True)
            return

        key = get_waitlist_key(gamemode)

        # Log command usage (we'll log the specific user after we get them from queue)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        # Get second user from queue
        old_queue = []
        async with state_lock:
            if key not in state["queues"] or len(state["queues"][key]) < 2:
                await safe_followup(interaction, f"❌ Need at least 2 people in queue for **{gamemode_label(gamemode)}**.",
                                    ephemeral=True)
                return

            old_queue = state["queues"][key].copy()
            uid_to_test = state["queues"][key][1]
            state["queues"][key].remove(uid_to_test)
            save_state()

        target = interaction.guild.get_member(uid_to_test)
        if not target:
            await safe_followup(interaction, "❌ User not found in the server.",
                                ephemeral=True)
            return

        # Log the skip with target info
        print(f"[{timestamp}] /skip - Used by: {interaction.user} ({interaction.user.id}) | Skipped user: {target} ({target.id}) | Queue: {key}")

        # Get verified username
        username = await get_verified_username(str(uid_to_test))
        if not username:
            username = target.display_name

        # Create ticket
        ticket_channel = await create_ticket_channel(interaction.guild,
                                                     interaction.user, target, gamemode, username)
        if not ticket_channel:
            # Restore queue if creation failed (e.g. Discord API 503)
            async with state_lock:
                state["queues"][key] = old_queue
                save_state()
            await update_queue_message(interaction.guild, key, old_queue)
            await safe_followup(interaction, "❌ Could not create ticket channel (Discord API error). Queue restored.",
                                ephemeral=True)
            return

        # Send ping message
        await ticket_channel.send(f"{target.mention}")

        # Store ticket info
        open_tickets[ticket_channel.id] = {
            "user_id": target.id,
            "gamemode": gamemode,
        }

        # Set cooldown for this gamemode
        await set_user_cooldown(str(uid_to_test), target.display_name, gamemode, datetime.now(timezone.utc))
        
        # Update queue message
        await update_queue_message(interaction.guild, key, old_queue)

        await safe_followup(interaction,
            f"✅ Skipped first user. Ticket opened: {ticket_channel.mention}", ephemeral=True)

    except Exception as e:
        print(f"❌ Error in /skip: {e}")
        await safe_followup(interaction, f"❌ Error: {e}", ephemeral=True)


@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(member="The member to remove from the queue")
@bot.tree.command(name="removefromqueue", description="Remove a user from the testing queue")
async def removefromqueue(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.defer(ephemeral=True)
    try:
        tester_role = discord.utils.get(interaction.guild.roles, name=TESTER_ROLE_NAME)
        if not tester_role or tester_role not in interaction.user.roles:
            await interaction.followup.send("❌ You need the Tester role to use this command.", ephemeral=True)
            return

        # Log command usage
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"[{timestamp}] /removefromqueue - Used by: {interaction.user} ({interaction.user.id}) | Target: {member} ({member.id})")

        user_id = member.id
        user_lists = get_user_waitlists_and_queues(user_id)
        
        if not user_lists["queues"]:
            await interaction.followup.send(f"❌ {member.mention} is not in any active queue.", ephemeral=True)
            return
        
        view = StaffRemoveSelectView(member, user_lists, "queue")
        await interaction.followup.send(f"Select which queue to remove {member.mention} from:", view=view, ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)


class LeaveSelect(discord.ui.Select):
    def __init__(self, user_lists: dict, user_id: int):
        options = []
        for q in set(user_lists["waitlists"] + user_lists["queues"]):
            options.append(discord.SelectOption(label=f"Waitlist/Queue: {gamemode_label(q)}", value=q))
        super().__init__(placeholder="Select which one to leave...", min_values=1, max_values=1, options=options)
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        key = self.values[0]
        
        async with state_lock:
            removed_from = []
            if key in state.get("waitlists", {}) and self.user_id in state["waitlists"][key]:
                state["waitlists"][key].remove(self.user_id)
                removed_from.append("waitlist")
            if key in state.get("queues", {}) and self.user_id in state["queues"][key]:
                state["queues"][key].remove(self.user_id)
                removed_from.append("queue")
            save_state()
        
        if removed_from:
            ch_leave = f"waitlist-{key}"
            waitlist_channel = discord.utils.get(interaction.guild.text_channels, name=ch_leave)
            if waitlist_channel:
                try:
                    await waitlist_channel.set_permissions(interaction.user, overwrite=None)
                except Exception as e:
                    print(f"Could not remove permissions for {interaction.user}: {e}")
            
            if "queue" in removed_from:
                await update_queue_message(interaction.guild, key)
            
            removed_str = " and ".join(removed_from)
            await interaction.followup.send(
                f"✅ You have been removed from the **{gamemode_label(key)}** {removed_str}.",
                ephemeral=True)
        else:
            await interaction.followup.send("❌ You are not in that waitlist/queue anymore.", ephemeral=True)


class LeaveSelectView(GuardedView):
    def __init__(self, user_lists: dict, user_id: int):
        super().__init__(timeout=None)
        self.add_item(LeaveSelect(user_lists, user_id))


class StaffRemoveSelect(discord.ui.Select):
    def __init__(self, target: discord.Member, target_lists: dict, action_type: str):
        options = []
        for q in target_lists[action_type + "s"]:
            options.append(discord.SelectOption(label=f"{action_type.capitalize()}: {gamemode_label(q)}", value=q))
        super().__init__(placeholder=f"Select {action_type} to remove from...", min_values=1, max_values=1, options=options)
        self.target = target
        self.action_type = action_type

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        key = self.values[0]
        
        async with state_lock:
            removed = False
            if self.action_type == "waitlist" and key in state.get("waitlists", {}) and self.target.id in state["waitlists"][key]:
                state["waitlists"][key].remove(self.target.id)
                removed = True
            elif self.action_type == "queue" and key in state.get("queues", {}) and self.target.id in state["queues"][key]:
                state["queues"][key].remove(self.target.id)
                removed = True
            
            if removed:
                save_state()
            else:
                await interaction.followup.send(f"❌ {self.target.mention} is not in that {self.action_type} anymore.", ephemeral=True)
                return
        
        ch_name = f"waitlist-{key}"
        waitlist_channel = discord.utils.get(interaction.guild.text_channels, name=ch_name)
        if waitlist_channel:
            try:
                await waitlist_channel.set_permissions(self.target, overwrite=None)
            except Exception as e:
                pass
        
        if self.action_type == "queue":
            await update_queue_message(interaction.guild, key)
            
        try:
            await self.target.send(f"You have been removed from the **{gamemode_label(key)}** {self.action_type} in **{interaction.guild.name}** by a tester.")
            dm_status = " (DM sent)"
        except (discord.Forbidden, discord.HTTPException):
            dm_status = " (Could not send DM)"
        
        await interaction.followup.send(f"✅ {self.target.mention} has been removed from the **{gamemode_label(key)}** {self.action_type}.{dm_status}", ephemeral=True)


class StaffRemoveSelectView(GuardedView):
    def __init__(self, target: discord.Member, target_lists: dict, action_type: str):
        super().__init__(timeout=None)
        self.add_item(StaffRemoveSelect(target, target_lists, action_type))


@app_commands.default_permissions(administrator=True)
@bot.tree.command(name="sync", description="Manually sync slash commands (Admin only)")
async def sync_commands(interaction: discord.Interaction):
    """Manually sync slash commands - useful for debugging"""
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "❌ This command is only available to administrators.",
            ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    try:
        synced = await push_app_commands(bot)
        await interaction.followup.send(
            f"✅ Successfully synced {len(synced)} slash command(s)!\n" +
            "\n".join([f"• /{cmd.name}" for cmd in synced]),
            ephemeral=True)
    except Exception as e:
        await interaction.followup.send(
            f"❌ Failed to sync commands: {e}",
            ephemeral=True)


@app_commands.default_permissions(manage_messages=True)
@bot.tree.command(name="setup", description="Setup waitlist selection message")
async def setup(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        tester_role = discord.utils.get(interaction.guild.roles,
                                        name=TESTER_ROLE_NAME)
        if not tester_role or tester_role not in interaction.user.roles:
            await interaction.followup.send("❌ You need the Tester role to use this command.",
                                            ephemeral=True)
            return
    
        gm_list = " | ".join(GAMEMODE_LABELS[gid] for gid in GAMEMODES)
        embed = discord.Embed(
            title="🎮 NL Testing",
            description=(
                "Welcome to NL tier list!\n\n"
                "**How it works:**\n"
                "1️⃣ Click the **Join Waitlist** button below\n"
                "2️⃣ Select your **gamemode** from the dropdown to join that waitlist\n"
                "3️⃣ When a tester opens the queue, you can join using the **Join Queue** button\n\n"
                f"**Available Gamemodes:**\n{gm_list}\n\n"
                "**Important Notes:**\n"
                "• You can only be in ONE waitlist/queue at a time\n"
                "• You must be verified (`/verify`) before joining\n"
                "• There's a 1-week cooldown after being tested\n"
                "• Use `/leave` to leave your current waitlist or queue"
            ),
            color=QUEUE_EMBED_COLOR
        )
        embed.set_footer(text="Click the button below to get started!")

        view = SetupButtonView()
        
        # Check if setup message already exists
        if "setup_message" in state and state["setup_message"]:
            try:
                msg_info = state["setup_message"]
                channel = interaction.guild.get_channel(msg_info["channel_id"])
                if channel:
                    msg = await channel.fetch_message(msg_info["message_id"])
                    await msg.edit(embed=embed, view=view)
                    await interaction.followup.send(
                        "✅ Setup message updated!",
                        ephemeral=True
                    )
                    return
            except Exception as e:
                print(f"Could not update existing setup message: {e}")
        
        # Send new setup message
        setup_msg = await interaction.channel.send(embed=embed, view=view)
        async with state_lock:
            state["setup_message"] = {
                "channel_id": interaction.channel.id,
                "message_id": setup_msg.id
            }
            save_state()
        
        await interaction.followup.send(
            "✅ Setup message created!",
            ephemeral=True
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)


@app_commands.describe(minecraft_username="Your Minecraft username")
@bot.tree.command(name="verify", description="Verify your Minecraft account")
async def verify(interaction: discord.Interaction, minecraft_username: str):
    await interaction.response.defer(ephemeral=True)
    try:
        user_id = str(interaction.user.id)

        # Check if Discord user is already verified
        if await is_discord_verified(user_id):
            await interaction.followup.send(
                "❌ Your Discord account is already verified. Contact an admin/developer for assistance.",
                ephemeral=True)
            return

        # Get Minecraft UUID from Mojang API
        mc_data = get_minecraft_uuid(minecraft_username)
        if not mc_data:
            await interaction.followup.send(
                "❌ Invalid Minecraft username. Please check the spelling and try again.",
                ephemeral=True)
            return

        uuid = mc_data["uuid"]
        username = mc_data["username"]

        # Check if Minecraft account is already verified by another Discord user
        if await is_minecraft_verified(uuid):
            await interaction.followup.send(
                "❌ This Minecraft account is already verified with another Discord account. Contact an admin/developer for assistance.",
                ephemeral=True)
            return

        # Add to Firestore 'verify' collection
        try:
            conn = db_manager.get_db()
            cursor = conn.cursor()
            with db_manager.db_lock:
                cursor.execute(
                    "INSERT OR REPLACE INTO verify (discord_id, uuid, username) VALUES (?, ?, ?)",
                    (str(user_id), uuid, username)
                )
                
                # Add to 'updated_players' collection with empty tiers
                player_data = {
                    'username': username,
                    'uuid': uuid,
                    'overall': 0
                }
                for gamemode in GAMEMODES:
                    player_data[gamemode] = ""
                
                columns = ", ".join(player_data.keys())
                placeholders = ", ".join(["?" for _ in player_data])
                params = list(player_data.values())
                
                cursor.execute(f"INSERT OR REPLACE INTO updated_players ({columns}) VALUES ({placeholders})", params)
                conn.commit()
            conn.close()

            # Update local cache
            verify_cache[user_id] = {
                'uuid': uuid,
                'username': username
            }
            save_verify_cache()

            await interaction.followup.send(
                f"✅ Successfully verified! Your Minecraft account **{username}** has been linked to your Discord account.",
                ephemeral=True)
        except Exception as e:
            await interaction.followup.send(
                f"❌ Error saving to database: {e}",
                ephemeral=True)

    except Exception as e:
        await handle_command_error(interaction, e)


@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(
    member="The Discord member to unverify",
    ign="The Minecraft username (IGN) to unverify (if member left Discord)"
)
@bot.tree.command(name="unverify", description="Unverify a Discord member's Minecraft account via Discord or IGN (Tester only)")
async def unverify(
    interaction: discord.Interaction,
    member: Optional[discord.Member] = None,
    ign: Optional[str] = None
):
    await interaction.response.defer(ephemeral=True)
    try:
        tester_role = discord.utils.get(interaction.guild.roles, name=TESTER_ROLE_NAME)
        if not tester_role or tester_role not in interaction.user.roles:
            await interaction.followup.send("❌ You need the Tester role to use this command.", ephemeral=True)
            return

        if not member and not ign:
            await interaction.followup.send("❌ Specify either a @member or an `ign` to unverify.", ephemeral=True)
            return

        target_user_id = str(member.id) if member else None
        target_uuid = None
        target_username = None

        conn = db_manager.get_db()
        cursor = conn.cursor()

        if target_user_id:
            cursor.execute("SELECT uuid, username FROM verify WHERE discord_id = ?", (target_user_id,))
            row = cursor.fetchone()
            if row:
                target_uuid = row['uuid']
                target_username = row['username']
        elif ign:
            target_username = ign.strip()
            cursor.execute("SELECT discord_id, uuid, username FROM verify WHERE username LIKE ?", (f"%{target_username}%",))
            row = cursor.fetchone()
            if row:
                target_user_id = row['discord_id']
                target_uuid = row['uuid']
                target_username = row['username']
            else:
                cursor.execute("SELECT uuid, username FROM updated_players WHERE username LIKE ?", (f"%{target_username}%",))
                row2 = cursor.fetchone()
                if row2:
                    target_uuid = row2['uuid']
                    target_username = row2['username']

        if not target_uuid and not target_user_id:
            conn.close()
            await interaction.followup.send(f"❌ Could not find verification data for **{ign or member}**.", ephemeral=True)
            return

        with db_manager.db_lock:
            if target_user_id:
                cursor.execute("DELETE FROM verify WHERE discord_id = ?", (target_user_id,))
                cursor.execute("DELETE FROM updated_cooldowns WHERE discord_id = ?", (target_user_id,))
            if target_uuid:
                cursor.execute("DELETE FROM updated_players WHERE uuid = ?", (target_uuid,))
                cursor.execute("DELETE FROM verify WHERE uuid = ?", (target_uuid,))
            conn.commit()
        conn.close()

        if target_user_id and target_user_id in verify_cache:
            del verify_cache[target_user_id]
            save_verify_cache()

        await update_leaderboard_message(interaction.guild)

        target_mention_str = f" ({member.mention})" if member else ""
        await interaction.followup.send(
            f"✅ Successfully unverified **{target_username or ign}**{target_mention_str}.\n"
            f"Their verification data, tier progress, and cooldowns have been completely removed.",
            ephemeral=True
        )

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"[{timestamp}] /unverify - Used by: {interaction.user} | Target IGN: {target_username} | Discord ID: {target_user_id}")

    except Exception as e:
        await handle_command_error(interaction, e)


@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    minecraft_username="The new Minecraft username to link",
    member="The Discord member to reverify",
    ign="The old Minecraft username (IGN) to reverify (if member left Discord)"
)
@bot.tree.command(name="reverify", description="Manually reverify an account with a new Minecraft username (Admin only)")
async def reverify(
    interaction: discord.Interaction,
    minecraft_username: str,
    member: Optional[discord.Member] = None,
    ign: Optional[str] = None
):
    await interaction.response.defer(ephemeral=True)
    try:
        if not interaction.user.guild_permissions.administrator:
            await interaction.followup.send("❌ You need administrator permissions to use this command.", ephemeral=True)
            return

        if not member and not ign:
            await interaction.followup.send("❌ Specify either a @member or an old `ign` to reverify.", ephemeral=True)
            return

        user_id = str(member.id) if member else None
        old_uuid = None
        old_username = None

        conn = db_manager.get_db()
        cursor = conn.cursor()

        if user_id:
            cursor.execute("SELECT uuid, username FROM verify WHERE discord_id = ? LIMIT 1", (user_id,))
            row = cursor.fetchone()
            if row:
                old_uuid = row['uuid']
                old_username = row['username']
        elif ign:
            target_old = ign.strip()
            cursor.execute("SELECT discord_id, uuid, username FROM verify WHERE username LIKE ? LIMIT 1", (f"%{target_old}%",))
            row = cursor.fetchone()
            if row:
                user_id = row['discord_id']
                old_uuid = row['uuid']
                old_username = row['username']
            else:
                cursor.execute("SELECT uuid, username FROM updated_players WHERE username LIKE ? LIMIT 1", (f"%{target_old}%",))
                row2 = cursor.fetchone()
                if row2:
                    old_uuid = row2['uuid']
                    old_username = row2['username']
        conn.close()

        mc_data = get_minecraft_uuid(minecraft_username)
        if not mc_data:
            await interaction.followup.send("❌ Invalid Minecraft username. Please check the spelling.", ephemeral=True)
            return

        new_uuid = mc_data["uuid"]
        new_username = mc_data["username"]

        conn = db_manager.get_db()
        cursor = conn.cursor()

        with db_manager.db_lock:
            if old_uuid:
                cursor.execute("DELETE FROM verify WHERE uuid = ?", (old_uuid,))
                cursor.execute("DELETE FROM updated_players WHERE uuid = ?", (old_uuid,))

            if user_id:
                cursor.execute(
                    "INSERT OR REPLACE INTO verify (discord_id, uuid, username) VALUES (?, ?, ?)",
                    (str(user_id), new_uuid, new_username)
                )

            player_data = {'username': new_username, 'uuid': new_uuid, 'overall': 0}
            for gamemode in GAMEMODES:
                player_data[gamemode] = ""

            columns = ", ".join(player_data.keys())
            placeholders = ", ".join(["?" for _ in player_data])
            params = list(player_data.values())
            cursor.execute(f"INSERT OR REPLACE INTO updated_players ({columns}) VALUES ({placeholders})", params)

            conn.commit()
        conn.close()

        if user_id:
            verify_cache[user_id] = {'uuid': new_uuid, 'username': new_username}
            save_verify_cache()

        await update_leaderboard_message(interaction.guild)

        target_str = member.mention if member else (old_username or ign)
        await interaction.followup.send(
            f"✅ Successfully reverified **{target_str}** with new Minecraft account **{new_username}** (`{new_uuid}`)!",
            ephemeral=True
        )

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"[{timestamp}] /reverify - Used by: {interaction.user} | Target: {target_str} | New IGN: {new_username}")

    except Exception as e:
        await handle_command_error(interaction, e)


@bot.tree.command(name="leave", description="Leave your current waitlist")
async def leave(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        user_id = interaction.user.id
        
        # Check if user is a tester and actively testing
        tester_role = discord.utils.get(interaction.guild.roles, name=TESTER_ROLE_NAME)
        is_tester = tester_role in interaction.user.roles if tester_role else False
        
        # Find if tester is actively testing in any queue
        active_testing_key = None
        if is_tester:
            for key, testers in state.get("active_testers", {}).items():
                if user_id in testers:
                    active_testing_key = key
                    break
        
        # If tester is actively testing, behave like /stop
        if active_testing_key:
            async with state_lock:
                # Remove tester from active list
                state["active_testers"][active_testing_key].remove(user_id)
                
                # Check if last tester
                is_last_tester = len(state["active_testers"][active_testing_key]) == 0
                
                if is_last_tester:
                    # Clear the queue
                    if active_testing_key in state["queues"]:
                        state["queues"][active_testing_key] = []
                    
                    # Delete ping message
                    if active_testing_key in state.get("ping_messages", {}):
                        ping_info = state["ping_messages"][active_testing_key]
                        del state["ping_messages"][active_testing_key]
                        
                        # Try to delete the ping message
                        try:
                            channel = interaction.guild.get_channel(ping_info["channel_id"])
                            if channel:
                                ping_msg = await channel.fetch_message(ping_info["message_id"])
                                await ping_msg.delete()
                        except Exception as e:
                            print(f"Could not delete ping message: {e}")
                    
                    save_state()
            
                else:
                    save_state()
            
            # Update queue message outside the lock (whether last tester or not)
            await update_queue_message(interaction.guild, active_testing_key)
            
            gm = active_testing_key
            if is_last_tester:
                await interaction.followup.send(
                    f"✅ Queue closed for **{gamemode_label(gm)}**. You were the last tester.",
                    ephemeral=True)
            else:
                await interaction.followup.send(
                    f"✅ You've stopped testing **{gamemode_label(gm)}**. Queue is still active.",
                    ephemeral=True)
            return
        
        # Normal leave behavior for non-testers or testers not actively testing
        user_lists = get_user_waitlists_and_queues(user_id)
        if not user_lists["waitlists"] and not user_lists["queues"]:
            await interaction.followup.send(
                "❌ You are not in any waitlist or queue.",
                ephemeral=True)
            return
        
        view = LeaveSelectView(user_lists, user_id)
        await interaction.followup.send("Select which waitlist or queue to leave:", view=view, ephemeral=True)
            
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)


@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(member="The member to remove from waitlist")
@bot.tree.command(name="removefromwaitlist", description="Remove a user from their waitlist")
async def removefromwaitlist(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.defer(ephemeral=True)
    try:
        tester_role = discord.utils.get(interaction.guild.roles,
                                        name=TESTER_ROLE_NAME)
        if not tester_role or tester_role not in interaction.user.roles:
            await interaction.followup.send("❌ You need the Tester role to use this command.",
                                            ephemeral=True)
            return
        
        # Log command usage
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"[{timestamp}] /removefromwaitlist - Used by: {interaction.user} ({interaction.user.id}) | Target: {member} ({member.id})")
        
        user_id = member.id
        
        # Check if member is an active tester in any queue
        tester_key = find_tester_active_queue(user_id)
        
        if tester_key:
            # Member is an active tester, handle queue closure/removal
            async with state_lock:
                # Check if tester is active in this queue
                if tester_key not in state["active_testers"] or user_id not in state["active_testers"][tester_key]:
                    await interaction.followup.send(
                        f"❌ {member.mention} is not actively testing.",
                        ephemeral=True)
                    return
                
                # Remove tester
                state["active_testers"][tester_key].remove(user_id)
                
                # Check if last tester
                is_last_tester = len(state["active_testers"][tester_key]) == 0
                
                if is_last_tester:
                    # Clear the queue
                    if tester_key in state["queues"]:
                        state["queues"][tester_key] = []
                    
                    # Delete ping message
                    if tester_key in state.get("ping_messages", {}):
                        ping_info = state["ping_messages"][tester_key]
                        del state["ping_messages"][tester_key]
                        
                        # Try to delete the ping message
                        try:
                            channel = interaction.guild.get_channel(ping_info["channel_id"])
                            if channel:
                                ping_msg = await channel.fetch_message(ping_info["message_id"])
                                await ping_msg.delete()
                        except Exception as e:
                            print(f"Could not delete ping message: {e}")
                    
                    save_state()
                    
                    # Update queue message to show closed state
                    await update_queue_message(interaction.guild, tester_key)
                    
                    await interaction.followup.send(
                        f"✅ {member.mention} was removed from active testers. Queue closed for **{gamemode_label(tester_key)}**.",
                        ephemeral=True)
                else:
                    save_state()
                    
                    # Update queue message to reflect removed tester
                    await update_queue_message(interaction.guild, tester_key)
                    
                    await interaction.followup.send(
                        f"✅ {member.mention} removed from active testers for **{gamemode_label(tester_key)}**. Queue is still active.",
                        ephemeral=True)
            return
        
        # Not an active tester, check if in waitlist or queue as a regular user
        user_lists = get_user_waitlists_and_queues(user_id)
        if not user_lists["waitlists"]:
            await interaction.followup.send(
                f"❌ {member.mention} is not in any waitlist.",
                ephemeral=True)
            return
        
        view = StaffRemoveSelectView(member, user_lists, "waitlist")
        await interaction.followup.send(f"Select which waitlist to remove {member.mention} from:", view=view, ephemeral=True)
            
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)


@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(tier="Tier to assign, e.g., LT5, HT1")
@bot.tree.command(name="close", description="Close ticket and assign tier")
async def close(interaction: discord.Interaction, tier: str):
    await interaction.response.defer(ephemeral=True)
    try:
        tester_role = discord.utils.get(interaction.guild.roles,
                                        name=TESTER_ROLE_NAME)
        if not tester_role or tester_role not in interaction.user.roles:
            await interaction.followup.send("❌ You need the Tester role to use this command.",
                                            ephemeral=True)
            return

        channel = interaction.channel
        ticket_info = open_tickets.get(channel.id)
        
        # If not in memory, try loading from Firestore
        if not ticket_info:
            ticket_info = await load_ticket_from_db(channel.id)
            if ticket_info:
                # Cache it in memory
                open_tickets[channel.id] = {
                    "user_id": ticket_info["testee_id"],
                    "gamemode": ticket_info["gamemode"],
                }
        
        if not ticket_info:
            await interaction.followup.send(
                "This command only works in a ticket opened by /next.",
                ephemeral=True)
            return

        # Extract info from ticket
        if "user_id" in ticket_info:
            target_id = ticket_info["user_id"]
        else:
            target_id = ticket_info["testee_id"]
        gamemode = ticket_info["gamemode"]

        if interaction.user.id == target_id:
            await interaction.followup.send(
                "❌ You cannot close your own ticket or assign yourself a tier.",
                ephemeral=True)
            return

        tier = tier.upper()
        if tier not in TIER_ROLES:
            await interaction.followup.send(
                f"Invalid tier. Use one of: {', '.join(TIER_ROLES)}",
                ephemeral=True)
            return

        member = interaction.guild.get_member(target_id)
        if not member:
            await interaction.followup.send("User not found.", ephemeral=True)
            return

        # Get verified Minecraft username and UUID
        minecraft_username = await get_verified_username(str(target_id))
        if not minecraft_username:
            await interaction.followup.send(
                "❌ User is not verified. They need to use `/verify` first.",
                ephemeral=True)
            return
        # Get UUID for the player
        uuid = await get_verified_uuid(str(target_id))
        if not uuid:
            await interaction.followup.send("❌ Could not find UUID for user.", ephemeral=True)
            return

        previous_tier = "Unranked"
        db_updated = True

        try:
            # Get previous tier for this gamemode
            player_data = await get_player_data(uuid)
            if player_data and gamemode in player_data:
                tier_value = player_data[gamemode]
                if tier_value and tier_value.strip():
                    previous_tier = tier_value
        except Exception as e:
            print(f"Error getting player data during close: {e}")

        try:
            # Update player tier in updated_players collection
            await update_player_tier(uuid, minecraft_username, gamemode, tier)
            # Set cooldown for this gamemode upon grading
            await set_user_cooldown(str(target_id), member.display_name, gamemode, datetime.now(timezone.utc))
        except Exception as e:
            print(f"Error updating player tier/cooldown in database during close: {e}")
            db_updated = False


        # Assign tier role (general tier role)
        await assign_tier_role(interaction.guild, member, tier)
        
        # Assign gamemode-specific tier role (e.g., "LT5 Sword")
        await assign_gamemode_tier_role(interaction.guild, member, tier, gamemode)

        # Message in ticket
        ticket_desc = f"{member.mention} has been assigned **{tier}** for **{gamemode_label(gamemode)}**."
        if not db_updated:
            ticket_desc += "\n⚠️ **Note**: Database update failed (quota exceeded). Tier updated in Discord only."
        await channel.send(embed=discord.Embed(
            title="Ticket Closed",
            description=ticket_desc,
            color=TICKET_EMBED_COLOR))

        # Send embed to results channel
        results_channel = interaction.guild.get_channel(RESULTS_CHANNEL_ID)
        if not results_channel:
            try:
                results_channel = await interaction.guild.fetch_channel(RESULTS_CHANNEL_ID)
            except Exception as e:
                print(f"Could not fetch results channel with ID {RESULTS_CHANNEL_ID}: {e}")
                results_channel = None

        if results_channel:
            # Format gamemode name properly
            formatted_gamemode = format_gamemode_name(gamemode)
            
            embed = discord.Embed(
                title=f"{minecraft_username}'s Test Results 🏆",
                color=TICKET_EMBED_COLOR)
            if not db_updated:
                embed.description = "⚠️ **Note**: Database update failed (quota exceeded). Tier updated in Discord only."
            embed.add_field(name="Tester:",
                            value=interaction.user.mention,
                            inline=False)
            embed.add_field(name="Username:",
                            value=minecraft_username,
                            inline=False)
            embed.add_field(name="Previous Rank:",
                            value=previous_tier,
                            inline=False)
            embed.add_field(name="Rank Earned:",
                            value=f"{tier} {formatted_gamemode}",
                            inline=False)
            
            embed.set_thumbnail(url=f"https://mc-heads.net/body/{minecraft_username}")
            
            # Ping the tested player before the embed
            await results_channel.send(content=member.mention, embed=embed)
        else:
            print(f"Skipping result embed because results channel ({RESULTS_CHANNEL_ID}) could not be retrieved.")


        # Increment tester stats and update leaderboard
        await increment_tester_stats(str(interaction.user.id), interaction.user.display_name)
        if interaction.guild:
            await update_leaderboard_message(interaction.guild)
            await update_quota_message(interaction.guild)

        # Remove user from waitlist (user was already removed from queue by /next)
        key = get_waitlist_key(gamemode)
        async with state_lock:
            if key in state["waitlists"] and target_id in state["waitlists"][key]:
                state["waitlists"][key].remove(target_id)
            
            save_state()
        
        # Remove user's permissions from the waitlist channel
        channel_name = f"waitlist-{gamemode}"
        waitlist_channel = discord.utils.get(interaction.guild.text_channels, name=channel_name)
        if waitlist_channel:
            try:
                await waitlist_channel.set_permissions(member, overwrite=None)
            except Exception as e:
                print(f"Could not remove permissions for {member}: {e}")
        
        # Update queue message to show queue is cleared
        await update_queue_message(interaction.guild, key)

        # Delete ticket from memory and Firestore
        if channel.id in open_tickets:
            del open_tickets[channel.id]
        await delete_ticket_from_db(channel.id)
        await channel.delete(reason=f"Ticket closed by {interaction.user}")

        await interaction.followup.send(
            f"Tier {tier} assigned to {minecraft_username} for {gamemode_label(gamemode)} and ticket deleted.",
            ephemeral=True)

    except Exception as e:
        await handle_command_error(interaction, e)


@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    member="The Discord member whose tier you want to set",
    gamemode="The gamemode to set the tier for",
    tier="The tier to assign, e.g. LT5, HT3, HT1"
)
@bot.tree.command(name="settier", description="Manually set a player's tier on the website (Admin only)")
async def settier(interaction: discord.Interaction, member: discord.Member, gamemode: str, tier: str):
    await interaction.response.defer(ephemeral=True)
    try:
        # Admin only
        if not interaction.user.guild_permissions.administrator:
            await interaction.followup.send(
                "❌ You need administrator permissions to use this command.",
                ephemeral=True)
            return

        # Validate gamemode
        gamemode = gamemode.lower().strip()
        if gamemode not in GAMEMODES:
            gm_list = ", ".join(GAMEMODES)
            await interaction.followup.send(
                f"❌ Invalid gamemode `{gamemode}`. Available: {gm_list}",
                ephemeral=True)
            return

        # Validate tier
        tier = tier.upper().strip()
        if tier not in TIER_ROLES:
            await interaction.followup.send(
                f"❌ Invalid tier `{tier}`. Use one of: {', '.join(TIER_ROLES)}",
                ephemeral=True)
            return

        # Check if member is verified
        discord_id = str(member.id)
        minecraft_username = await get_verified_username(discord_id)
        if not minecraft_username:
            await interaction.followup.send(
                f"❌ {member.mention} is not verified. They need to use `/verify` first.",
                ephemeral=True)
            return

        # Get their UUID
        uuid = await get_verified_uuid(discord_id)
        if not uuid:
            await interaction.followup.send(
                f"❌ Could not find Minecraft UUID for {member.mention}.",
                ephemeral=True)
            return

        try:
            # Get previous tier for this gamemode (for the log)
            player_data = await get_player_data(uuid)
            previous_tier = "Unranked"
            if player_data and gamemode in player_data:
                tier_value = player_data.get(gamemode, "")
                if tier_value and str(tier_value).strip():
                    previous_tier = tier_value

            # Update SQLite (this is what the website reads from)
            await update_player_tier(uuid, minecraft_username, gamemode, tier)
        except Exception as e:
            await interaction.followup.send(
                f"❌ Error updating database: {e}",
                ephemeral=True)
            return

        # Assign Discord roles
        await assign_tier_role(interaction.guild, member, tier)
        await assign_gamemode_tier_role(interaction.guild, member, tier, gamemode)

        # Log command usage
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"[{timestamp}] /settier - Used by: {interaction.user} ({interaction.user.id}) | Target: {member} ({member.id}) | Gamemode: {gamemode} | Tier: {tier} | Previous: {previous_tier}")

        await interaction.followup.send(
            f"✅ **Tier updated on the website!**\n"
            f"**Player:** {minecraft_username} ({member.mention})\n"
            f"**Gamemode:** {gamemode_label(gamemode)}\n"
            f"**Previous tier:** {previous_tier}\n"
            f"**New tier:** {tier}",
            ephemeral=True)

    except Exception as e:
        await handle_command_error(interaction, e)


@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    member="The Discord member whose tier you want to remove",
    gamemode="The gamemode to remove the tier from"
)
@bot.tree.command(name="removetier", description="Remove a player's tier for a gamemode from the website (Admin only)")
async def removetier(interaction: discord.Interaction, member: discord.Member, gamemode: str):
    await interaction.response.defer(ephemeral=True)
    try:
        # Admin only
        if not interaction.user.guild_permissions.administrator:
            await interaction.followup.send(
                "❌ You need administrator permissions to use this command.",
                ephemeral=True)
            return

        # Validate gamemode
        gamemode = gamemode.lower().strip()
        if gamemode not in GAMEMODES:
            gm_list = ", ".join(GAMEMODES)
            await interaction.followup.send(
                f"❌ Invalid gamemode `{gamemode}`. Available: {gm_list}",
                ephemeral=True)
            return

        # Check if member is verified
        discord_id = str(member.id)
        minecraft_username = await get_verified_username(discord_id)
        if not minecraft_username:
            await interaction.followup.send(
                f"❌ {member.mention} is not verified.",
                ephemeral=True)
            return

        # Get their UUID
        uuid = await get_verified_uuid(discord_id)
        if not uuid:
            await interaction.followup.send(
                f"❌ Could not find Minecraft UUID for {member.mention}.",
                ephemeral=True)
            return

        try:
            # Get current tier so we can log it and remove the Discord role
            player_data = await get_player_data(uuid)
            current_tier = None
            if player_data and gamemode in player_data:
                tier_value = player_data.get(gamemode, "")
                if tier_value and str(tier_value).strip():
                    current_tier = tier_value

            if not current_tier:
                await interaction.followup.send(
                    f"❌ {member.mention} has no tier for **{gamemode_label(gamemode)}** — nothing to remove.",
                    ephemeral=True)
                return

            # Clear the tier in SQLite by setting it to empty string
            if player_data:
                player_data[gamemode] = ""
                player_data['overall'] = calculate_overall_score(player_data)
                
                set_clause = ", ".join([f"{k} = ?" for k in player_data.keys() if k != 'uuid'])
                params = [player_data[k] for k in player_data.keys() if k != 'uuid'] + [uuid]
                
                conn = db_manager.get_db()
                cursor = conn.cursor()
                with db_manager.db_lock:
                    cursor.execute(f"UPDATE updated_players SET {set_clause} WHERE uuid = ?", params)
                    conn.commit()
                conn.close()

        except Exception as e:
            await interaction.followup.send(
                f"❌ Error updating database: {e}",
                ephemeral=True)
            return

        # Remove the gamemode-specific Discord role (e.g. "LT4 Sword")
        formatted_gamemode = format_gamemode_name(gamemode)
        roles_to_remove = []
        for role in member.roles:
            for t in TIER_ROLES:
                if role.name.upper() == f"{t} {formatted_gamemode}".upper():
                    roles_to_remove.append(role)
                    break

        for role in roles_to_remove:
            try:
                await member.remove_roles(role, reason=f"Tier removed for {formatted_gamemode} by admin")
            except Exception as e:
                print(f"Could not remove role {role.name}: {e}")

        # Log command usage
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"[{timestamp}] /removetier - Used by: {interaction.user} ({interaction.user.id}) | Target: {member} ({member.id}) | Gamemode: {gamemode} | Removed tier: {current_tier}")

        await interaction.followup.send(
            f"✅ **Tier removed from the website!**\n"
            f"**Player:** {minecraft_username} ({member.mention})\n"
            f"**Gamemode:** {gamemode_label(gamemode)}\n"
            f"**Removed tier:** {current_tier}",
            ephemeral=True)

    except Exception as e:
        await handle_command_error(interaction, e)


@app_commands.default_permissions(manage_messages=True)
@bot.tree.command(name="forceclose", description="Force close ticket without assigning tier or cooldown")
async def forceclose(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        tester_role = discord.utils.get(interaction.guild.roles,
                                        name=TESTER_ROLE_NAME)
        if not tester_role or tester_role not in interaction.user.roles:
            await interaction.followup.send("❌ You need the Tester role to use this command.",
                                            ephemeral=True)
            return

        channel = interaction.channel
        ticket_info = open_tickets.get(channel.id)
        
        # If not in memory, try loading from Firestore
        if not ticket_info:
            ticket_info = await load_ticket_from_db(channel.id)
            if ticket_info:
                # Cache it in memory
                open_tickets[channel.id] = {
                    "user_id": ticket_info["testee_id"],
                    "gamemode": ticket_info["gamemode"],
                }
        
        if not ticket_info:
            await interaction.followup.send(
                "This command only works in a ticket opened by /next, /skip, or /forcetest.",
                ephemeral=True)
            return

        # Extract info from ticket
        if "user_id" in ticket_info:
            target_id = ticket_info["user_id"]
        else:
            target_id = ticket_info["testee_id"]
        gamemode = ticket_info["gamemode"]

        if interaction.user.id == target_id:
            await interaction.followup.send(
                "❌ You cannot force close your own ticket.",
                ephemeral=True)
            return

        member = interaction.guild.get_member(target_id)
        member_name = member.display_name if member else f"User {target_id}"

        # Log command usage
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"[{timestamp}] /forceclose - Used by: {interaction.user} ({interaction.user.id}) | Target: {member_name} ({target_id}) | Queue: {gamemode}")

        # Remove cooldown for this gamemode (since test is cancelled)
        await remove_user_cooldown(str(target_id), gamemode)

        # Remove user from waitlist
        key = get_waitlist_key(gamemode)
        async with state_lock:
            if key in state["waitlists"] and target_id in state["waitlists"][key]:
                state["waitlists"][key].remove(target_id)
            
            # Remove only the target player from the queue if they are in it
            if key in state["queues"] and target_id in state["queues"][key]:
                state["queues"][key].remove(target_id)
            
            save_state()
        
        channel_name = f"waitlist-{gamemode}"
        waitlist_channel = discord.utils.get(interaction.guild.text_channels, name=channel_name)
        if waitlist_channel and member:
            try:
                await waitlist_channel.set_permissions(member, overwrite=None)
            except Exception as e:
                print(f"Could not remove permissions for {member}: {e}")
        
        # Update queue message to show queue is cleared
        await update_queue_message(interaction.guild, key)

        # Message in ticket
        await channel.send(embed=discord.Embed(
            title="Ticket Force Closed",
            description=f"Ticket for {member.mention if member else member_name} was force closed by {interaction.user.mention}.\nNo tier assigned, cooldown removed.",
            color=discord.Color.orange()))

        # Delete ticket from memory and Firestore
        if channel.id in open_tickets:
            del open_tickets[channel.id]
        await delete_ticket_from_db(channel.id)
        await channel.delete(reason=f"Ticket force closed by {interaction.user}")

        await interaction.followup.send(
            f"✅ Ticket force closed for {member_name}. No tier assigned, cooldown removed.",
            ephemeral=True)

    except Exception as e:
        await handle_command_error(interaction, e)


async def gamemode_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """Autocomplete for gamemode parameter"""
    cur = current.lower()
    return [
        app_commands.Choice(name=GAMEMODE_LABELS[g], value=g)
        for g in GAMEMODES
        if cur in g or cur in GAMEMODE_LABELS[g].lower()
    ]


@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(
    tester="The tester (must have Tester role)",
    testee="The player to be tested",
    gamemode="The gamemode for testing"
)
@app_commands.autocomplete(gamemode=gamemode_autocomplete)
@bot.tree.command(name="forcetest", description="Force open a testing ticket without queue")
async def forcetest(interaction: discord.Interaction, tester: discord.Member, testee: discord.Member, gamemode: str):
    await interaction.response.defer(ephemeral=True)
    try:
        tester_role = discord.utils.get(interaction.guild.roles,
                                        name=TESTER_ROLE_NAME)
        if not tester_role or tester_role not in interaction.user.roles:
            await interaction.followup.send("❌ You need the Tester role to use this command.",
                                            ephemeral=True)
            return

        # Validate gamemode
        gamemode = gamemode.lower()
        if gamemode not in GAMEMODES:
            await interaction.followup.send(
                f"❌ Invalid gamemode. Use one of: {', '.join(GAMEMODE_LABELS[g] for g in GAMEMODES)}",
                ephemeral=True
            )
            return

        # Validate tester has the Tester role
        if tester_role not in tester.roles:
            await interaction.followup.send(
                f"❌ {tester.mention} does not have the Tester role.",
                ephemeral=True)
            return

        # Log command usage
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"[{timestamp}] /forcetest - Used by: {interaction.user} ({interaction.user.id}) | Tester: {tester} ({tester.id}) | Testee: {testee} ({testee.id}) | Gamemode: {gamemode}")

        # Check if testee is verified
        if not await is_discord_verified(str(testee.id)):
            await interaction.followup.send(
                f"❌ {testee.mention} is not verified. They need to use `/verify` first.",
                ephemeral=True)
            return

        # Get verified username
        username = await get_verified_username(str(testee.id))
        if not username:
            username = testee.display_name

        # Create ticket
        ticket_channel = await create_ticket_channel(interaction.guild,
                                                     tester, testee, gamemode, username)
        if not ticket_channel:
            await interaction.followup.send("❌ Could not create ticket.",
                                            ephemeral=True)
            return

        # Send ping message
        await ticket_channel.send(f"{testee.mention}")

        # Store ticket info
        open_tickets[ticket_channel.id] = {
            "user_id": testee.id,
            "gamemode": gamemode,
        }

        # Set cooldown for this gamemode
        await set_user_cooldown(str(testee.id), testee.display_name, gamemode, datetime.now(timezone.utc))

        await interaction.followup.send(
            f"✅ Force test ticket opened: {ticket_channel.mention}\n"
            f"**Tester:** {tester.mention}\n"
            f"**Testee:** {testee.mention}\n"
            f"**Gamemode:** {gamemode_label(gamemode)}",
            ephemeral=True)

    except Exception as e:
        print(f"❌ Error in /forcetest: {e}")
        await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)


@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(
    member="The member to remove from cooldown",
    gamemode="Specific gamemode cooldown to remove (leave empty to remove all)"
)
@bot.tree.command(name="removecooldown", description="Remove a user's testing cooldown")
async def removecooldown(interaction: discord.Interaction, member: discord.Member, gamemode: str = None):
    await interaction.response.defer(ephemeral=True)
    try:
        tester_role = discord.utils.get(interaction.guild.roles,
                                        name=TESTER_ROLE_NAME)
        if not tester_role or tester_role not in interaction.user.roles:
            await interaction.followup.send("❌ You need the Tester role to use this command.",
                                            ephemeral=True)
            return

        # Log command usage
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        gamemode_str = f" (gamemode: {gamemode})" if gamemode else " (all gamemodes)"
        print(f"[{timestamp}] /removecooldown - Used by: {interaction.user} ({interaction.user.id}) | Target: {member} ({member.id}){gamemode_str}")

        if gamemode:
            # Remove specific gamemode cooldown
            gamemode = gamemode.lower()
            if gamemode not in GAMEMODES:
                await interaction.followup.send(
                    f"❌ Invalid gamemode. Use one of: {', '.join(GAMEMODE_LABELS[g] for g in GAMEMODES)}",
                    ephemeral=True
                )
                return
            await remove_user_cooldown(str(member.id), gamemode)
            await interaction.followup.send(
                f"✅ **{gamemode_label(gamemode)}** cooldown removed for {member.mention}.",
                ephemeral=True)
        else:
            # Remove all gamemode cooldowns
            try:
                db.collection('updated_cooldowns').document(str(member.id)).delete()
                await interaction.followup.send(
                    f"✅ All cooldowns removed for {member.mention}. They can now be tested again.",
                    ephemeral=True)
            except Exception as e:
                await interaction.followup.send(
                    f"❌ Error removing cooldowns: {e}",
                    ephemeral=True
                )

    except Exception as e:
        await handle_command_error(interaction, e)


@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(member="The member to add to this ticket")
@bot.tree.command(name="add", description="Add a member to the current ticket")
async def add_to_ticket(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.defer(ephemeral=True)
    try:
        tester_role = discord.utils.get(interaction.guild.roles,
                                        name=TESTER_ROLE_NAME)
        if not tester_role or tester_role not in interaction.user.roles:
            await interaction.followup.send("❌ You need the Tester role to use this command.",
                                            ephemeral=True)
            return

        channel = interaction.channel
        
        # Check if ticket exists in memory or Firestore
        if channel.id not in open_tickets:
            ticket_info = await load_ticket_from_db(channel.id)
            if ticket_info:
                # Cache it in memory
                open_tickets[channel.id] = {
                    "user_id": ticket_info["testee_id"],
                    "gamemode": ticket_info["gamemode"],
                }
            else:
                await interaction.followup.send(
                    "❌ This command only works in a ticket channel opened by /next.",
                    ephemeral=True)
                return

        # Log command usage
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"[{timestamp}] /add - Used by: {interaction.user} ({interaction.user.id}) | Added: {member} ({member.id}) | Ticket: {channel.name}")

        # Add permissions for the member
        await channel.set_permissions(member,
                                      read_messages=True,
                                      send_messages=True)
        
        await interaction.followup.send(
            f"✅ {member.mention} has been added to this ticket.",
            ephemeral=True)
        await channel.send(f"{member.mention} has been added to the ticket.")

    except Exception as e:
        await handle_command_error(interaction, e)


@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(member="The member to remove from this ticket")
@bot.tree.command(name="remove", description="Remove a member from the current ticket")
async def remove_from_ticket(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.defer(ephemeral=True)
    try:
        tester_role = discord.utils.get(interaction.guild.roles,
                                        name=TESTER_ROLE_NAME)
        if not tester_role or tester_role not in interaction.user.roles:
            await interaction.followup.send("❌ You need the Tester role to use this command.",
                                            ephemeral=True)
            return

        channel = interaction.channel
        
        # Check if ticket exists in memory or Firestore
        if channel.id not in open_tickets:
            ticket_info = await load_ticket_from_db(channel.id)
            if ticket_info:
                # Cache it in memory
                open_tickets[channel.id] = {
                    "user_id": ticket_info["testee_id"],
                    "gamemode": ticket_info["gamemode"],
                }
            else:
                await interaction.followup.send(
                    "❌ This command only works in a ticket channel opened by /next.",
                    ephemeral=True)
                return

        # Log command usage
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"[{timestamp}] /remove - Used by: {interaction.user} ({interaction.user.id}) | Removed: {member} ({member.id}) | Ticket: {channel.name}")

        # Remove permissions for the member
        await channel.set_permissions(member,
                                      read_messages=False,
                                      send_messages=False)
        
        await interaction.followup.send(
            f"✅ {member.mention} has been removed from this ticket.",
            ephemeral=True)
        await channel.send(f"{member.mention} has been removed from the ticket.")

    except Exception as e:
        await handle_command_error(interaction, e)


@app_commands.describe(member="The member to view information for")
@bot.tree.command(name="info", description="View a player's tier information and test history")
async def info(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.defer(ephemeral=False)
    try:
        # Check if user is verified
        if not await is_discord_verified(str(member.id)):
            await interaction.followup.send(
                f"❌ {member.mention} is not verified. They need to use `/verify` first.",
                ephemeral=True
            )
            return
        
        # Get their Minecraft username and UUID
        minecraft_username = await get_verified_username(str(member.id))
        
        # Get UUID
        uuid = await get_verified_uuid(str(member.id))
        if not uuid:
            await interaction.followup.send(
                "❌ Could not find UUID for this user.",
                ephemeral=True
            )
            return
        
        # Get player data from updated_players table
        player_data = await get_player_data(uuid)
        
        # Get cooldown data from updated_cooldowns table
        conn = db_manager.get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT cooldowns FROM updated_cooldowns WHERE discord_id = ?", (str(member.id),))
        row = cursor.fetchone()
        conn.close()
        cooldown_data = json.loads(row['cooldowns']) if row else {}
        
        # Calculate overall score
        overall_score = calculate_overall_score(player_data) if player_data else 0
        
        # Create embed
        embed = discord.Embed(
            title=f"📊 Player Information: {minecraft_username}",
            color=discord.Color.blue()
        )
        
        embed.add_field(
            name="Discord",
            value=member.mention,
            inline=True
        )
        
        embed.add_field(
            name="Minecraft",
            value=minecraft_username,
            inline=True
        )
        
        embed.add_field(
            name="Overall Score",
            value=str(overall_score),
            inline=True
        )
        
        # Add tier information for each gamemode
        tier_info = []
        for gamemode in GAMEMODES:
            # Get tier
            tier = "Unranked"
            if player_data and gamemode in player_data:
                tier_value = player_data[gamemode]
                if tier_value and tier_value.strip():
                    tier = tier_value
            
            # Get last tested date
            last_tested = " "
            if gamemode in cooldown_data:
                try:
                    test_date = datetime.fromisoformat(cooldown_data[gamemode])
                    # Format date nicely
                    last_tested = test_date.strftime("%b %d, %Y")
                except Exception:
                    last_tested = "Unknown"
            
            # Format: Gamemode: Tier (Last tested: date)
            tier_info.append(f"**{gamemode_label(gamemode)}:** {tier}\n{last_tested}")
        
        # Split into two columns for better display
        half = len(tier_info) // 2
        
        embed.add_field(
            name="🎮 Gamemode Tiers",
            value="\n\n".join(tier_info[:half]),
            inline=True
        )
        
        embed.add_field(
            name="🎮 Gamemode Tiers (cont.)",
            value="\n\n".join(tier_info[half:]),
            inline=True
        )
        
        embed.set_thumbnail(url=f'https://render.crafty.gg/3d/bust/{minecraft_username}')
        embed.set_footer(text=f"UUID: {uuid}")
        
        await interaction.followup.send(embed=embed, ephemeral=True)
        
    except Exception as e:
        print(f"Error in /info: {e}")
        await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)


@app_commands.default_permissions(manage_messages=True)
@bot.tree.command(name="syncuuid", description="Sync Minecraft usernames with current UUIDs")
async def syncuuid(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        tester_role = discord.utils.get(interaction.guild.roles,
                                        name=TESTER_ROLE_NAME)
        if not tester_role or tester_role not in interaction.user.roles:
            await interaction.followup.send("❌ You need the Tester role to use this command.",
                                            ephemeral=True)
            return

        # Log command usage
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"[{timestamp}] /syncuuid - Used by: {interaction.user} ({interaction.user.id})")

        # Get all documents from 'verify' table
        conn = db_manager.get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT discord_id, uuid, username FROM verify")
        verify_rows = cursor.fetchall()
        
        checked_count = len(verify_rows)
        updated_count = 0
        errors = []

        from concurrent.futures import ThreadPoolExecutor

        def _check_user(row):
            stored_username = row['username']
            uuid = row['uuid']
            if not uuid:
                return None
            current_username = get_minecraft_username_from_uuid(uuid)
            if current_username and current_username != stored_username:
                return (uuid, current_username, stored_username)
            return None

        # Fetch current Minecraft usernames in parallel (20 workers)
        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = [loop.run_in_executor(executor, _check_user, row) for row in verify_rows]
            results = await asyncio.gather(*futures)

        # Batch update database for changed usernames
        with db_manager.db_lock:
            for item in results:
                if item:
                    uuid, current_username, stored_username = item
                    try:
                        cursor.execute("UPDATE verify SET username = ? WHERE uuid = ?", (current_username, uuid))
                        cursor.execute("UPDATE updated_players SET username = ? WHERE uuid = ?", (current_username, uuid))
                        updated_count += 1
                    except Exception as e:
                        errors.append(f"{stored_username}: {str(e)}")
            conn.commit()
        conn.close()

        result_message = f"✅ Sync complete!\n\n**Checked:** {checked_count} accounts\n**Updated:** {updated_count} usernames"
        
        if errors:
            result_message += f"\n\n**Errors ({len(errors)}):**\n" + "\n".join(errors[:5])
            if len(errors) > 5:
                result_message += f"\n... and {len(errors) - 5} more errors"
        
        await interaction.followup.send(result_message, ephemeral=True)

    except Exception as e:
        await handle_command_error(interaction, e)


def build_tester_guide_embed() -> discord.Embed:
    """Embed for /tester-commands (guide + command list)."""
    embed = discord.Embed(
        title="❓ HOE TEST JE? ❓",
        description=(
            "**Stap 1:** Ga naar het juiste waitlist-kanaal (`waitlist-{gamemode}`, bv. `waitlist-sword`) "
            "en gebruik **`/start`** om de queue te openen.\n"
            "*Als je al actief bent in een andere gamemode: eerst **`/stop`** daar.*\n\n"
            "**Stap 2:** Als spelers in de queue zitten: **`/next`** in hetzelfde waitlist-kanaal "
            "→ nieuw ticket (testee, actieve testers, Staff, bot).\n\n"
            "**Stap 3:** Test zoals jullie afspreken.\n\n"
            "**Stap 4:** Tier bepalen (**LT5**–**HT1**).\n\n"
            "**Stap 5:** In het ticket **`/close`** met tier → ticket dicht, tier in DB, results-kanaal."
        ),
        color=QUEUE_EMBED_COLOR,
    )
    embed.add_field(
        name="⚙️ Tester commands — queue / waitlist",
        value=(
            "**`/start`** · waitlist-kanaal · Queue openen; jij actieve tester.\n"
            "**`/stop`** · waitlist-kanaal · Stoppen als tester.\n"
            "**`/forcestop`** · waitlist-kanaal · Hele queue sluiten.\n"
            "**`/next`** · waitlist-kanaal · Eerste uit queue → ticket.\n"
            "**`/skip`** · waitlist-kanaal · Tweede uit queue → ticket.\n"
            "**`/removefromqueue`** · waitlist-kanaal · Uit queue halen.\n"
            "**`/removefromwaitlist`** · waar je het gebruikt · Uit waitlist.\n"
            "**`/setup`** · gewenst kanaal · Waitlist-menu."
        ),
        inline=False,
    )
    embed.add_field(
        name="⚙️ Tester commands — tickets / overig",
        value=(
            "**`/close`** · in ticket · Tier + sluiten.\n"
            "**`/forceclose`** · in ticket · Sluiten zonder tier.\n"
            "**`/forcetest`** · overal · Ticket zonder queue.\n"
            "**`/add`** · in ticket · Lid toevoegen.\n"
            "**`/remove`** · in ticket · Lid verwijderen.\n"
            "**`/unverify`** · overal.\n"
            "**`/removecooldown`** · overal.\n"
            "**`/syncuuid`** · overal."
        ),
        inline=False,
    )
    embed.add_field(
        name="📎 Extra · 🔒 admins",
        value=(
            "**`/info`** · **`/verify`** · **`/leave`**\n"
            "**`/sync`** · **`/reverify`**"
        ),
        inline=False,
    )
    embed.set_footer(text="NL Tier List · Verified Tester")
    return embed


async def _send_tester_guide_embed(interaction: discord.Interaction) -> None:
    tester_role = discord.utils.get(interaction.guild.roles, name=TESTER_ROLE_NAME)
    if not tester_role or tester_role not in interaction.user.roles:
        await interaction.response.send_message(
            "❌ You need the Tester role to use this command.",
            ephemeral=True,
        )
        return
    await interaction.response.send_message(
        embed=build_tester_guide_embed(),
        ephemeral=True,
    )


@bot.tree.command(
    name="testerguide",
    description="Overzicht: hoe testen werkt en alle tester-commands",
)
async def testerguide(interaction: discord.Interaction):
    await _send_tester_guide_embed(interaction)


@bot.tree.command(
    name="tester",
    description="Zelfde als /testerguide — tester-uitleg en commandlijst",
)
async def tester_guide_alias(interaction: discord.Interaction):
    await _send_tester_guide_embed(interaction)


async def _handle_eval(interaction: discord.Interaction, tier: str, is_force: bool = False):
    channel = interaction.channel
    ticket_info = open_tickets.get(channel.id)
    
    if not ticket_info:
        ticket_info = await load_ticket_from_db(channel.id)
        if ticket_info:
            open_tickets[channel.id] = {
                "user_id": ticket_info["testee_id"],
                "gamemode": ticket_info["gamemode"],
            }
            
    if not ticket_info:
        await interaction.followup.send("❌ Dit commando kan alleen worden gebruikt in een actief testing ticket.", ephemeral=True)
        return
        
    if "user_id" in ticket_info:
        target_id = ticket_info["user_id"]
    else:
        target_id = ticket_info["testee_id"]
        
    gamemode = ticket_info["gamemode"]
    
    target = interaction.guild.get_member(target_id)
    if not target:
        try:
            target = await interaction.guild.fetch_member(target_id)
        except Exception:
            await interaction.followup.send("❌ Kon de testee niet vinden in de server.", ephemeral=True)
            return

    new_name = f"{gamemode}-{target.display_name}-{tier}"[:100]
    edit_kwargs = {"name": new_name}
    
    category_id = EVAL_CATEGORIES.get(tier.upper())
    if category_id:
        category = interaction.guild.get_channel(category_id)
        if category and isinstance(category, discord.CategoryChannel):
            edit_kwargs["category"] = category
            
    try:
        await channel.edit(**edit_kwargs)
    except Exception as e:
        await interaction.followup.send(f"⚠️ Fout bij het aanpassen van het kanaal: {e}", ephemeral=True)
        # Continue to send the embed even if channel edit failed
        
    embed = discord.Embed(
        title=f"📈 Evaluatie Aanvraag: {tier.upper()}",
        color=0xFFD700) # Gold
    embed.add_field(name="Testee (Speler):", value=target.mention, inline=False)
    embed.add_field(name="Huidige Tester:", value=interaction.user.mention, inline=False)
    embed.add_field(name="Gamemode:", value=gamemode_label(gamemode), inline=False)
    embed.add_field(name="Aangevraagde Tier:", value=tier.upper(), inline=False)
    
    minecraft_username = await get_verified_username(str(target.id))
    if minecraft_username:
        embed.set_thumbnail(url=f"https://mc-heads.net/body/{minecraft_username}")
    elif target.avatar:
        embed.set_thumbnail(url=target.avatar.url)
        
    ping_content = f"<@&{DISCORD_HELPER_ROLE_ID}>"
    await channel.send(content=ping_content, embed=embed)
    
    cmd_type = "/forceeval" if is_force else "/eval"
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{timestamp}] {cmd_type} - Used by: {interaction.user} | Target: {target} | Gamemode: {gamemode} | Tier: {tier}")
    
    await interaction.followup.send(f"✅ Evaluatie voor {tier.upper()} succesvol aangevraagd!", ephemeral=True)


@bot.tree.command(name="eval", description="Vraag een evaluatie aan voor een hogere tier")
@app_commands.describe(tier="De tier waar de testee voor geëvalueerd moet worden (bijv. HT3)")
async def eval_cmd(interaction: discord.Interaction, tier: str):
    await interaction.response.defer()
    try:
        tester_role = discord.utils.get(interaction.guild.roles, id=VERIFIED_TESTER_ROLE_ID)
        # Fallback to name if ID fails
        if not tester_role:
            tester_role = discord.utils.get(interaction.guild.roles, name=TESTER_ROLE_NAME)
            
        if not tester_role or tester_role not in interaction.user.roles:
            await interaction.followup.send("❌ Alleen Verified Testers mogen dit commando gebruiken.", ephemeral=True)
            return
            
        await _handle_eval(interaction, tier, is_force=False)
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)


@bot.tree.command(name="forceeval", description="Vraag een evaluatie aan (Regulator Only)")
@app_commands.describe(tier="De tier waar de testee voor geëvalueerd moet worden (bijv. HT3)")
async def forceeval_cmd(interaction: discord.Interaction, tier: str):
    await interaction.response.defer()
    try:
        has_regulator_role = any(role.id in FORCEEVAL_ROLE_IDS for role in interaction.user.roles)
        if not has_regulator_role:
            await interaction.followup.send("❌ Alleen Regulators mogen dit commando gebruiken.", ephemeral=True)
            return
            
        await _handle_eval(interaction, tier, is_force=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)


RESTRICT_REASONS = {
    "boosting": {
        "reason_text": "Boosting",
        "duration_text": "30d",
        "duration_days": 30,
        "appealable_text": "1 maand",
    },
    "overweging_boosting": {
        "reason_text": "Overweging om geboost te worden",
        "duration_text": "90d",
        "duration_days": 90,
        "appealable_text": "2 maanden",
    },
    "alting": {
        "reason_text": "Alting",
        "duration_text": "90d",
        "duration_days": 90,
        "appealable_text": "2 maanden",
    },
    "banned_mid_test": {
        "reason_text": "Banned mid test",
        "duration_text": "100d",
        "duration_days": 100,
        "appealable_text": "3 maanden",
    },
    "account_sharing": {
        "reason_text": "Account sharing",
        "duration_text": "110d",
        "duration_days": 110,
        "appealable_text": "3 maanden",
    },
    "faking_scores": {
        "reason_text": "Faking scores",
        "duration_text": "60d",
        "duration_days": 60,
        "appealable_text": "1 maand",
    },
    "faking_fights": {
        "reason_text": "Faking fights",
        "duration_text": "60d",
        "duration_days": 60,
        "appealable_text": "1 maand",
    },
    "weigering_ss": {
        "reason_text": "Weigering van screenshare",
        "duration_text": "Permanent",
        "duration_days": None,
        "appealable_text": "2 maanden",
    },
    "cheating": {
        "reason_text": "Cheating",
        "duration_text": "Permanent",
        "duration_days": None,
        "appealable_text": "Never",
    },
    "alt_restricted": {
        "reason_text": "Alt account van een restricted persoon",
        "duration_text": "Permanent",
        "duration_days": None,
        "appealable_text": "Never",
    },
}

@app_commands.default_permissions(manage_roles=True)
@bot.tree.command(name="restrict", description="Restrict spelers met (gestapelde) redenen")
@app_commands.describe(
    speler="De hoofdspeler die je wilt restricten",
    reden1="Hoofdreden van de restrictie",
    reden2="Optionele 2e reden (voor gestapelde restrictie)",
    reden3="Optionele 3e reden (voor gestapelde restrictie)",
    speler2="Optionele 2e speler (bijv. alts)",
    speler3="Optionele 3e speler (bijv. alts)",
    speler4="Optionele 4e speler (bijv. alts)"
)
@app_commands.choices(
    reden1=[
        app_commands.Choice(name="Boosting (30d)", value="boosting"),
        app_commands.Choice(name="Overweging om geboost te worden (90d)", value="overweging_boosting"),
        app_commands.Choice(name="Alting (90d)", value="alting"),
        app_commands.Choice(name="Banned mid test (100d)", value="banned_mid_test"),
        app_commands.Choice(name="Account sharing (110d)", value="account_sharing"),
        app_commands.Choice(name="Faking scores (60d)", value="faking_scores"),
        app_commands.Choice(name="Faking fights (60d)", value="faking_fights"),
        app_commands.Choice(name="Weigering van screenshare (Permanent)", value="weigering_ss"),
        app_commands.Choice(name="Cheating (Permanent)", value="cheating"),
        app_commands.Choice(name="Alt account van een restricted persoon (Permanent)", value="alt_restricted"),
    ],
    reden2=[
        app_commands.Choice(name="Boosting (30d)", value="boosting"),
        app_commands.Choice(name="Overweging om geboost te worden (90d)", value="overweging_boosting"),
        app_commands.Choice(name="Alting (90d)", value="alting"),
        app_commands.Choice(name="Banned mid test (100d)", value="banned_mid_test"),
        app_commands.Choice(name="Account sharing (110d)", value="account_sharing"),
        app_commands.Choice(name="Faking scores (60d)", value="faking_scores"),
        app_commands.Choice(name="Faking fights (60d)", value="faking_fights"),
        app_commands.Choice(name="Weigering van screenshare (Permanent)", value="weigering_ss"),
        app_commands.Choice(name="Cheating (Permanent)", value="cheating"),
        app_commands.Choice(name="Alt account van een restricted persoon (Permanent)", value="alt_restricted"),
    ],
    reden3=[
        app_commands.Choice(name="Boosting (30d)", value="boosting"),
        app_commands.Choice(name="Overweging om geboost te worden (90d)", value="overweging_boosting"),
        app_commands.Choice(name="Alting (90d)", value="alting"),
        app_commands.Choice(name="Banned mid test (100d)", value="banned_mid_test"),
        app_commands.Choice(name="Account sharing (110d)", value="account_sharing"),
        app_commands.Choice(name="Faking scores (60d)", value="faking_scores"),
        app_commands.Choice(name="Faking fights (60d)", value="faking_fights"),
        app_commands.Choice(name="Weigering van screenshare (Permanent)", value="weigering_ss"),
        app_commands.Choice(name="Cheating (Permanent)", value="cheating"),
        app_commands.Choice(name="Alt account van een restricted persoon (Permanent)", value="alt_restricted"),
    ]
)
async def restrict(
    interaction: discord.Interaction,
    speler: discord.Member,
    reden1: app_commands.Choice[str],
    reden2: Optional[app_commands.Choice[str]] = None,
    reden3: Optional[app_commands.Choice[str]] = None,
    speler2: Optional[discord.Member] = None,
    speler3: Optional[discord.Member] = None,
    speler4: Optional[discord.Member] = None
):
    await interaction.response.defer(ephemeral=True)
    try:
        restricted_role = interaction.guild.get_role(RESTRICTED_ROLE_ID)
        if not restricted_role:
            await interaction.followup.send("❌ Kon de Restricted rol niet vinden in deze server.", ephemeral=True)
            return

        # Collect unique players
        target_players = []
        for p in [speler, speler2, speler3, speler4]:
            if p and p not in target_players:
                target_players.append(p)

        # Collect unique reasons
        selected_choices = [c for c in [reden1, reden2, reden3] if c is not None]
        unique_reasons = []
        for c in selected_choices:
            if c.value not in [x.value for x in unique_reasons]:
                unique_reasons.append(c)

        # Calculate stacked duration and combined reason text
        is_permanent = False
        total_days = 0
        reason_texts = []
        max_appealable_days = 0
        is_tier_wipe = False

        for c in unique_reasons:
            info = RESTRICT_REASONS.get(c.value, {
                "reason_text": c.name,
                "duration_text": "Permanent",
                "duration_days": None,
                "appealable_text": "Never"
            })
            reason_texts.append(info["reason_text"])

            if c.value in ["cheating", "account_sharing", "alt_restricted"]:
                is_tier_wipe = True

            if info["duration_days"] is None or str(info["duration_text"]).lower() == "permanent":
                is_permanent = True
            else:
                total_days += info["duration_days"]

            # Parse appealable days
            app_str = info["appealable_text"].lower()
            if "maand" in app_str:
                try:
                    num = int(app_str.split()[0])
                    max_appealable_days = max(max_appealable_days, num * 30)
                except Exception:
                    pass
            elif "dag" in app_str:
                try:
                    num = int(app_str.split()[0])
                    max_appealable_days = max(max_appealable_days, num)
                except Exception:
                    pass

        if is_permanent:
            duration_display = "Permanent"
        else:
            duration_display = f"{total_days}d"

        if len(reason_texts) > 1:
            reason_display = f"Stacked offense - {' + '.join(reason_texts)}"
        else:
            reason_display = reason_texts[0]

        if is_permanent and max_appealable_days == 0:
            appealable_display = "Never"
        elif max_appealable_days >= 30:
            months = max_appealable_days // 30
            appealable_display = f"{months} maand" if months == 1 else f"{months} maanden"
        else:
            appealable_display = f"{max_appealable_days} dagen"

        # Expiration line
        expires_line = ""
        if not is_permanent and total_days > 0:
            expire_time = datetime.now(timezone.utc) + timedelta(days=total_days)
            unix_ts = int(expire_time.timestamp())
            expires_line = f"\n**Expires:** <t:{unix_ts}:F>"
        elif is_permanent:
            expires_line = "\n**Expires:** Never"

        # Build mentions list
        mentions_str = " / ".join([p.mention for p in target_players])

        # Process each target player (Roles, Queues, Database, Tierwipe)
        blockquote_lines = []
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        for p in target_players:
            # 1. Add Restricted role
            try:
                await p.add_roles(restricted_role, reason=f"Restricted door {interaction.user.display_name}: {reason_display}")
            except Exception as e:
                print(f"Error adding restricted role to {p}: {e}")

            # 2. Remove from queues & waitlists
            await remove_user_from_all_waitlists_and_queues(interaction.guild, p)

            # 3. Handle tierwipe if applicable
            if is_tier_wipe:
                try:
                    roles_to_remove = [
                        r for r in p.roles
                        if not r.is_default() and r.id != RESTRICTED_ROLE_ID and "member" not in r.name.lower()
                    ]
                    if roles_to_remove:
                        await p.remove_roles(*roles_to_remove, reason=f"Tierwipe due to restriction: {reason_display}")
                except Exception as e:
                    print(f"Error removing roles from {p}: {e}")

            # 4. Fetch Minecraft Info
            mc_user = await get_verified_username(str(p.id)) or p.display_name
            mc_uuid = await get_verified_uuid(str(p.id))
            if not mc_uuid:
                mojang = get_minecraft_uuid(mc_user)
                mc_uuid = mojang["uuid"] if (mojang and "uuid" in mojang) else "Unknown UUID"

            if len(mc_uuid) == 32:
                mc_uuid = f"{mc_uuid[:8]}-{mc_uuid[8:12]}-{mc_uuid[12:16]}-{mc_uuid[16:20]}-{mc_uuid[20:]}"

            # Clear DB if tierwipe
            if is_tier_wipe and mc_uuid != "Unknown UUID":
                try:
                    conn = db_manager.get_db()
                    cursor = conn.cursor()
                    wipe_dict = {"username": mc_user, "overall": 0}
                    for gm in GAMEMODES:
                        wipe_dict[gm] = ""
                        wipe_dict[f"{gm}_peak"] = ""
                    set_clause = ", ".join([f"{k} = ?" for k in wipe_dict.keys()])
                    params = [wipe_dict[k] for k in wipe_dict.keys()] + [mc_uuid]
                    with db_manager.db_lock:
                        cursor.execute(f"UPDATE updated_players SET {set_clause} WHERE uuid = ?", params)
                        conn.commit()
                    conn.close()
                except Exception as e:
                    print(f"Error wiping database tiers for {p}: {e}")

            blockquote_lines.append(f"> {mc_user} - `{mc_uuid}`")

            # 5. DM Notification to player
            try:
                dm_embed = discord.Embed(
                    title="You were restricted",
                    color=0xE74C3C
                )
                avatar_url = p.display_avatar.url if p.display_avatar else None
                if avatar_url:
                    dm_embed.set_thumbnail(url=avatar_url)
                dm_embed.add_field(name="Offense", value=reason_display, inline=False)
                dm_embed.add_field(name="Duration", value=duration_display, inline=False)
                if not is_permanent and total_days > 0:
                    expire_time = datetime.now(timezone.utc) + timedelta(days=total_days)
                    unix_ts = int(expire_time.timestamp())
                    dm_embed.add_field(name="Expires", value=f"<t:{unix_ts}:F>", inline=False)
                else:
                    dm_embed.add_field(name="Expires", value="Never", inline=False)

                await p.send(embed=dm_embed)
            except Exception as e:
                print(f"Could not DM restricted player {p}: {e}")

        # Update leaderboard message if tierwipe occurred
        if is_tier_wipe:
            try:
                await update_leaderboard_message(interaction.guild)
            except Exception as e:
                print(f"Error updating leaderboard: {e}")

        # Build public punishment message matching Bijlage 1
        blockquote_str = "\n".join(blockquote_lines)
        message_text = (
            f"{mentions_str} has been **Restricted** ({duration_display} - {reason_display})\n\n"
            f"{blockquote_str}\n\n"
            f"**Appealable:** ||{appealable_display}||"
            f"{expires_line}"
        )

        # Send to straffen channel
        straffen_channel = interaction.guild.get_channel(STRAFFEN_CHANNEL_ID)
        if straffen_channel:
            await straffen_channel.send(content=message_text)
        else:
            print(f"[{timestamp}] Could not find straffen channel with ID {STRAFFEN_CHANNEL_ID}")

        await interaction.followup.send(f"✅ Restriction(s) for **{mentions_str}** processed successfully ({duration_display}).", ephemeral=True)

    except discord.Forbidden:
        await interaction.followup.send("❌ Geen rechten om rollen toe te wijzen/verwijderen.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Error bij restrictie: {e}", ephemeral=True)



# /unrestrict: Unrestrict a player
@app_commands.default_permissions(manage_roles=True)
@bot.tree.command(name="unrestrict", description="Verwijder een restrictie van een speler")
@app_commands.describe(speler="De speler die je wilt un-restricten")
async def unrestrict(interaction: discord.Interaction, speler: discord.Member):
    await interaction.response.defer(ephemeral=True)
    try:
        restricted_role = interaction.guild.get_role(RESTRICTED_ROLE_ID)
        if not restricted_role:
            await interaction.followup.send("❌ Kon de Restricted rol niet vinden in deze server.", ephemeral=True)
            return

        if restricted_role not in speler.roles:
            await interaction.followup.send(f"❌ {speler.mention} heeft de Restricted rol niet.", ephemeral=True)
            return

        await speler.remove_roles(restricted_role, reason=f"Un-restricteerd door {interaction.user.display_name}")

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"[{timestamp}] /unrestrict - Used by: {interaction.user} | Target: {speler}")

        await interaction.followup.send(f"✅ Restrictie is verwijderd van {speler.mention}.", ephemeral=True)

    except discord.Forbidden:
        await interaction.followup.send("❌ Ik heb niet de juiste rechten.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)


# /start: Open queue for this gamemode (must be in waitlist channel)
@app_commands.default_permissions(manage_messages=True)
@bot.tree.command(name="start", description="Start the testing queue for this waitlist")
async def start(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    try:
        tester_role = discord.utils.get(interaction.guild.roles,
                                        name=TESTER_ROLE_NAME)
        has_tester_role = tester_role and tester_role in interaction.user.roles
        has_new_role = any(role.id == 1498060823833346122 for role in interaction.user.roles)
        
        if not (has_tester_role or has_new_role):
            await interaction.followup.send("❌ You need the Tester role or correct permissions to use this command.",
                                            ephemeral=True)
            return


        gamemode = parse_waitlist_channel(interaction.channel)
        if not gamemode:
            await interaction.followup.send(
                "❌ This command must be used in a waitlist channel (format: `waitlist-{gamemode}`).",
                ephemeral=True)
            return

        key = get_waitlist_key(gamemode)

        # Block only if this tester is already active in a *different* gamemode queue
        existing_queue = find_tester_active_queue(interaction.user.id)
        if existing_queue and existing_queue != key:
            await interaction.followup.send(
                f"❌ You are already testing in **{gamemode_label(existing_queue)}**. Use `/stop` there first.",
                ephemeral=True)
            return

        already_active_here = False
        async with state_lock:
            # Initialize structures if needed
            if key not in state["active_testers"]:
                state["active_testers"][key] = []
            if key not in state["queues"]:
                state["queues"][key] = []
            if key not in state["waitlists"]:
                state["waitlists"][key] = []
            if "queue_messages" not in state:
                state["queue_messages"] = {}
            
            if interaction.user.id in state["active_testers"][key]:
                already_active_here = True
            else:
                is_first_tester = len(state["active_testers"][key]) == 0

                # Add tester
                state["active_testers"][key].append(interaction.user.id)

                # Check if there are now multiple testers
                has_multiple_testers = len(state["active_testers"][key]) > 1

                # Get existing ping message if any
                existing_ping_msg = None
                if key in state.get("ping_messages", {}):
                    existing_ping_msg = state["ping_messages"][key]

                save_state()

        if already_active_here:
            await interaction.followup.send(
                "ℹ️ You are already registered as an active tester for this waitlist.",
                ephemeral=True)
            return

        await grant_tester_access_to_open_tickets(interaction.guild, key, interaction.user)

        # Handle @here ping message
        try:
            # If there are multiple testers, delete and resend the @here message
            if has_multiple_testers and existing_ping_msg:
                try:
                    channel = interaction.guild.get_channel(existing_ping_msg["channel_id"])
                    if channel:
                        msg = await channel.fetch_message(existing_ping_msg["message_id"])
                        await msg.delete()
                except Exception as e:
                    print(f"Could not delete old @here ping: {e}")
            
            # Send new @here ping if first tester OR if there are multiple testers
            if is_first_tester or has_multiple_testers:
                ping_msg = await interaction.channel.send("@here")
                async with state_lock:
                    if "ping_messages" not in state:
                        state["ping_messages"] = {}
                    state["ping_messages"][key] = {
                        "channel_id": interaction.channel.id,
                        "message_id": ping_msg.id
                    }
                    save_state()
        except Exception as e:
            print(f"Could not send @here ping: {e}")
        
        # Send or update queue message
        try:
            embed = await get_queue_embed_for_key(interaction.guild, key)
            view = JoinQueueView(key)
            
            # Check if queue message already exists
            if key in state.get("queue_messages", {}):
                # Update existing message
                await update_queue_message(interaction.guild, key)
            else:
                # Send new queue message
                queue_msg = await interaction.channel.send(embed=embed, view=view)
                async with state_lock:
                    if "queue_messages" not in state:
                        state["queue_messages"] = {}
                    state["queue_messages"][key] = {
                        "channel_id": interaction.channel.id,
                        "message_id": queue_msg.id
                    }
            save_state()
        except Exception as e:
            print(f"Error sending queue message: {e}")

        await interaction.followup.send(
            f"✅ Queue opened for **{gamemode_label(gamemode)}**!\n"
            f"Users in the waitlist can now join the queue using the buttons below.",
                                        ephemeral=True)

    except Exception as e:
        print(f"❌ Error in /start: {e}")
        await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)


# /stop: Remove tester from queue, move users back to waitlist if last tester
@app_commands.default_permissions(manage_messages=True)
@bot.tree.command(name="stop", description="Stop testing for this waitlist's queue")
async def stop(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    try:
        tester_role = discord.utils.get(interaction.guild.roles,
                                        name=TESTER_ROLE_NAME)
        if not tester_role or tester_role not in interaction.user.roles:
            await interaction.followup.send("❌ You need the Tester role to use this command.",
                                            ephemeral=True)
            return

        gamemode = parse_waitlist_channel(interaction.channel)
        if not gamemode:
            await interaction.followup.send(
                "❌ This command must be used in a waitlist channel (format: `waitlist-{gamemode}`).",
                ephemeral=True)
            return

        key = get_waitlist_key(gamemode)

        async with state_lock:
            # Check if tester is active in this queue
            if key not in state["active_testers"] or interaction.user.id not in state["active_testers"][key]:
                await interaction.followup.send(
                    f"❌ You are not testing in **{gamemode_label(gamemode)}**.",
                                                ephemeral=True)
                return

            # Remove tester
            state["active_testers"][key].remove(interaction.user.id)
            
            # Check if last tester
            is_last_tester = len(state["active_testers"][key]) == 0
            
            if is_last_tester:
                if "last_testing_sessions" not in state:
                    state["last_testing_sessions"] = {}
                state["last_testing_sessions"][key] = int(datetime.now(timezone.utc).timestamp())
                # Clear the queue
                if key in state["queues"]:
                    state["queues"][key] = []
                
                # Delete ping message
                if key in state.get("ping_messages", {}):
                    ping_info = state["ping_messages"][key]
                    del state["ping_messages"][key]
                    
                    # Try to delete the ping message
                    try:
                        channel = interaction.guild.get_channel(ping_info["channel_id"])
                        if channel:
                            ping_msg = await channel.fetch_message(ping_info["message_id"])
                            await ping_msg.delete()
                    except Exception as e:
                        print(f"Could not delete ping message: {e}")
                
                save_state()
                
                # Update queue message to show closed state (don't delete it)
                await update_queue_message(interaction.guild, key)
                
                await interaction.followup.send(
                    f"✅ Queue closed for **{gamemode_label(gamemode)}**.",
                    ephemeral=True)
            else:
                save_state()

                await revoke_tester_access_from_open_tickets(
                    interaction.guild, key, interaction.user)

                # Update queue message to reflect removed tester
                await update_queue_message(interaction.guild, key)
                
                await interaction.followup.send(
                    f"✅ You've stopped testing **{gamemode_label(gamemode)}**. Queue is still active.",
                    ephemeral=True)

    except Exception as e:
        print(f"❌ Error in /stop: {e}")
        await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)


@app_commands.default_permissions(manage_messages=True)
@bot.tree.command(name="forcestop", description="Force close the testing queue regardless of active testers")
async def forcestop(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    try:
        tester_role = discord.utils.get(interaction.guild.roles,
                                        name=TESTER_ROLE_NAME)
        if not tester_role or tester_role not in interaction.user.roles:
            await interaction.followup.send("❌ You need the Tester role to use this command.",
                                            ephemeral=True)
            return

        gamemode = parse_waitlist_channel(interaction.channel)
        if not gamemode:
            await interaction.followup.send(
                "❌ This command must be used in a waitlist channel (format: `waitlist-{gamemode}`).",
                ephemeral=True)
            return

        key = get_waitlist_key(gamemode)

        # Log command usage
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"[{timestamp}] /forcestop - Used by: {interaction.user} ({interaction.user.id}) | Queue: {key}")

        async with state_lock:
            # Check if there's an active queue
            if key not in state.get("active_testers", {}) or not state["active_testers"][key]:
                await interaction.followup.send(
                    f"❌ There is no active queue for **{gamemode_label(gamemode)}**.",
                    ephemeral=True)
                return
            
            # Remove all testers
            if "last_testing_sessions" not in state:
                state["last_testing_sessions"] = {}
            state["last_testing_sessions"][key] = int(datetime.now(timezone.utc).timestamp())
            state["active_testers"][key] = []
            
            # Clear the queue
            if key in state["queues"]:
                state["queues"][key] = []
            
            # Delete ping message
            if key in state.get("ping_messages", {}):
                ping_info = state["ping_messages"][key]
                del state["ping_messages"][key]
                
                # Try to delete the ping message
                try:
                    channel = interaction.guild.get_channel(ping_info["channel_id"])
                    if channel:
                        ping_msg = await channel.fetch_message(ping_info["message_id"])
                        await ping_msg.delete()
                except Exception as e:
                    print(f"Could not delete ping message: {e}")

            save_state()

            # Update queue message to show closed state (don't delete it)
            await update_queue_message(interaction.guild, key)

        await interaction.followup.send(
                f"✅ Queue force closed for **{gamemode_label(gamemode)}**. All testers removed.",
            ephemeral=True)

    except Exception as e:
        print(f"❌ Error in /forcestop: {e}")
        await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)


@app_commands.default_permissions(administrator=True)
@bot.tree.command(name="setup_leaderboard", description="Setup the live tester leaderboard message (Admin only)")
async def setup_leaderboard(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        # Create an initial placeholder message
        embed = discord.Embed(
            title="Tester Leaderboard",
            description="Loading leaderboard data...",
            color=0x2b2d31
        )
        msg = await interaction.channel.send(embed=embed)
        
        # Save to state
        async with state_lock:
            state["leaderboard_message"] = {
                "channel_id": interaction.channel.id,
                "message_id": msg.id
            }
            save_state()
            
        # Update immediately
        await update_leaderboard_message(interaction.guild)
        
        await interaction.followup.send("✅ Live leaderboard setup successfully!", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Error setting up leaderboard: {e}", ephemeral=True)


@app_commands.default_permissions(manage_messages=True)
@bot.tree.command(name="stats", description="View your personal tester stats")
async def stats(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        tester_role = discord.utils.get(interaction.guild.roles, name=TESTER_ROLE_NAME)
        if not tester_role or tester_role not in interaction.user.roles:
            await interaction.followup.send("❌ You need the Tester role to view tester stats.", ephemeral=True)
            return
            
        stats_data = await get_tester_stats(str(interaction.user.id))
        
        now = datetime.now(timezone.utc)
        month_key = now.strftime("%Y-%m")
        month_name = now.strftime("%B")
        
        all_time = stats_data.get('all_time_tests', 0)
        monthly = stats_data.get('monthly_tests', {}).get(month_key, 0)
        
        desc = f"**All Time:** {all_time}\n"
        desc += f"**{month_name}:** {monthly}\n"
        
        embed = discord.Embed(
            title=f"{interaction.user.display_name}'s Tester Stats",
            description=desc,
            color=0x2b2d31
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url if interaction.user.display_avatar else None)
        embed.set_footer(
            text="NL┃Testing Community", 
            icon_url="https://nltiers.vercel.app/logo.png"
        )
        
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Error getting stats: {e}", ephemeral=True)



@bot.tree.command(name="synctiers", description="Sync everyone's tier roles to the database")
async def synctiers(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("❌ You need administrator permissions to use this command.", ephemeral=True)
        return
        
    try:
        # Fetch all guild members at once using async comprehension
        guild_members = [m async for m in interaction.guild.fetch_members(limit=None)]
        member_map = {m.id: m for m in guild_members}
        
        updated = 0
        conn = db_manager.get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT discord_id, uuid, username FROM verify")
        rows = cursor.fetchall()
        
        for row in rows:
            discord_id = row['discord_id']
            uuid = row['uuid']
            username = row['username']
            
            if not discord_id or not uuid or not username:
                continue
                
            member = member_map.get(int(discord_id))
            if not member:
                continue
            
            # Find tier roles of the user
            current_tiers = {}
            for gamemode in GAMEMODES:
                label = format_gamemode_name(gamemode)
                found_tier = ""
                for tier in TIER_ROLES:
                    role_name = f"{tier} {label}"
                    role = discord.utils.find(lambda r: r.name.lower() == role_name.lower(), member.roles)
                    if role:
                        found_tier = tier
                        break
                current_tiers[gamemode] = found_tier

                
            player_data = await get_player_data(uuid)
            if not player_data:
                player_data = {
                    'username': username,
                    'uuid': uuid,
                    'overall': 0
                }
                for g in GAMEMODES:
                    player_data[g] = ""
                    
            changed = False
            # Clear any legacy gamemodes not active anymore
            for key in list(player_data.keys()):
                if key not in ['uuid', 'username', 'overall'] and key not in GAMEMODES:
                    if player_data[key] != "":
                        player_data[key] = ""
                        changed = True

            for g in GAMEMODES:
                if player_data.get(g, "") != current_tiers[g]:
                    player_data[g] = current_tiers[g]
                    changed = True
            
            new_overall = calculate_overall_score(player_data)
            if player_data.get('overall', 0) != new_overall:
                player_data['overall'] = new_overall
                changed = True
                    
            if changed:
                player_data['username'] = username
                # Update columns
                set_clause = ", ".join([f"{k} = ?" for k in player_data.keys() if k != 'uuid'])
                params = [player_data[k] for k in player_data.keys() if k != 'uuid'] + [uuid]
                
                with db_manager.db_lock:
                    cursor.execute(f"UPDATE updated_players SET {set_clause} WHERE uuid = ?", params)
                    conn.commit()
                updated += 1
        conn.close()
                
        await interaction.followup.send(f"✅ Successfully synced {updated} players to the database.", ephemeral=True)
    except Exception as e:
        await handle_command_error(interaction, e)


@app_commands.default_permissions(administrator=True)
@bot.tree.command(name="cleanuproles", description="Cleanup duplicate/grey tier roles in the server (Admin only)")
async def cleanuproles(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("❌ You need administrator permissions to use this command.", ephemeral=True)
        return
        
    try:
        guild = interaction.guild
        cleaned_count = 0
        roles_deleted = 0
        
        # We group all roles by their lowercase name
        role_groups = {}
        for role in guild.roles:
            role_name_lower = role.name.lower()
            # Check if this role is a tier role (e.g. "ht3 spear mace")
            is_tier_role = False
            for tier in TIER_ROLES:
                for gamemode in GAMEMODES:
                    formatted_gm = format_gamemode_name(gamemode)
                    if role_name_lower == f"{tier} {formatted_gm}".lower():
                        is_tier_role = True
                        break
                if is_tier_role:
                    break
            
            if is_tier_role:
                if role_name_lower not in role_groups:
                    role_groups[role_name_lower] = []
                role_groups[role_name_lower].append(role)
                
        # Now we process groups that have more than 1 role (duplicates)
        for name_lower, roles in role_groups.items():
            if len(roles) <= 1:
                continue
                
            # Find the best role (the one with a custom color, or the first one if none have color)
            # Default color value is 0 (or Color.default())
            correct_role = None
            for r in roles:
                if r.color.value != 0:
                    correct_role = r
                    break
            if not correct_role:
                correct_role = roles[0] # Fallback to first
                
            # The others are duplicate roles to be removed and deleted
            duplicate_roles = [r for r in roles if r.id != correct_role.id]
            
            for dup_role in duplicate_roles:
                # Move members from dup_role to correct_role
                for member in dup_role.members:
                    try:
                        # Remove duplicate, add correct
                        await member.remove_roles(dup_role, reason="Role cleanup: removing duplicate")
                        if correct_role not in member.roles:
                            await member.add_roles(correct_role, reason="Role cleanup: assigning colored role")
                        cleaned_count += 1
                    except Exception as e:
                        print(f"Error migrating roles for member {member.display_name}: {e}")
                        
                # Delete the duplicate role from the server
                try:
                    await dup_role.delete(reason="Role cleanup: duplicate of " + correct_role.name)
                    roles_deleted += 1
                except Exception as e:
                    print(f"Error deleting duplicate role {dup_role.name}: {e}")
                    
        await interaction.followup.send(
            f"✅ **Role Cleanup Complete!**\n"
            f"• Members updated: **{cleaned_count}**\n"
            f"• Duplicate/grey roles deleted: **{roles_deleted}**",
            ephemeral=True
        )
    except Exception as e:
        await handle_command_error(interaction, e)


@bot.tree.command(name="sus", description="Markeer een speler als verdacht van cheats")
@app_commands.describe(speler="De speler die je wilt markeren", reden="De reden waarom deze speler verdacht is")
async def sus_command(interaction: discord.Interaction, speler: discord.Member, reden: str = "Geen reden opgegeven"):
    await interaction.response.defer(ephemeral=True)
    try:
        regulator_role = discord.utils.get(interaction.guild.roles, id=1498060823845797924)
        if not interaction.user.guild_permissions.administrator and (not regulator_role or regulator_role not in interaction.user.roles):
            await interaction.followup.send("❌ Je hebt niet de juiste permissies (Regulator) om dit te doen.", ephemeral=True)
            return

        discord_id = str(speler.id)
        
        async with state_lock:
            if "sus_list" not in state:
                state["sus_list"] = {}
            
            state["sus_list"][discord_id] = {
                "reason": reden,
                "added_by": str(interaction.user.id)
            }
            save_state()

        await update_suslist_message(interaction.guild)
        await interaction.followup.send(f"✅ {speler.mention} is succesvol gemarkeerd als verdacht.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Fout tijdens het toevoegen aan de sus-lijst: {e}", ephemeral=True)


@bot.tree.command(name="unsus", description="Verwijder een speler van de verdachtenlijst")
@app_commands.describe(speler="De speler die je wilt verwijderen")
async def unsus_command(interaction: discord.Interaction, speler: discord.Member):
    await interaction.response.defer(ephemeral=True)
    try:
        regulator_role = discord.utils.get(interaction.guild.roles, id=1498060823845797924)
        if not interaction.user.guild_permissions.administrator and (not regulator_role or regulator_role not in interaction.user.roles):
            await interaction.followup.send("❌ Je hebt niet de juiste permissies (Regulator) om dit te doen.", ephemeral=True)
            return

        sus_list = state.get("sus_list", {})
        discord_id = str(speler.id)
        
        if discord_id not in sus_list:
            await interaction.followup.send(f"❌ {speler.mention} staat momenteel niet op de sus-lijst.", ephemeral=True)
            return

        async with state_lock:
            del state["sus_list"][discord_id]
            save_state()

        await update_suslist_message(interaction.guild)
        await interaction.followup.send(f"✅ {speler.mention} is succesvol verwijderd van de sus-lijst.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Fout tijdens het verwijderen van de sus-lijst: {e}", ephemeral=True)


@bot.tree.command(name="setup_suslist", description="Setup het live verdachtenlijst kanaal (Admin only)")
async def setup_suslist(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        if not interaction.user.guild_permissions.administrator:
            await interaction.followup.send("❌ You need administrator permissions to use this command.", ephemeral=True)
            return

        embed = discord.Embed(
            title="🚨 Suspected Cheaters List 🚨",
            description="Loading sus list data...",
            color=discord.Color.red()
        )
        msg = await interaction.channel.send(embed=embed)

        async with state_lock:
            state["suslist_message"] = {
                "channel_id": msg.channel.id,
                "message_id": msg.id
            }
            save_state()

        await update_suslist_message(interaction.guild)
        await interaction.followup.send("✅ Live sus list setup successfully!", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Error setting up sus list: {e}", ephemeral=True)



@bot.tree.command(name="setup_quota", description="Setup the live tester quota leaderboard (Admin only)")
async def setup_quota(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        if not interaction.user.guild_permissions.administrator:
            await interaction.followup.send("❌ You need administrator permissions to use this command.", ephemeral=True)
            return

        embed = discord.Embed(
            title="📊 Tester Quota Leaderboard",
            description="Loading quota data...",
            color=discord.Color.blue()
        )
        msg = await interaction.channel.send(embed=embed)

        async with state_lock:
            state["quota_message"] = {
                "channel_id": msg.channel.id,
                "message_id": msg.id
            }
            save_state()

        await update_quota_message(interaction.guild)
        await interaction.followup.send("✅ Live quota leaderboard setup successfully!", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Error setting up quota list: {e}", ephemeral=True)


def build_command_list_embed() -> discord.Embed:
    embed = discord.Embed(
        title="📜 NL Tiers Command List",
        description=(
            "All available commands, grouped by required permission.\n\n"
            "👤 **Everyone**\n"
            "**/verify username** — Verify your Minecraft account.\n"
            "**/leave** — Leave your current waitlist.\n"
            "**/info user** — View a player's tier info and test history.\n"
            "**/testerguide** — Overzicht van hoe het testsysteem werkt.\n"
            "**/eval tier** — Vraag een evaluatie aan voor een hogere tier.\n\n"
            "🟢 **Tester & Staff**\n"
            "**/next** — Open ticket with next user in queue.\n"
            "**/skip** — Skip the first person in queue.\n"
            "**/start** — Start the testing queue for a waitlist.\n"
            "**/stop** — Stop testing for a waitlist queue.\n"
            "**/close tier** — Close ticket and assign a tier.\n"
            "**/forceclose** — Force close ticket without tier or cooldown.\n"
            "**/stats** — View your personal tester stats.\n"
            "**/setpeak username gamemode peak_tier** — Set a player's peak tier manually.\n"
            "**/profile speler** — Bekijk uitgebreid profiel, cooldowns en historie (Staff Only).\n"
            "**/add member** — Add a member to the current ticket.\n"
            "**/remove member** — Remove a member from current ticket.\n"
            "**/removefromqueue member** — Remove a user from queue.\n"
            "**/removefromwaitlist member** — Remove a user from waitlist.\n\n"
            "🛡️ **Moderator**\n"
            "**/restrict speler reden** — Restrict a player using predefined dropdown reasons.\n"
            "**/unrestrict speler** — Remove restriction from a player.\n"
            "**/forcetest tester testee gamemode** — Force open test ticket.\n"
            "**/removecooldown member gamemode** — Remove testing cooldown.\n"
            "**/forcewin match_id winner** — Force award tournament win.\n\n"
            "📋 **Regulator**\n"
            "**/forceeval tier** — Request forced evaluation.\n"
            "**/sus speler reden** — Markeer speler als verdacht.\n"
            "**/unsus speler** — Verwijder speler van verdachtenlijst.\n\n"
            "👑 **Admin**\n"
            "**/tierwipe speler** — Wis alle tiers en rollen van een speler (Admin Only).\n"
            "**/settier member gamemode tier** — Manually set player tier.\n"
            "**/removetier member gamemode** — Remove player tier.\n"
            "**/reverify member username** — Reverfiy with new IGN.\n"
            "**/unverify member** — Unverify a Discord member.\n"
            "**/forcestop** — Force close queue regardless of testers.\n"
            "**/sync** — Manually sync slash commands.\n"
            "**/synctiers** — Sync tier roles to database.\n"
            "**/cleanuproles** — Cleanup duplicate/grey tier roles.\n"
            "**/syncuuid** — Sync Minecraft usernames with UUIDs.\n"
            "**/setup** — Setup waitlist selection message.\n"
            "**/setup_leaderboard** — Setup live tester leaderboard.\n"
            "**/setup_quota** — Setup live tester quota leaderboard.\n"
            "**/setup_suslist** — Setup live suslist channel.\n"
            "**/setup_commandlist** — Setup/update command list in channel.\n\n"
            "🏆 **Tournament (Admin)**\n"
            "**/tournament create** — Create a new tournament.\n"
            "**/tournament open** — Open tournament registration.\n"
            "**/tournament checkin** — Start tournament check-in phase.\n"
            "**/tournament start** — Start tournament & generate bracket."
        ),
        color=0x2b2d31
    )
    embed.set_footer(text="Improper use of commands may result in disciplinary action • NL Tiers")
    return embed


async def update_commandlist_message(guild: discord.Guild):
    try:
        channel_id = 1536743700321935541
        channel = guild.get_channel(channel_id)
        if not channel:
            try:
                channel = await guild.fetch_channel(channel_id)
            except Exception:
                pass
        if not channel:
            return

        embed = build_command_list_embed()
        
        cmd_info = state.get("commandlist_message", {})
        if cmd_info and cmd_info.get("message_id"):
            try:
                msg = await channel.fetch_message(cmd_info["message_id"])
                await msg.edit(embed=embed)
                return
            except Exception:
                pass

        async for msg in channel.history(limit=20):
            if msg.author.id == bot.user.id and msg.embeds and "NL Tiers Command List" in (msg.embeds[0].title or ""):
                await msg.edit(embed=embed)
                async with state_lock:
                    state["commandlist_message"] = {"channel_id": channel.id, "message_id": msg.id}
                    save_state()
                return

        new_msg = await channel.send(embed=embed)
        async with state_lock:
            state["commandlist_message"] = {"channel_id": channel.id, "message_id": new_msg.id}
            save_state()
    except Exception as e:
        print(f"Error updating commandlist message: {e}")


@bot.tree.command(name="commandlist", description="Show all available commands grouped by role")
async def commandlist(interaction: discord.Interaction):
    embed = build_command_list_embed()
    await interaction.response.send_message(embed=embed, ephemeral=False)


@bot.tree.command(name="setup_commandlist", description="Setup/post the command list embed in the current channel (Admin only)")
async def setup_commandlist(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        if not interaction.user.guild_permissions.administrator:
            await interaction.followup.send("❌ You need administrator permissions to use this command.", ephemeral=True)
            return

        embed = build_command_list_embed()
        await interaction.channel.send(embed=embed)
        await update_commandlist_message(interaction.guild)
        await interaction.followup.send("✅ Command list embed updated successfully in channel 1536743700321935541!", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Error posting command list: {e}", ephemeral=True)




@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(username="Minecraft username", gamemode="Gamemode id e.g. vanilla", peak_tier="Peak tier e.g. LT4, HT5")
@bot.tree.command(name="setpeak", description="Set a player's peak tier manually")
async def setpeak(interaction: discord.Interaction, username: str, gamemode: str, peak_tier: str):
    await interaction.response.defer(ephemeral=True)
    try:
        tester_role = discord.utils.get(interaction.guild.roles, name=TESTER_ROLE_NAME)
        if not tester_role or tester_role not in interaction.user.roles:
            await interaction.followup.send("❌ You need the Tester role to use this command.", ephemeral=True)
            return

        gamemode = gamemode.lower().strip()
        peak_tier = peak_tier.upper().strip()

        if gamemode not in GAMEMODES:
            await interaction.followup.send(f"❌ Invalid gamemode. Use one of: {', '.join(GAMEMODES)}", ephemeral=True)
            return

        if peak_tier not in TIER_ROLES:
            await interaction.followup.send(f"❌ Invalid tier. Use one of: {', '.join(TIER_ROLES)}", ephemeral=True)
            return

        conn = db_manager.get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT uuid, username FROM updated_players WHERE username LIKE ? LIMIT 1", (f"%{username}%",))
        row = cursor.fetchone()
        
        if not row:
            conn.close()
            await interaction.followup.send(f"❌ Player '{username}' not found in database.", ephemeral=True)
            return

        uuid, full_username = row[0], row[1]
        
        player_data = await get_player_data(uuid)
        if player_data:
            player_data[f"{gamemode}_peak"] = peak_tier
            player_data['overall'] = calculate_overall_score(player_data)
            
            set_clause = ", ".join([f"{k} = ?" for k in player_data.keys() if k != 'uuid'])
            params = [player_data[k] for k in player_data.keys() if k != 'uuid'] + [uuid]
            
            with db_manager.db_lock:
                cursor.execute(f"UPDATE updated_players SET {set_clause} WHERE uuid = ?", params)
                conn.commit()
            conn.close()
            
            await update_leaderboard_message(interaction.guild)
            await interaction.followup.send(f"✅ Set **{full_username}**'s peak tier for **{gamemode_label(gamemode)}** to **{peak_tier}**!", ephemeral=True)
        else:
            conn.close()
            await interaction.followup.send(f"❌ Could not retrieve player data.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)




@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    speler="De speler (Discord member) waarvan je alle tiers wilt wissen",
    ign="De Minecraft gebruikersnaam (IGN) van de speler (handig als iemand de Discord is geleaved)"
)
@bot.tree.command(name="tierwipe", description="Wis alle tiers en rollen van een speler via Discord of IGN (Admin Only)")
async def tierwipe(
    interaction: discord.Interaction,
    speler: Optional[discord.Member] = None,
    ign: Optional[str] = None
):
    await interaction.response.defer(ephemeral=True)
    try:
        if not interaction.user.guild_permissions.administrator:
            await interaction.followup.send("❌ Dit commando is alleen beschikbaar voor Administrators.", ephemeral=True)
            return

        if not speler and not ign:
            await interaction.followup.send("❌ Geef ten minste een @speler of een Minecraft `ign` op.", ephemeral=True)
            return

        target_uuid = None
        target_username = None
        discord_member = speler

        conn = db_manager.get_db()
        cursor = conn.cursor()

        if speler:
            discord_id = str(speler.id)
            target_username = await get_verified_username(discord_id) or speler.display_name
            target_uuid = await get_verified_uuid(discord_id)
        elif ign:
            target_username = ign.strip()
            # Lookup in SQLite database
            cursor.execute("SELECT uuid, username FROM updated_players WHERE username LIKE ? LIMIT 1", (f"%{target_username}%",))
            row = cursor.fetchone()
            if row:
                target_uuid, target_username = row[0], row[1]
            else:
                # Fetch via Mojang API if not in DB
                mojang = get_minecraft_uuid(target_username)
                if mojang and "uuid" in mojang:
                    target_uuid = mojang["uuid"]
                    target_username = mojang.get("username", target_username)

        if not target_uuid:
            conn.close()
            await interaction.followup.send(f"❌ Kon geen speler of UUID vinden voor **{ign or speler}** in de database.", ephemeral=True)
            return

        # 1. If discord_member is present in server, remove tier roles
        if discord_member:
            roles_to_remove = [
                r for r in discord_member.roles
                if not r.is_default() and r.id != RESTRICTED_ROLE_ID and "member" not in r.name.lower()
            ]
            if roles_to_remove:
                try:
                    await discord_member.remove_roles(*roles_to_remove, reason=f"Tierwipe uitgevoerd door {interaction.user.display_name}")
                except Exception as e:
                    print(f"Error removing roles in /tierwipe: {e}")

        # 2. Reset database tiers in updated_players
        wipe_dict = {"username": target_username, "overall": 0}
        for gm in GAMEMODES:
            wipe_dict[gm] = ""
            wipe_dict[f"{gm}_peak"] = ""

        set_clause = ", ".join([f"{k} = ?" for k in wipe_dict.keys()])
        params = [wipe_dict[k] for k in wipe_dict.keys()] + [target_uuid]

        with db_manager.db_lock:
            cursor.execute(f"UPDATE updated_players SET {set_clause} WHERE uuid = ?", params)
            conn.commit()
        conn.close()

        # 3. Update Live Leaderboards
        await update_leaderboard_message(interaction.guild)

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"[{timestamp}] /tierwipe - Executed by: {interaction.user} | Target IGN: {target_username} | UUID: {target_uuid}")

        member_mention_str = f" ({discord_member.mention})" if discord_member else " (niet meer in Discord)"
        await interaction.followup.send(
            f"✅ Alle tiers en rollen van **{target_username}** (`{target_uuid}`){member_mention_str} zijn succesvol gewist!",
            ephemeral=True
        )

    except discord.Forbidden:
        await interaction.followup.send("❌ Geen rechten om rollen te beheren.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Error bij tierwipe: {e}", ephemeral=True)


@app_commands.describe(speler="De speler waarvan je het profiel wilt inzien (standaard jezelf)")
@bot.tree.command(name="profile", description="Bekijk het uitgebreide profiel van een speler (Staff Only)")
async def profile_cmd(interaction: discord.Interaction, speler: Optional[discord.Member] = None):
    await interaction.response.defer(ephemeral=True)
    try:
        # Check staff permission (Staff role 1498060823833346127, Tester role, or Administrator)
        staff_role_ids = [STAFF_ROLE_ID, VERIFIED_TESTER_ROLE_ID, 1498060823833346122]
        has_staff_access = any(
            role.id in staff_role_ids or role.name == TESTER_ROLE_NAME or role.permissions.administrator
            for role in interaction.user.roles
        )

        if not has_staff_access:
            await interaction.followup.send("❌ Dit commando is alleen beschikbaar voor Staff.", ephemeral=True)
            return

        target = speler or interaction.user

        # Get Minecraft info
        minecraft_username = await get_verified_username(str(target.id))
        uuid = await get_verified_uuid(str(target.id))

        is_verified = bool(minecraft_username)
        mc_username_display = minecraft_username if is_verified else "Niet Geverifieerd ❌"
        uuid_display = f"`{uuid}`" if uuid else "Onbekend"
        namemc_url = f"https://namemc.com/profile/{uuid}" if uuid else None

        # Discord Timestamps
        joined_at_str = f"<t:{int(target.joined_at.timestamp())}:R> (<t:{int(target.joined_at.timestamp())}:F>)" if target.joined_at else "Onbekend"
        created_at_str = f"<t:{int(target.created_at.timestamp())}:R>" if target.created_at else "Onbekend"

        # Database queries for rank, tiers, last test, cooldowns
        overall_score = 0
        leaderboard_rank = "Unranked"
        tier_list_str = "Geen active tiers"
        last_test_str = "Geen recente tests gevonden"
        cooldown_str = "Geen actieve cooldowns ✅"

        conn = db_manager.get_db()
        cursor = conn.cursor()

        if uuid:
            player_data = await get_player_data(uuid)
            if player_data:
                overall_score = calculate_overall_score(player_data)

                # Leaderboard Rank
                cursor.execute(
                    "SELECT COUNT(*) FROM updated_players WHERE overall > ? OR (overall = ? AND uuid < ?)",
                    (overall_score, overall_score, uuid)
                )
                rank_num = cursor.fetchone()[0] + 1
                leaderboard_rank = f"#{rank_num}"

                # Active Tiers
                active_tiers = []
                for gm in GAMEMODES:
                    t = player_data.get(gm)
                    if t:
                        active_tiers.append(f"**{gamemode_label(gm)}:** `{t}`")
                if active_tiers:
                    tier_list_str = " | ".join(active_tiers)

        # 1. Query Last Test Info
        try:
            cursor.execute(
                "SELECT tester_id, gamemode, created_at FROM tickets WHERE testee_id = ? OR testee_id = ? ORDER BY created_at DESC LIMIT 1",
                (str(target.id), str(uuid) if uuid else "")
            )
            t_row = cursor.fetchone()
            if t_row:
                t_tester_id, t_gamemode, t_created = t_row[0], t_row[1], t_row[2]
                tester_mention = f"<@{t_tester_id}>" if t_tester_id else "Onbekend"
                try:
                    t_dt = datetime.fromisoformat(t_created)
                    t_ts = int(t_dt.timestamp())
                    last_test_str = f"**{gamemode_label(t_gamemode)}** door {tester_mention} op <t:{t_ts}:F> (<t:{t_ts}:R>)"
                except Exception:
                    last_test_str = f"**{gamemode_label(t_gamemode)}** door {tester_mention}"
        except Exception as e:
            print(f"Error getting last test for {target}: {e}")

        # 2. Query Active Cooldowns
        try:
            cursor.execute("SELECT cooldowns FROM updated_cooldowns WHERE discord_id = ?", (str(target.id),))
            cd_row = cursor.fetchone()
            if cd_row and cd_row[0]:
                cooldown_data = json.loads(cd_row[0])
                now = datetime.now(timezone.utc)
                active_cds = []
                for gm, dt_iso in cooldown_data.items():
                    try:
                        tested_dt = datetime.fromisoformat(dt_iso)
                        if tested_dt.tzinfo is None:
                            tested_dt = tested_dt.replace(tzinfo=timezone.utc)
                        time_since = now - tested_dt
                        cooldown_period = timedelta(weeks=1)
                        if time_since < cooldown_period:
                            time_left = cooldown_period - time_since
                            d = time_left.days
                            h, rem = divmod(time_left.seconds, 3600)
                            m, _ = divmod(rem, 60)
                            parts = []
                            if d > 0: parts.append(f"{d}d")
                            if h > 0: parts.append(f"{h}h")
                            if m > 0 and d == 0: parts.append(f"{m}m")
                            left_str = " ".join(parts) if parts else "< 1m"
                            active_cds.append(f"**{gamemode_label(gm)}:** nog `{left_str}`")
                    except Exception:
                        pass
                if active_cds:
                    cooldown_str = "\n".join(active_cds)
        except Exception as e:
            print(f"Error checking cooldowns for {target}: {e}")

        conn.close()

        # Check restriction status
        restricted_role = interaction.guild.get_role(RESTRICTED_ROLE_ID)
        is_restricted = "Ja 🚫" if (restricted_role and restricted_role in target.roles) else "Nee ✅"

        # Build Staff Embed
        embed = discord.Embed(
            title=f"👤 Staff Profiler: {target.display_name}",
            color=QUEUE_EMBED_COLOR
        )

        avatar_url = target.display_avatar.url if target.display_avatar else target.default_avatar.url
        embed.set_author(name=f"{target.name} ({target.id})", icon_url=avatar_url)

        if minecraft_username:
            embed.set_thumbnail(url=f"https://mc-heads.net/body/{minecraft_username}")
        else:
            embed.set_thumbnail(url=avatar_url)

        mc_value = f"**IGN:** {mc_username_display}\n**UUID:** {uuid_display}"
        if namemc_url:
            mc_value += f"\n🔗 [NameMC Profiel]({namemc_url})"

        embed.add_field(name="🎮 Minecraft Account", value=mc_value, inline=False)
        embed.add_field(name="🏆 Leaderboard & Punten", value=f"**Positie:** {leaderboard_rank}\n**Overall Score:** {overall_score} pts", inline=True)
        embed.add_field(name="🛡️ Restrictie Status", value=is_restricted, inline=True)
        embed.add_field(name="📅 Discord Details", value=f"**Account Gemaakt:** {created_at_str}\n**Server Gejoined:** {joined_at_str}", inline=False)

        if tier_list_str != "Geen active tiers":
            embed.add_field(name="⚔️ Behaalde Tiers", value=tier_list_str, inline=False)

        embed.add_field(name="🎟️ Laatste Test", value=last_test_str, inline=False)
        embed.add_field(name="⏱️ Actieve Cooldowns", value=cooldown_str, inline=False)

        embed.set_footer(text=f"NL Tiers Staff Intelligence | ID: {target.id}")

        await interaction.followup.send(embed=embed, ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"❌ Error bij ophalen van profiel: {e}", ephemeral=True)


# Get token from environment variable
token = os.getenv("DISCORD_TOKEN")

if not token:
    print("❌ Error: DISCORD_TOKEN environment variable not set.")
    print("Please create a .env file with DISCORD_TOKEN=your_token_here")
else:
    bot.run(token)
