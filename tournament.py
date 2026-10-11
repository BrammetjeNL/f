import discord
from discord import app_commands
from discord.ext import commands
import uuid
from datetime import datetime, timezone
from tournament_models import Tournament, Participant, Match
from tournament_services import TournamentService
from tournament_views import TournamentRegistrationView, MatchTicketView
import firebase_admin
from firebase_admin import firestore

db = firestore.client()

class TournamentCheckInView(discord.ui.View):
    def __init__(self, tournament_id: str):
        super().__init__(timeout=None)
        self.tournament_id = tournament_id
        self.check_in_btn.custom_id = f"t_checkin:{tournament_id}"

    @discord.ui.button(label="Check In", style=discord.ButtonStyle.success, emoji="✅")
    async def check_in_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        t_id = self.tournament_id
        user_id = str(interaction.user.id)
        
        tournament = await TournamentService.get_tournament(t_id)
        if not tournament or tournament.status != "CHECK_IN":
            await interaction.followup.send("❌ Check-in is not active.", ephemeral=True)
            return

        doc_ref = db.collection('tournament_participants').document(f"{t_id}:{user_id}")
        doc = doc_ref.get()
        if not doc.exists:
            await interaction.followup.send("❌ You are not registered for this tournament.", ephemeral=True)
            return

        doc_ref.update({'status': 'CHECKED_IN'})
        await interaction.followup.send("✅ Successfully checked in!", ephemeral=True)


class TournamentGroup(app_commands.Group, name="tournament"):
    @app_commands.command(name="create", description="Create a new tournament (Admin only)")
    @app_commands.describe(
        name="Tournament Name",
        gamemode="Gamemode key (e.g. sword)",
        format="Bracket format",
        max_players="Maximum participants",
        required_role="Role name required to join",
        min_rank="Minimum rank (optional)",
        max_rank="Maximum rank (optional)",
        forfeit_time="Forfeit time in seconds (default 300)"
    )
    @app_commands.choices(format=[
        app_commands.Choice(name="Single Elimination", value="SINGLE_ELIMINATION"),
        app_commands.Choice(name="Double Elimination", value="DOUBLE_ELIMINATION")
    ])
    async def create(
        self,
        interaction: discord.Interaction,
        name: str,
        gamemode: str,
        format: str,
        max_players: int,
        required_role: str,
        min_rank: str = None,
        max_rank: str = None,
        forfeit_time: int = 300
    ):
        await interaction.response.defer(ephemeral=True)
        
        if not interaction.user.guild_permissions.administrator:
            await interaction.followup.send("❌ Only Administrators can use this command.", ephemeral=True)
            return

        t_id = str(uuid.uuid4())[:8]
        tournament = Tournament(
            id=t_id,
            name=name,
            gamemode=gamemode,
            status="DRAFT",
            format=format,
            max_players=max_players,
            required_role=required_role,
            minimum_rank=min_rank,
            maximum_rank=max_rank,
            forfeit_time=forfeit_time,
            channel_id=interaction.channel.id
        )
        
        await TournamentService.create_tournament(tournament)
        
        embed = discord.Embed(
            title=f"🏆 Tournament: {name}",
            description=(
                f"**Tournament ID:** `{t_id}`\n\n"
                f"**Gamemode:** {gamemode.upper()}\n"
                f"**Format:** {format.replace('_', ' ')}\n"
                f"**Max Players:** {max_players}\n"
                f"**Required Role:** {required_role}\n"
                f"**Status:** DRAFT\n\n"
                f"Use `/tournament open tournament_id: {t_id}` to open registration!"
            ),
            color=discord.Color.blue()
        )

        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="open", description="Open registration for a tournament (Admin only)")
    @app_commands.describe(tournament_id="The ID of the tournament")
    async def open_registration(self, interaction: discord.Interaction, tournament_id: str):
        await interaction.response.defer(ephemeral=True)
        if not interaction.user.guild_permissions.administrator:
            return
            
        tournament = await TournamentService.get_tournament(tournament_id)
        if not tournament:
            await interaction.followup.send("❌ Tournament not found.", ephemeral=True)
            return

        tournament.status = "REGISTRATION_OPEN"
        db.collection('tournaments').document(tournament_id).update({'status': 'REGISTRATION_OPEN'})

        embed = discord.Embed(
            title=f"🏆 Inschrijving geopend: {tournament.name}",
            description=(
                f"**Gamemode:** {tournament.gamemode.upper()}\n"
                f"**Format:** {tournament.format.replace('_', ' ')}\n"
                f"**Max Players:** {tournament.max_players}\n"
                f"**Required Role:** {tournament.required_role}\n\n"
                f"**Deelnemers (0/{tournament.max_players}):**\n"
                f"Geen\n\n"
                f"Klik op de knop hieronder om mee te doen!"
            ),
            color=discord.Color.green()
        )
        view = TournamentRegistrationView(tournament_id)

        msg = await interaction.channel.send(embed=embed, view=view)
        await interaction.followup.send(f"✅ Registration opened! Message sent to channel: {msg.jump_url}", ephemeral=True)

    @app_commands.command(name="checkin", description="Start check-in phase for a tournament (Admin only)")
    @app_commands.describe(tournament_id="The ID of the tournament")
    async def checkin(self, interaction: discord.Interaction, tournament_id: str):
        await interaction.response.defer(ephemeral=True)
        if not interaction.user.guild_permissions.administrator:
            return

        tournament = await TournamentService.get_tournament(tournament_id)
        if not tournament:
            await interaction.followup.send("❌ Tournament not found.", ephemeral=True)
            return

        tournament.status = "CHECK_IN"
        db.collection('tournaments').document(tournament_id).update({'status': 'CHECK_IN'})

        embed = discord.Embed(
            title=f"⏰ Check-In Active: {tournament.name}",
            description="All registered participants must click the **Check In** button below to participate in the bracket!",
            color=discord.Color.orange()
        )
        view = TournamentCheckInView(tournament_id)
        await interaction.channel.send(embed=embed, view=view)
        await interaction.followup.send("✅ Check-in phase started!", ephemeral=True)

    @app_commands.command(name="start", description="Start the tournament and generate bracket (Admin only)")
    @app_commands.describe(tournament_id="The ID of the tournament")
    async def start(self, interaction: discord.Interaction, tournament_id: str):
        await interaction.response.defer(ephemeral=True)
        if not interaction.user.guild_permissions.administrator:
            return

        try:
            matches = await TournamentService.generate_bracket(tournament_id)
            await interaction.followup.send(f"✅ Tournament started! Bracket generated with **{len(matches)}** matches.", ephemeral=True)
            
            # Send notification to channel
            await interaction.channel.send(f"🚀 **The tournament bracket has been generated and matches are active! Check your ticket channels!**")
            
            # Create match tickets for active matches
            for match in matches:
                if match.status == "WAITING_READY":
                    await self.create_match_ticket(interaction.guild, match)
        except Exception as e:
            await interaction.followup.send(f"❌ Error starting tournament: {e}", ephemeral=True)

    async def create_match_ticket(self, guild: discord.Guild, match: Match):
        # Create channel for match
        # Find category
        category = guild.categories[0] # Fallback, or configure category ID
        
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }
        
        p1 = guild.get_member(int(match.player1_id)) if match.player1_id else None
        p2 = guild.get_member(int(match.player2_id)) if match.player2_id else None
        
        if p1:
            overwrites[p1] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
        if p2:
            overwrites[p2] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        channel_name = f"match-r{match.round}-m{match.id.split(':')[-1]}"
        channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites
        )
        
        match.ticket_channel_id = channel.id
        await TournamentService.update_match(match)
        
        embed = discord.Embed(
            title=f"⚔️ Match Ticket: Round {match.round}",
            description=(
                f"**Player 1:** {p1.mention if p1 else 'Unknown'} ({match.player1_username})\n"
                f"**Player 2:** {p2.mention if p2 else 'Unknown'} ({match.player2_username})\n\n"
                f"Please click **Ready** when you are ready to fight!"
            ),
            color=discord.Color.red()
        )
        view = MatchTicketView(match.id)
        await channel.send(content=f"{p1.mention if p1 else ''} {p2.mention if p2 else ''}", embed=embed, view=view)

    @app_commands.command(name="forcewin", description="Forcefully award match win to a player (Admin/Staff only)")
    @app_commands.describe(match_id="The ID of the match", winner="The winning member")
    async def forcewin(self, interaction: discord.Interaction, match_id: str, winner: discord.Member):
        await interaction.response.defer(ephemeral=True)
        if not interaction.user.guild_permissions.manage_messages:
            return

        match = await TournamentService.get_match(match_id)
        if not match:
            await interaction.followup.send("❌ Match not found.", ephemeral=True)
            return

        match.status = "FINISHED"
        match.winner_id = str(winner.id)
        await TournamentService.update_match(match)
        
        await interaction.followup.send(f"✅ Match win successfully forced for {winner.display_name}!", ephemeral=True)
        
        # Notify ticket channel if exists
        if match.ticket_channel_id:
            channel = interaction.guild.get_channel(match.ticket_channel_id)
            if channel:
                await channel.send(f"🏆 **Staff Force Win: Winner set to {winner.mention}!** Ticket will close shortly.")
                
        # Advance bracket
        await TournamentService.advance_tournament_bracket(match.tournament_id)
