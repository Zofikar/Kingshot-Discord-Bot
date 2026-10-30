"""Council mandates, queued proposals, persistent private-feedback voting UI."""
import asyncio
import json
import logging
import time
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.permission_handler import PermissionManager
from .. import storage
from ..council import MandateStore
from ..settings import SettingsStore
from ..vote_storage import VoteStore
from ..voting import tally

logger = logging.getLogger("extension")
ANSWERS = [app_commands.Choice(name="Yes", value="YES"),
           app_commands.Choice(name="No", value="NO"),
           app_commands.Choice(name="Abstained", value="ABSTAINED")]


def utc(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def safe(text):
    return discord.utils.escape_mentions(discord.utils.escape_markdown(str(text)))


class VoteView(discord.ui.View):
    def __init__(self, cog, vote, closed=False):
        super().__init__(timeout=None)
        closed = closed or vote["status"] != "OPEN" or time.time() >= vote["closes_at"]
        for label, answer, style in [
            ("Yes", "YES", discord.ButtonStyle.success),
            ("No", "NO", discord.ButtonStyle.danger),
            ("Abstain", "ABSTAINED", discord.ButtonStyle.secondary),
            ("Participation", None, discord.ButtonStyle.primary),
        ]:
            button = discord.ui.Button(label=label, style=style,
                custom_id=f"council:{vote['id']}:{answer or 'participation'}",
                disabled=closed and answer is not None)

            async def callback(interaction, selected=answer):
                if selected is None:
                    await cog.show_participation(interaction, vote["id"])
                else:
                    await cog.cast_ballot(interaction, vote["id"], selected)

            button.callback = callback
            self.add_item(button)


class ConclusionView(discord.ui.View):
    def __init__(self, cog, vote_id, actor):
        super().__init__(timeout=180)
        self.cog, self.vote_id, self.actor = cog, vote_id, actor

    @discord.ui.button(label="Confirm conclusion and publish", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            if interaction.user.id != self.actor:
                raise ValueError("Only the administrator who requested this preview can confirm it.")
            self.cog.require_admin(interaction)
            async with self.cog.lock:
                self.cog.store.conclude(self.vote_id, interaction.user.id)
                await self.cog.publish_and_advance(self.cog.store.get(self.vote_id), interaction.guild)
            self.stop()
            await interaction.followup.send("Vote concluded and result published.", ephemeral=True)
        except (ValueError, discord.HTTPException) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)


class Council(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.settings = getattr(bot, "extension_settings", None) or SettingsStore()
        self.store = VoteStore()
        self.mandates = MandateStore()
        self.lock = asyncio.Lock()
        self.restored = False
        self.restoration_complete = asyncio.Event()

    async def cog_load(self):
        self.maintenance.start()

    def cog_unload(self):
        self.maintenance.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        if self.restored:
            return
        for guild in self.bot.guilds:
            if not guild.chunked:
                await guild.chunk()
            for vote in self.store.list_votes(guild.id):
                if vote["message_id"]:
                    self.bot.add_view(VoteView(self, vote), message_id=vote["message_id"])
            await self.reconcile(guild)
        self.restored = True
        self.restoration_complete.set()

    @commands.Cog.listener()
    async def on_member_join(self, member):
        await self.reconcile(member.guild)

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        await self.reconcile(member.guild)

    def require_admin(self, interaction):
        if not interaction.guild or not PermissionManager.is_admin(interaction.user.id)[0]:
            raise ValueError("You don't have admin permission.")

    def vote_for(self, interaction, vote_id):
        vote = self.store.get(vote_id)
        if vote["guild_id"] != interaction.guild_id:
            raise ValueError("This vote belongs to another server.")
        return vote

    def main_player(self, member_id):
        fid = storage.main_fid_for_discord(member_id)
        row = storage.user_row(fid) if fid is not None else None
        if row is None:
            raise ValueError("A verified MAIN FID is required.")
        return dict(row)

    def electorate(self, guild_id):
        pending = self.store.pending(guild_id)
        return json.loads(pending["electorate"]) if pending else storage.latest_nap_snapshot()

    async def reconcile(self, guild):
        # Missing cached members are not proof of departure until chunking has
        # completed. This guard covers every caller, not just startup/maintenance.
        if not guild.chunked:
            await guild.chunk()
        if not guild.chunked:
            raise ValueError("Guild member cache is incomplete; mandates were not reconciled.")
        eligible = {row["aid"] for row in self.electorate(guild.id)}
        present = {member.id for member in guild.members}
        leaders = {}
        for fid, member_id, _ in storage.linked_main_accounts():
            if member_id not in present:
                continue
            player = storage.user_row(fid)
            if player["rank"] == 5 and player["alliance"] is not None:
                leaders[int(player["alliance"])] = member_id
        self.mandates.reconcile(guild.id, eligible, present, leaders)
        active = set(self.mandates.active(guild.id).values())
        role_id = self.settings.get_int("role_diplomat_id")
        role = guild.get_role(role_id) if role_id else None
        if role:
            for member in guild.members:
                try:
                    if member.id in active and role not in member.roles:
                        await member.add_roles(role, reason="Council mandate reconciliation")
                    elif member.id not in active and role in member.roles:
                        await member.remove_roles(role, reason="Council mandate reconciliation")
                except discord.HTTPException:
                    logger.exception("Could not reconcile diplomat role for %s", member.id)

    async def attribution(self, vote, guild):
        member_id = vote["submitter_discord"]
        if member_id and guild.get_member(member_id):
            return f"<@{member_id}>"
        return f"[{safe(vote['submitter_tag'] or '?')}] {safe(vote['submitter_name'] or 'Unknown')} ({vote['submitter_fid']})"

    async def vote_embed(self, vote, guild):
        deadline = int(vote["closes_at"])
        status = vote["status"]
        if status == "OPEN" and time.time() >= deadline:
            status = "AWAITS_APPROVAL"
        return discord.Embed(title=f"Council vote #{vote['id']}",
            description=f"### {safe(vote['title'])}\nSubmitted by: {await self.attribution(vote, guild)}\n"
                        f"Status: **{status}**\nVoting closes: <t:{deadline}:F> (<t:{deadline}:R>)\n"
                        "Each alliance has one ballot. Use Participation to view current ballots.")

    def participation_embed(self, vote_id):
        groups = self.store.participation(vote_id)
        total = sum(map(len, groups.values()))
        cast = total - len(groups["DID_NOT_VOTE"])
        embed = discord.Embed(title=f"Vote #{vote_id} participation",
                              description=f"{cast}/{total} alliances ({100 * cast / total:.1f}%)")
        for answer, label in [("YES", "Yes"), ("NO", "No"),
                              ("ABSTAINED", "Abstained"), ("DID_NOT_VOTE", "Did not vote")]:
            embed.add_field(name=f"{label}: {len(groups[answer])}",
                            value=safe(" ".join(groups[answer]))[:1024] or "—", inline=False)
        return embed

    async def show_participation(self, interaction, vote_id):
        self.vote_for(interaction, vote_id)
        await interaction.response.send_message(embed=self.participation_embed(vote_id), ephemeral=True)

    async def cast_ballot(self, interaction, vote_id, answer):
        await interaction.response.defer(ephemeral=True)
        try:
            async with self.lock:
                vote = self.vote_for(interaction, vote_id)
                await self.reconcile(interaction.guild)
                aid = next((aid for aid, member in self.mandates.active(interaction.guild_id).items()
                            if member == interaction.user.id), None)
                if aid is None:
                    raise ValueError("Only the alliance's active diplomat may cast its ballot.")
                fid = storage.main_fid_for_discord(interaction.user.id)
                # Explicit R5/admin appointment permits an unverified server member.
                # actor is always the actual Discord representative; 0 denotes no linked FID.
                fid = fid if fid is not None else 0
                previous = [r for r in self.store.audit(vote_id) if r["alliance_id"] == aid]
                recorded = time.time()
                self.store.cast(vote_id, aid, answer, fid, interaction.user.id, now=recorded)
                tag = next(r["abbr"] for r in json.loads(vote["electorate"]) if r["aid"] == aid)
                text = (f"Your alliance's vote has been recorded.\nVote: {safe(vote['title'])}\n"
                        f"Alliance: {safe(tag)}\nAnswer: {answer}\n"
                        f"Representative: {interaction.user.mention}"
                        + (f" ({fid})" if fid else " (no linked FID)")
                        + f"\nRecorded at: {utc(recorded)}")
                if previous:
                    text += f"\nPrevious answer: {previous[-1]['answer']}"
                await interaction.followup.send(text, ephemeral=True)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)

    async def channel(self, channel_id):
        if not channel_id:
            raise ValueError("Configure the required vote channel with /admin_setting first.")
        return self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)

    async def start_next(self, guild):
        channel = await self.channel(self.settings.get_int("vote_channel_id"))
        if channel.guild.id != guild.id:
            raise ValueError("The vote channel must belong to this server.")
        snapshot = storage.latest_nap_snapshot()
        if not snapshot:
            raise ValueError("No posted NAP snapshot is available; post NAP first.")
        vote_id = self.store.open_next(guild.id, snapshot)
        self.store.update_delivery(vote_id, channel_id=channel.id)
        await self.reconcile(guild)
        await self.ensure_open_message(self.store.get(vote_id), guild)

    async def ensure_open_message(self, vote, guild):
        if vote["message_id"]:
            return
        channel = await self.channel(vote["channel_id"] or self.settings.get_int("vote_channel_id"))
        message = await channel.send(embed=await self.vote_embed(vote, guild), view=VoteView(self, vote),
                                     allowed_mentions=discord.AllowedMentions.none())
        self.store.update_delivery(vote["id"], channel_id=channel.id, message_id=message.id)

    async def result_text(self, vote, guild):
        groups = self.store.participation(vote["id"])
        lines = ["## Vote result", f"### {safe(vote['title'])}",
                 f"Submitted by: {await self.attribution(vote, guild)}", ""]
        for answer, label in [("YES", "voted yes"), ("NO", "voted no"),
                              ("ABSTAINED", "abstained"), ("DID_NOT_VOTE", "did not vote")]:
            lines.append(f"{len(groups[answer])} {label} - {safe(' '.join(groups[answer])) or '—'}")
        outcome = vote["status"].lower()
        if outcome == "voided":
            outcome += " due to low participation"
        role_id = self.settings.get_int("role_council_id")
        lines += ["", f"Therefore vote {outcome}" + (f" <@&{role_id}>" if role_id else "")]
        manual = [r for r in self.store.audit(vote["id"]) if r["manual"]]
        if manual:
            lines += ["", "### Manual vote adjustments"]
            tags = {r["aid"]: r["abbr"] for r in json.loads(vote["electorate"])}
            for row in manual:
                player = storage.user_row(row["fid"])
                name = player["nickname"] if player else "Unknown"
                lines.append(f"- {safe(tags[row['alliance_id']])} — {row['answer']} — "
                             f"{safe(name)} ({row['fid']}) — entered by <@{row['actor']}> at "
                             f"{utc(row['recorded_at'])} — {safe(row['reason'])}"
                             + (f" (replaces {row['previous_answer']})" if row["previous_answer"] else ""))
        return "\n".join(lines)

    async def publish_and_advance(self, vote, guild):
        if not vote["result_message_id"]:
            channel_id = (self.settings.get_int("vote_result_annoucement_channel") or
                          self.settings.get_int("vote_result_announcement_channel"))
            channel = await self.channel(channel_id)
            if channel.guild.id != guild.id:
                raise ValueError("The result channel must belong to this server.")
            text = await self.result_text(vote, guild)
            role_id = self.settings.get_int("role_council_id")
            mentions = discord.AllowedMentions(everyone=False, users=False,
                roles=[discord.Object(id=role_id)] if role_id else False)
            if len(text) <= 2000:
                message = await channel.send(text, allowed_mentions=mentions)
            else:
                import io
                message = await channel.send(text.split("### Manual vote adjustments")[0][:1900],
                    file=discord.File(io.BytesIO(text.encode("utf-8")), filename=f"vote-{vote['id']}-result.txt"),
                    allowed_mentions=mentions)
            self.store.update_delivery(vote["id"], result_message_id=message.id)
        await self.refresh_message(self.store.get(vote["id"]), guild)
        if not vote["advanced"]:
            if not self.store.pending(guild.id) and self.store.queue(guild.id):
                await self.start_next(guild)
            self.store.update_delivery(vote["id"], advanced=1)
        await self.reconcile(guild)

    async def refresh_message(self, vote, guild):
        if not vote["message_id"]:
            return
        try:
            channel = await self.channel(vote["channel_id"])
            message = await channel.fetch_message(vote["message_id"])
            await message.edit(embed=await self.vote_embed(vote, guild), view=VoteView(self, vote))
        except discord.NotFound:
            logger.warning("Vote message %s was deleted", vote["message_id"])

    @tasks.loop(seconds=30)
    async def maintenance(self):
        await self.bot.wait_until_ready()
        # Bot readiness precedes completion of our on_ready listener. Wait outside
        # the command lock so restoration cannot deadlock against maintenance.
        await self.restoration_complete.wait()
        async with self.lock:
            self.store.expire()
            for guild in self.bot.guilds:
                try:
                    await self.reconcile(guild)
                    pending = self.store.pending(guild.id)
                    if pending:
                        await self.ensure_open_message(pending, guild)
                        if pending["status"] == "AWAITS_APPROVAL" and not pending["notified"]:
                            await self.refresh_message(self.store.get(pending["id"]), guild)
                            channel_id = self.settings.get_int("vote_approval_notification_channel_id")
                            if channel_id:
                                channel = await self.channel(channel_id)
                                role_id = self.settings.get_int("vote_approval_notification_role_id")
                                await channel.send((f"<@&{role_id}> " if role_id else "") +
                                    f"Vote #{pending['id']} awaits approval. Use /vote_conclude after manual adjustments.",
                                    allowed_mentions=discord.AllowedMentions(everyone=False, users=False,
                                        roles=[discord.Object(id=role_id)] if role_id else False))
                            self.store.update_delivery(pending["id"], notified=1)
                    for vote in reversed(self.store.list_votes(guild.id)):
                        if vote["status"] in ("PASSED", "FAILED", "VOIDED") and not vote["advanced"]:
                            await self.publish_and_advance(vote, guild)
                except (ValueError, discord.HTTPException):
                    logger.exception("Council maintenance failed for guild %s", guild.id)

    @app_commands.command(name="diplomat_assign", description="Appoint a Discord member as an alliance diplomat.")
    @app_commands.guild_only()
    async def diplomat_assign(self, interaction: discord.Interaction, alliance_id: int, member: discord.Member):
        await interaction.response.defer(ephemeral=True)
        try:
            async with self.lock:
                await self.reconcile(interaction.guild)
                if alliance_id not in {r["aid"] for r in self.electorate(interaction.guild_id)}:
                    raise ValueError("This alliance is not currently eligible.")
                admin = PermissionManager.is_admin(interaction.user.id)[0]
                current = self.mandates.active(interaction.guild_id).get(alliance_id)
                player = None if admin else self.main_player(interaction.user.id)
                leader = player and player["rank"] == 5 and str(player["alliance"]) == str(alliance_id)
                if not admin and not leader:
                    if current != interaction.user.id:
                        raise ValueError("Only the current diplomat, alliance R5, or admin can assign.")
                    recipient = self.main_player(member.id)
                    if str(recipient["alliance"]) != str(alliance_id):
                        raise ValueError("A diplomat may only appoint a verified member of the represented alliance.")
                self.mandates.assign(interaction.guild_id, alliance_id, member.id,
                                     interaction.user.id, "admin" if admin else "leader" if leader else "handover")
                await self.reconcile(interaction.guild)
            await interaction.followup.send(f"{member.mention} now represents alliance {alliance_id}.", ephemeral=True)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)

    @app_commands.command(name="diplomat_claim", description="R5: override your seat; R4: claim a vacant leader-absent seat.")
    @app_commands.guild_only()
    async def diplomat_claim(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            async with self.lock:
                await self.reconcile(interaction.guild)
                player = self.main_player(interaction.user.id)
                aid = int(player["alliance"] or 0)
                if aid not in {r["aid"] for r in self.electorate(interaction.guild_id)}:
                    raise ValueError("Your MAIN alliance is not eligible.")
                if player["rank"] not in (4, 5):
                    raise ValueError("Only a verified R4 or R5 may claim.")
                if player["rank"] == 4 and aid in self.mandates.assignments(interaction.guild_id):
                    raise ValueError("This seat is occupied; ask the diplomat or R5 to assign you.")
                self.mandates.assign(interaction.guild_id, aid, interaction.user.id,
                                     interaction.user.id, "claim")
                await self.reconcile(interaction.guild)
            await interaction.followup.send("Diplomatic mandate claimed.", ephemeral=True)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)

    @app_commands.command(name="diplomat_relinquish", description="Give up your diplomatic mandate.")
    @app_commands.guild_only()
    async def diplomat_relinquish(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            async with self.lock:
                self.mandates.relinquish(interaction.guild_id, interaction.user.id)
                await self.reconcile(interaction.guild)
            await interaction.followup.send("Mandate relinquished; the alliance returned to default assignment rules.", ephemeral=True)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)

    @app_commands.command(name="vote_submit", description="Council R4/R5: submit a Yes/No/Abstain proposal.")
    @app_commands.guild_only()
    async def vote_submit(self, interaction: discord.Interaction, question: str,
                          submitter: discord.Member | None = None, fid: str | None = None):
        await interaction.response.defer(ephemeral=True)
        try:
            if len(question) > 1000:
                raise ValueError("Questions must be at most 1000 characters.")
            if submitter and fid:
                raise ValueError("Choose either a Discord submitter or FID, not both.")
            admin = PermissionManager.is_admin(interaction.user.id)[0]
            if (submitter or fid) and not admin:
                raise ValueError("Only admins can submit on behalf of another player.")
            player = dict(storage.user_row(int(fid)) or {}) if fid else self.main_player(
                submitter.id if submitter else interaction.user.id)
            if not player:
                raise ValueError("Unknown FID; register the player first.")
            aid = int(player["alliance"] or 0)
            if not admin and (player["rank"] not in (4, 5) or aid not in {
                    r["aid"] for r in storage.latest_nap_snapshot()}):
                raise ValueError("Only a verified R4/R5 of a council alliance can submit.")
            alliance = storage.alliance_row(aid)
            async with self.lock:
                vote_id = self.store.submit(interaction.guild_id, question, aid, player["fid"], interaction.user.id)
                self.store.set_identity(vote_id, alliance["abbr"] if alliance else "?",
                                        player["nickname"], player["discord_id"])
            await interaction.followup.send(f"Vote #{vote_id} added to the queue (DEFINED).", ephemeral=True)
        except (ValueError, TypeError) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)

    @app_commands.command(name="vote_queue", description="View the pending vote and ordered proposal queue.")
    @app_commands.guild_only()
    async def vote_queue(self, interaction: discord.Interaction):
        pending = self.store.pending(interaction.guild_id)
        lines = [f"Pending: #{pending['id']} — {safe(pending['title'])}" if pending else "No pending vote."]
        for vote in self.store.queue(interaction.guild_id):
            lines.append(f"#{vote['id']} {'URGENT ' if vote['urgent'] else ''}[{vote['priority']}] {safe(vote['title'])}")
        await interaction.response.send_message("\n".join(lines)[:1950], ephemeral=True)

    @app_commands.command(name="vote_urgent", description="Admin: set or cancel urgency for a DEFINED proposal.")
    @app_commands.guild_only()
    async def vote_urgent(self, interaction: discord.Interaction, vote_id: int, urgent: bool, reason: str = ""):
        await interaction.response.defer(ephemeral=True)
        try:
            self.require_admin(interaction)
            self.vote_for(interaction, vote_id)
            async with self.lock:
                self.store.set_urgent(vote_id, urgent, interaction.user.id, reason)
            await interaction.followup.send("Queue urgency updated.", ephemeral=True)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)

    @app_commands.command(name="vote_remove", description="Remove a DEFINED proposal; admin/R5 must supply a reason.")
    @app_commands.guild_only()
    async def vote_remove(self, interaction: discord.Interaction, vote_id: int, reason: str = ""):
        await interaction.response.defer(ephemeral=True)
        try:
            vote = self.vote_for(interaction, vote_id)
            owner = storage.discord_for_fid(vote["submitter_fid"]) == interaction.user.id
            admin = PermissionManager.is_admin(interaction.user.id)[0]
            if not owner and not admin:
                player = self.main_player(interaction.user.id)
                if player["rank"] != 5 or str(player["alliance"]) != str(vote["alliance_id"]):
                    raise ValueError("Only the submitter, their alliance R5, or admin may remove this vote.")
            if not owner and not reason.strip():
                raise ValueError("A reason is required.")
            async with self.lock:
                self.store.remove(vote_id, interaction.user.id, reason)
            await interaction.followup.send("Proposal removed; cancellation retained in history.", ephemeral=True)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)

    @app_commands.command(name="vote_next", description="Admin: start the next queued vote when idle.")
    @app_commands.guild_only()
    async def vote_next(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            self.require_admin(interaction)
            async with self.lock:
                await self.start_next(interaction.guild)
            await interaction.followup.send("Next vote opened for 24 hours.", ephemeral=True)
        except (ValueError, discord.HTTPException) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)

    @app_commands.command(name="vote_manual", description="Admin: record an in-game ballot or correction with voting FID and reason.")
    @app_commands.guild_only()
    @app_commands.choices(answer=ANSWERS)
    async def vote_manual(self, interaction: discord.Interaction, vote_id: int, alliance_id: int,
                          answer: str, fid: str, reason: str):
        await interaction.response.defer(ephemeral=True)
        try:
            self.require_admin(interaction)
            self.vote_for(interaction, vote_id)
            if int(fid) <= 0 or len(reason) > 1000:
                raise ValueError("Supply a positive player FID and a reason of at most 1000 characters.")
            async with self.lock:
                self.store.cast(vote_id, alliance_id, answer, int(fid), interaction.user.id,
                                manual=True, reason=reason)
            await interaction.followup.send(
                f"Manual ballot recorded: alliance {alliance_id}, {answer}, player {fid}.\n"
                f"Reason: {safe(reason)}\n{utc(time.time())}", ephemeral=True)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)

    @app_commands.command(name="vote_conclude", description="Admin: preview and confirm the pending vote result.")
    @app_commands.guild_only()
    async def vote_conclude(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            self.require_admin(interaction)
            self.store.expire()
            vote = self.store.pending(interaction.guild_id)
            if not vote:
                raise ValueError("No pending vote.")
            groups = self.store.participation(vote["id"])
            result = tally(sum(map(len, groups.values())), len(groups["YES"]),
                           len(groups["NO"]), len(groups["ABSTAINED"]))
            if vote["status"] == "OPEN" and not result.early_conclusion:
                raise ValueError("Early conclusion requires Yes > half of all eligible alliances.")
            await interaction.followup.send(f"Proposed outcome: **{result.status}**. "
                "Confirmation revalidates current ballots before concluding.",
                embed=self.participation_embed(vote["id"]),
                view=ConclusionView(self, vote["id"], interaction.user.id), ephemeral=True)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)

    @app_commands.command(name="vote_history", description="View concluded and removed proposals.")
    @app_commands.guild_only()
    async def vote_history(self, interaction: discord.Interaction):
        lines = [f"#{v['id']} {v['status']} — {safe(v['title'])}" for v in
                 self.store.list_votes(interaction.guild_id) if v["status"] not in ("DEFINED", "OPEN", "AWAITS_APPROVAL")]
        await interaction.response.send_message("\n".join(lines)[:1950] or "No vote history.", ephemeral=True)