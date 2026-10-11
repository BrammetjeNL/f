import discord
from discord.ext import commands
from typing import Optional
from tournament_models import Tournament, Participant, Match
from tournament_services import TournamentService
import firebase_admin
from firebase_admin import firestore

db = firestore.client()

class TournamentRegistrationView(discord.ui.View):
    def __init__(self, tournament_id: str):
        super().__init__(timeout=None)
        self.tournament_id = tournament_id
        
        # Set dynamic custom IDs for persistence
        self.join_btn.custom_id = f"t_join:{tournament_id}"
        self.leave_btn.custom_id = f"t_leave:{tournament_id}"

    async def update_registration_embed(self, interaction: discord.Interaction):
        t_id = self.tournament_id
        tournament = await TournamentService.get_tournament(t_id)
        if not tournament:
            return
            
        participants = await TournamentService.get_participants(t_id)
        
        participant_list_str = ""
        if participants:
            participant_list_str = "\n".join([f"• {p.minecraft_username} (<@{p.player_id}>)" for p in participants])
        else:
            participant_list_str = "Geen"
            
        embed = discord.Embed(
            title=f"🏆 Inschrijving geopend: {tournament.name}",
            description=(
                f"**Gamemode:** {tournament.gamemode.upper()}\n"
                f"**Format:** {tournament.format.replace('_', ' ')}\n"
                f"**Max Players:** {tournament.max_players}\n"
                f"**Required Role:** {tournament.required_role}\n\n"
                f"**Deelnemers ({len(participants)}/{tournament.max_players}):**\n"
                f"{participant_list_str}\n\n"
                f"Klik op de knop hieronder om mee te doen!"
            ),
            color=discord.Color.green()
        )
        await interaction.message.edit(embed=embed)

    @discord.ui.button(label="Join Tournament", style=discord.ButtonStyle.success, emoji="🏆")
    async def join_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        t_id = self.tournament_id
        user_id = str(interaction.user.id)
        
        tournament = await TournamentService.get_tournament(t_id)
        if not tournament or tournament.status != "REGISTRATION_OPEN":
            await interaction.followup.send("❌ Registration for this tournament is currently closed.", ephemeral=True)
            return

        # Verification check
        # Get Minecraft verify info
        docs = list(db.collection('verify').where('discord', '==', user_id).limit(1).stream())
        if not docs:
            await interaction.followup.send("❌ You must verify your Minecraft account first using `/verify`.", ephemeral=True)
            return
            
        verify_data = docs[0].to_dict()
        mc_username = verify_data.get('username')
        mc_uuid = verify_data.get('uuid')

        # Check if already registered
        participants = await TournamentService.get_participants(t_id)
        if any(p.player_id == user_id for p in participants):
            await interaction.followup.send("❌ You are already registered for this tournament.", ephemeral=True)
            return

        if len(participants) >= tournament.max_players:
            await interaction.followup.send("❌ This tournament is full.", ephemeral=True)
            return

        # Add participant
        participant = Participant(
            tournament_id=t_id,
            player_id=user_id,
            minecraft_username=mc_username,
            minecraft_uuid=mc_uuid,
            seed=0,
            status="REGISTERED"
        )
        await TournamentService.add_participant(participant)
        await interaction.followup.send(f"✅ Successfully registered for **{tournament.name}**!", ephemeral=True)
        
        # Update embed
        await self.update_registration_embed(interaction)

    @discord.ui.button(label="Leave Tournament", style=discord.ButtonStyle.danger, emoji="🚪")
    async def leave_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        t_id = self.tournament_id
        user_id = str(interaction.user.id)
        
        tournament = await TournamentService.get_tournament(t_id)
        if not tournament or tournament.status != "REGISTRATION_OPEN":
            await interaction.followup.send("❌ You cannot leave the tournament at this stage.", ephemeral=True)
            return

        db.collection('tournament_participants').document(f"{t_id}:{user_id}").delete()
        await interaction.followup.send("✅ You have left the tournament.", ephemeral=True)
        
        # Update embed
        await self.update_registration_embed(interaction)




class MatchTicketView(discord.ui.View):
    def __init__(self, match_id: str):
        super().__init__(timeout=None)
        self.match_id = match_id
        self.ready_btn.custom_id = f"m_ready:{match_id}"
        self.report_btn.custom_id = f"m_report:{match_id}"

    @discord.ui.button(label="Ready", style=discord.ButtonStyle.success, emoji="✅")
    async def ready_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        match = await TournamentService.get_match(self.match_id)
        if not match or match.status != "WAITING_READY":
            return
            
        user_id = str(interaction.user.id)
        if user_id == match.player1_id:
            match.player1_vote = "READY"
        elif user_id == match.player2_id:
            match.player2_vote = "READY"
        else:
            return

        if match.player1_vote == "READY" and match.player2_vote == "READY":
            match.status = "ACTIVE"
            match.player1_vote = None
            match.player2_vote = None
            await interaction.channel.send("⚔️ **Both players are ready! The match is now active. Fight!**")
            # Enable reporting view
            
        await TournamentService.update_match(match)

    @discord.ui.button(label="Report Result", style=discord.ButtonStyle.primary, emoji="📣")
    async def report_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        match = await TournamentService.get_match(self.match_id)
        if not match or match.status != "ACTIVE":
            await interaction.response.send_message("❌ Match is not active yet or has already finished.", ephemeral=True)
            return

        user_id = str(interaction.user.id)
        if user_id != match.player1_id and user_id != match.player2_id:
            await interaction.response.send_message("❌ You are not a participant in this match.", ephemeral=True)
            return

        view = MatchReportingView(self.match_id)
        await interaction.response.send_message("Who won the match?", view=view, ephemeral=True)


class MatchReportingView(discord.ui.View):
    def __init__(self, match_id: str):
        super().__init__(timeout=120)
        self.match_id = match_id

    @discord.ui.button(label="I Won", style=discord.ButtonStyle.success)
    async def i_won(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        match = await TournamentService.get_match(self.match_id)
        if not match or match.status != "ACTIVE":
            return

        user_id = str(interaction.user.id)
        if user_id == match.player1_id:
            match.player1_vote = user_id
        elif user_id == match.player2_id:
            match.player2_vote = user_id
        else:
            return

        await self.process_votes(interaction, match)

    @discord.ui.button(label="Opponent Won", style=discord.ButtonStyle.danger)
    async def opponent_won(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        match = await TournamentService.get_match(self.match_id)
        if not match or match.status != "ACTIVE":
            return

        user_id = str(interaction.user.id)
        if user_id == match.player1_id:
            match.player1_vote = match.player2_id
        elif user_id == match.player2_id:
            match.player2_vote = match.player1_id
        else:
            return

        await self.process_votes(interaction, match)

    async def process_votes(self, interaction: discord.Interaction, match: Match):
        await TournamentService.update_match(match)
        
        # Check if both have voted
        if match.player1_vote and match.player2_vote:
            if match.player1_vote == match.player2_vote:
                # Consensus
                match.status = "FINISHED"
                match.winner_id = match.player1_vote
                await TournamentService.update_match(match)
                
                winner_username = match.player1_username if match.winner_id == match.player1_id else match.player2_username
                await interaction.channel.send(f"🏆 **Match Finished! Winner: {winner_username}!** Ticket will close shortly.")
                
                # Advance bracket
                await TournamentService.advance_tournament_bracket(match.tournament_id)
            else:
                # Conflict (Dispute)
                match.status = "DISPUTED"
                await TournamentService.update_match(match)
                await interaction.channel.send("⚠️ **Result conflict! Match marked as DISPUTED. Staff review required.**")
                # Request evidence
                view = DisputeEvidenceView(self.match_id)
                await interaction.channel.send("Please upload your match evidence (Screenshots/YouTube links) using the button below:", view=view)


class DisputeEvidenceView(discord.ui.View):
    def __init__(self, match_id: str):
        super().__init__(timeout=None)
        self.match_id = match_id
        self.upload_btn.custom_id = f"m_dispute:{match_id}"

    @discord.ui.button(label="Submit Evidence", style=discord.ButtonStyle.secondary, emoji="📁")
    async def upload_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = DisputeEvidenceModal(self.match_id)
        await interaction.response.send_modal(modal)


class DisputeEvidenceModal(discord.ui.Modal, title="Submit Dispute Evidence"):
    evidence_url = discord.ui.TextInput(
        label="Evidence URL (YouTube, Medal, or Imgur link)",
        placeholder="https://youtu.be/...",
        required=True
    )

    def __init__(self, match_id: str):
        super().__init__()
        self.match_id = match_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        match = await TournamentService.get_match(self.match_id)
        if not match:
            return

        match.evidence_urls.append(self.evidence_url.value)
        await TournamentService.update_match(match)
        await interaction.followup.send("✅ Evidence submitted successfully!", ephemeral=True)
        await interaction.channel.send(f"📁 **New evidence submitted by {interaction.user.display_name}:** {self.evidence_url.value}")
