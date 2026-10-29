"""
Barons Motorcycle Club - Character Kill Contract  (Modmail plugin)

Usage:   <prefix>ck <user>

The bot DMs the user an embed with  [Send] [Questions] [Cancel]  buttons.
The user answers every question, presses Send, and the finished contract is
posted to SUBMIT_CHANNEL_ID.

Repo layout (see "Folder Structure" in the plugin docs):

    your-repo/
        charkill/
            charkill.py      <- this file

Install with:  ?plugin add <github-user>/<repo>/charkill

Everything you are likely to want to change is in the CONFIGURATION section.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import discord
from discord import ButtonStyle, Interaction, TextStyle
from discord.ext import commands

from core import checks
from core.models import PermissionLevel

logger = logging.getLogger("Modmail.CharacterKill")


# =============================================================================
#  CONFIGURATION
# =============================================================================

# Channel the finished contracts are posted to (right click channel -> Copy Channel ID).
SUBMIT_CHANNEL_ID = 1507936269521391847

# Modmail permission level needed to run the command.
# (You can also override it in Discord with `?perms add override ck <role>`.)
COMMAND_PERMISSION = PermissionLevel.MODERATOR

# ---- Look of both embeds ----------------------------------------------------
EMBED_COLOR = 0xA68937  # #a68937
EMBED_THUMBNAIL = "https://cdn.discordapp.com/attachments/1506363914550771742/1556464154146242642/2.png"

# ---- Embed templates --------------------------------------------------------
# Placeholders available in every title / entry / footer:
#   {user}       the user the contract was sent to (the one who fills it out)
#   {moderator}  whoever ran the command
#   {time}       date and time
#   {option}     label of the button that produced the embed
#                ("Send" on the submitted form, empty on the DM panel)
#
# Only the ENTRY templates also have:
#   {questions}  the question / answer block (see QUESTION_FORMAT below)
#
# {user} and {moderator} are NEVER mentions. They always show as
# "username (ID)", e.g.  3nrique_puravida (553361894115770386)
#
# {time} is plain UTC text everywhere, except inside the entry of the SUBMITTED
# embed, where it is shown in each reader's own timezone (Discord can't do that
# in titles or footers).

# DM sent to the user
DM_TITLE = "Barons Motorcycle Club - Character Kill Contract"
DM_ENTRY = "{questions}"
DM_FOOTER = "Sent by {moderator} on {time}. DO NOT REPLY TO THIS MESSAGE."

# Embed posted to SUBMIT_CHANNEL_ID once the user presses Send
SUBMIT_TITLE = "Barons Motorcycle Club - Character Kill Contract"
SUBMIT_ENTRY = "{questions}\n**Answer from** {user}"
SUBMIT_FOOTER = "Sent by {moderator} at {time}"

# How each question is written inside {questions}. Placeholders: {question} {answer}
QUESTION_FORMAT = "**{question}**\n{answer}\n"

# Shown as the answer until the user fills a question in.
UNANSWERED_TEXT = "Fill out"

# Format of {time} when it has to be plain text (always UTC).
TIME_FORMAT_UTC = "%d %b %Y, %H:%M UTC"

# ---- Buttons and messages ---------------------------------------------------
BUTTON_SEND = "Send"
BUTTON_QUESTIONS = "Questions"
BUTTON_CANCEL = "Cancel"

TEXT_SENT = "✅ Your Character Kill contract has been submitted."
TEXT_CANCELLED = "❌ This Character Kill contract was cancelled."
TEXT_MISSING = "You must fill out every question before sending.\n**Still to fill out:**\n{missing}"
TEXT_INACTIVE = "This contract is no longer active."

# ---- Questions --------------------------------------------------------------
# These are EXAMPLES - replace them with your own. Every question is required.
#
#   id           unique, letters / numbers / underscore only (never change it
#                while contracts are pending)
#   label        the question text (max 100 characters)
#   type         "short"    one-line text box
#                "long"     multi-line text box
#                "dropdown" pick one value from `options`
#   placeholder  (optional, short/long) grey hint text inside the box
#   min_length / max_length   (optional, short/long) limits for the answer
#                (defaults: short = 150, long = 1000, hard cap = 4000)
#   options      (dropdown only) up to 25 choices, each max 100 characters
#
# Max 25 questions.
QUESTIONS: List[Dict[str, Any]] = [
    {
        "id": "character_name",
        "label": "Character Name",
        "type": "short",
        "placeholder": "Full name of the character",
        "max_length": 100,
    },
    {
        "id": "reason",
        "label": "Reason for the Character Kill",
        "type": "long",
        "placeholder": "Explain what led to this contract.",
        "max_length": 1000,
    },
    {
        "id": "consent",
        "label": "Do you accept this Character Kill?",
        "type": "dropdown",
        "options": ["Yes, I accept", "No, I do not accept"],
    },
    {
        "id": "signature",
        "label": "Signature",
        "type": "short",
        "placeholder": "Type your character's full name",
    },
]

# =============================================================================
#  END OF CONFIGURATION
# =============================================================================

_DEFAULT_MAX_LENGTH = {"short": 150, "long": 1000}
_QUESTION_TYPES = ("short", "long", "dropdown")
QUESTIONS_BY_ID: Dict[str, Dict[str, Any]] = {}

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


def _validate_config() -> None:
    """Fail early (with a readable message) if QUESTIONS is malformed."""
    if not 1 <= len(QUESTIONS) <= 25:
        raise ValueError("charkill: QUESTIONS must contain between 1 and 25 questions.")

    QUESTIONS_BY_ID.clear()
    worst_case = 0
    for q in QUESTIONS:
        qid = q.get("id")
        if not isinstance(qid, str) or not re.fullmatch(r"[A-Za-z0-9_]+", qid):
            raise ValueError(f"charkill: invalid question id {qid!r} (letters, numbers and _ only).")
        if qid in QUESTIONS_BY_ID:
            raise ValueError(f"charkill: duplicate question id {qid!r}.")
        if not q.get("label"):
            raise ValueError(f"charkill: question {qid!r} has no label.")
        qtype = q.get("type")
        if qtype not in _QUESTION_TYPES:
            raise ValueError(f"charkill: question {qid!r} has an invalid type {qtype!r}.")

        if qtype == "dropdown":
            options = q.get("options")
            if (
                not isinstance(options, list)
                or not 1 <= len(options) <= 25
                or any(not isinstance(o, str) or not 1 <= len(o) <= 100 for o in options)
            ):
                raise ValueError(
                    f"charkill: question {qid!r} needs 1-25 `options`, each 1-100 characters."
                )
            size = max(len(o) for o in options)
        else:
            size = _max_length(q)

        worst_case += len(q["label"]) + size + 6
        QUESTIONS_BY_ID[qid] = q

    if worst_case > 3800:
        logger.warning(
            "charkill: the questions can add up to ~%s characters, which may not fit in one embed "
            "description (4096). Lower some max_length values.",
            worst_case,
        )


def _max_length(question: Dict[str, Any]) -> int:
    return min(int(question.get("max_length", _DEFAULT_MAX_LENGTH[question["type"]])), 4000)


# -----------------------------------------------------------------------------
#  Text / embed helpers
# -----------------------------------------------------------------------------


def _clip(text: Any, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _fill(template: str, values: Dict[str, str]) -> str:
    """Replace {placeholders} in one pass. Unknown ones are left untouched."""
    return _PLACEHOLDER_RE.sub(lambda m: values.get(m.group(1), m.group(0)), template)


def _is_answered(value: Optional[str]) -> bool:
    if not value:
        return False
    value = value.strip()
    return bool(value) and value.lower() != UNANSWERED_TEXT.lower()


def _questions_block(answers: Dict[str, str]) -> str:
    parts = []
    for q in QUESTIONS:
        answer = answers.get(q["id"])
        parts.append(
            _fill(
                QUESTION_FORMAT,
                {
                    "question": q["label"],
                    "answer": answer if _is_answered(answer) else UNANSWERED_TEXT,
                },
            )
        )
    return "\n".join(parts)


def _placeholders(rec: Dict[str, Any], *, local_time: bool, option: str, when: int) -> Dict[str, str]:
    if local_time:
        time_text = f"<t:{when}:f>"  # rendered in each reader's own timezone
    else:
        time_text = datetime.fromtimestamp(when, tz=timezone.utc).strftime(TIME_FORMAT_UTC)
    return {
        # never a mention: "username (ID)"
        "user": f"{rec['user_name']} ({rec['user_id']})",
        "moderator": f"{rec['moderator_name']} ({rec['moderator_id']})",
        "time": time_text,
        "option": option,
    }


def _build_embed(
    rec: Dict[str, Any],
    answers: Dict[str, str],
    *,
    title: str,
    entry: str,
    footer: str,
    local_time_entry: bool,
    option: str,
    when: int,
) -> discord.Embed:
    # Titles and footers can never show local timestamps -> UTC text.
    plain = _placeholders(rec, local_time=False, option=option, when=when)
    entry_values = _placeholders(rec, local_time=local_time_entry, option=option, when=when)
    entry_values["questions"] = _questions_block(answers)

    embed = discord.Embed(
        title=_clip(_fill(title, plain), 256),
        description=_clip(_fill(entry, entry_values), 4096),
        colour=discord.Colour(EMBED_COLOR),
    )
    if EMBED_THUMBNAIL:
        embed.set_thumbnail(url=EMBED_THUMBNAIL)
    embed.set_footer(text=_clip(_fill(footer, plain), 2048))
    return embed


def build_dm_embed(rec: Dict[str, Any], answers: Dict[str, str]) -> discord.Embed:
    return _build_embed(
        rec,
        answers,
        title=DM_TITLE,
        entry=DM_ENTRY,
        footer=DM_FOOTER,
        local_time_entry=False,  # DM shows {time} in UTC
        option="",
        when=rec["created_at"],
    )


def build_submit_embed(rec: Dict[str, Any], answers: Dict[str, str], when: int) -> discord.Embed:
    return _build_embed(
        rec,
        answers,
        title=SUBMIT_TITLE,
        entry=SUBMIT_ENTRY,
        footer=SUBMIT_FOOTER,
        local_time_entry=True,  # {time} in the description shows in each reader's timezone
        option=BUTTON_SEND,
        when=when,
    )


def picker_text(answers: Dict[str, str]) -> str:
    done = sum(1 for q in QUESTIONS if _is_answered(answers.get(q["id"])))
    text = (
        f"**Questions** - {done}/{len(QUESTIONS)} answered\n"
        "Pick a question below to answer it.  ✅ answered · ❌ still needs an answer"
    )
    if done == len(QUESTIONS):
        text += f"\n\nAll done! Press **{BUTTON_SEND}** on the contract to submit it."
    return text


# -----------------------------------------------------------------------------
#  Question picker (ephemeral, opened by the "Questions" button)
# -----------------------------------------------------------------------------


class AnswerModal(discord.ui.Modal):
    """Text box for `short` and `long` questions."""

    def __init__(self, cog: CharacterKill, dm_message: discord.Message, question: Dict[str, Any], current: Optional[str]):
        super().__init__(title=_clip(question["label"], 45), timeout=900)
        self.cog = cog
        self.dm_message = dm_message
        self.question = question
        self.answer = discord.ui.TextInput(
            label=_clip(question["label"], 45),
            style=TextStyle.long if question["type"] == "long" else TextStyle.short,
            placeholder=question.get("placeholder"),
            default=current if _is_answered(current) else None,
            required=True,
            min_length=question.get("min_length"),
            max_length=_max_length(question),
        )
        self.add_item(self.answer)

    async def on_submit(self, interaction: Interaction) -> None:
        value = self.answer.value.strip()
        if not _is_answered(value):
            await interaction.response.send_message(
                f"Please enter a real answer for **{self.question['label']}**.", ephemeral=True
            )
            return

        await interaction.response.defer()
        rec = await self.cog.save_answer(self.dm_message, self.question["id"], value)
        if rec is None:
            await interaction.followup.send(TEXT_INACTIVE, ephemeral=True)
            return
        answers = rec.get("answers", {})
        await interaction.edit_original_response(
            content=picker_text(answers),
            view=QuestionPicker(self.cog, self.dm_message, answers),
        )


class QuestionSelect(discord.ui.Select):
    def __init__(self, cog: CharacterKill, dm_message: discord.Message, answers: Dict[str, str]):
        options = []
        for q in QUESTIONS:
            answer = answers.get(q["id"])
            answered = _is_answered(answer)
            preview = " ".join(str(answer).split()) if answered else UNANSWERED_TEXT
            options.append(
                discord.SelectOption(
                    label=_clip(q["label"], 100),
                    value=q["id"],
                    description=_clip(preview, 100),
                    emoji="✅" if answered else "❌",
                )
            )
        super().__init__(placeholder="Choose a question to answer", options=options, min_values=1, max_values=1)
        self.cog = cog
        self.dm_message = dm_message

    async def callback(self, interaction: Interaction) -> None:
        question = QUESTIONS_BY_ID.get(self.values[0])
        rec = await self.cog.coll.find_one({"_id": self.dm_message.id})
        if question is None or rec is None:
            await interaction.response.edit_message(content=TEXT_INACTIVE, view=None)
            return

        current = rec.get("answers", {}).get(question["id"])
        if question["type"] == "dropdown":
            view = AnswerDropdownView(self.cog, self.dm_message, question, current)
            await interaction.response.edit_message(
                content=f"**{question['label']}**\nChoose your answer below.", view=view
            )
        else:
            await interaction.response.send_modal(AnswerModal(self.cog, self.dm_message, question, current))


class QuestionPicker(discord.ui.View):
    def __init__(self, cog: CharacterKill, dm_message: discord.Message, answers: Dict[str, str]):
        super().__init__(timeout=600)
        self.add_item(QuestionSelect(cog, dm_message, answers))


class AnswerSelect(discord.ui.Select):
    """Drop-down of answers for `dropdown` questions."""

    def __init__(self, cog: CharacterKill, dm_message: discord.Message, question: Dict[str, Any], current: Optional[str]):
        options = [
            discord.SelectOption(label=option, value=str(i), default=(option == current))
            for i, option in enumerate(question["options"])
        ]
        super().__init__(
            placeholder=_clip(question["label"], 150), options=options, min_values=1, max_values=1
        )
        self.cog = cog
        self.dm_message = dm_message
        self.question = question

    async def callback(self, interaction: Interaction) -> None:
        value = self.question["options"][int(self.values[0])]
        await interaction.response.defer()
        rec = await self.cog.save_answer(self.dm_message, self.question["id"], value)
        if rec is None:
            await interaction.edit_original_response(content=TEXT_INACTIVE, view=None)
            return
        answers = rec.get("answers", {})
        await interaction.edit_original_response(
            content=picker_text(answers),
            view=QuestionPicker(self.cog, self.dm_message, answers),
        )


class AnswerDropdownView(discord.ui.View):
    def __init__(self, cog: CharacterKill, dm_message: discord.Message, question: Dict[str, Any], current: Optional[str]):
        super().__init__(timeout=600)
        self.cog = cog
        self.dm_message = dm_message
        self.add_item(AnswerSelect(cog, dm_message, question, current))

    @discord.ui.button(label="Back", style=ButtonStyle.grey, row=1)
    async def back_button(self, interaction: Interaction, button: discord.ui.Button) -> None:
        rec = await self.cog.coll.find_one({"_id": self.dm_message.id})
        if rec is None:
            await interaction.response.edit_message(content=TEXT_INACTIVE, view=None)
            return
        answers = rec.get("answers", {})
        await interaction.response.edit_message(
            content=picker_text(answers),
            view=QuestionPicker(self.cog, self.dm_message, answers),
        )


# -----------------------------------------------------------------------------
#  The contract itself: Send / Questions / Cancel
#  (persistent: the buttons keep working after a bot restart)
# -----------------------------------------------------------------------------


class ContractPanel(discord.ui.View):
    def __init__(self, cog: CharacterKill):
        super().__init__(timeout=None)
        self.cog = cog

    async def _load(self, interaction: Interaction) -> Optional[Dict[str, Any]]:
        """Fetch this DM's contract record, replying to the user if it can't be used."""
        message = interaction.message
        rec = await self.cog.coll.find_one({"_id": message.id}) if message else None
        if rec is None:
            await interaction.response.send_message(TEXT_INACTIVE, ephemeral=True)
            if message:
                try:
                    await message.edit(view=None)
                except discord.HTTPException:
                    pass
            return None
        if interaction.user.id != rec["user_id"]:
            await interaction.response.send_message("This contract isn't for you.", ephemeral=True)
            return None
        return rec

    @discord.ui.button(label=BUTTON_SEND, style=ButtonStyle.green, custom_id="charkill:send")
    async def send_button(self, interaction: Interaction, button: discord.ui.Button) -> None:
        rec = await self._load(interaction)
        if rec is None:
            return

        answers = rec.get("answers", {})
        missing = [q["label"] for q in QUESTIONS if not _is_answered(answers.get(q["id"]))]
        if missing:
            text = TEXT_MISSING.format(missing="\n".join(f"- {label}" for label in missing))
            await interaction.response.send_message(text, ephemeral=True)
            return

        await interaction.response.defer()

        # Claim the record so a double click can't submit the contract twice.
        claimed = await self.cog.coll.find_one_and_delete({"_id": interaction.message.id})
        if claimed is None:
            await interaction.followup.send("This contract was already sent or cancelled.", ephemeral=True)
            return

        embed = build_submit_embed(claimed, claimed.get("answers", {}), int(time.time()))
        try:
            channel = await self.cog.get_submit_channel()
            if channel is None:
                raise RuntimeError("submit channel not found")
            await channel.send(embed=embed)
        except Exception:
            logger.exception("charkill: could not post the contract to channel %s", SUBMIT_CHANNEL_ID)
            await self.cog.coll.insert_one(claimed)  # put it back so the user can retry
            await interaction.followup.send(
                "Something went wrong while sending your contract. Please try again, "
                "and contact staff if it keeps happening.",
                ephemeral=True,
            )
            return

        await interaction.edit_original_response(content=TEXT_SENT, view=None)

    @discord.ui.button(label=BUTTON_QUESTIONS, style=ButtonStyle.blurple, custom_id="charkill:questions")
    async def questions_button(self, interaction: Interaction, button: discord.ui.Button) -> None:
        rec = await self._load(interaction)
        if rec is None:
            return
        answers = rec.get("answers", {})
        await interaction.response.send_message(
            content=picker_text(answers),
            view=QuestionPicker(self.cog, interaction.message, answers),
            ephemeral=True,
        )

    @discord.ui.button(label=BUTTON_CANCEL, style=ButtonStyle.red, custom_id="charkill:cancel")
    async def cancel_button(self, interaction: Interaction, button: discord.ui.Button) -> None:
        rec = await self._load(interaction)
        if rec is None:
            return
        await self.cog.coll.delete_one({"_id": interaction.message.id})
        await interaction.response.edit_message(content=TEXT_CANCELLED, view=None)


# -----------------------------------------------------------------------------
#  Cog
# -----------------------------------------------------------------------------


class CharacterKill(commands.Cog):
    """Character Kill contracts for the Barons Motorcycle Club."""

    def __init__(self, bot):
        self.bot = bot
        self.coll = bot.api.get_plugin_partition(self)  # motor collection
        self._persistent_view: Optional[ContractPanel] = None

    async def cog_load(self) -> None:
        # Re-attach the buttons of contracts that are still pending after a restart.
        self._persistent_view = ContractPanel(self)
        self.bot.add_view(self._persistent_view)

    def cog_unload(self) -> None:
        if self._persistent_view is not None:
            self._persistent_view.stop()

    # -- helpers --------------------------------------------------------------

    async def get_submit_channel(self) -> Optional[discord.abc.Messageable]:
        if not SUBMIT_CHANNEL_ID:
            return None
        channel = self.bot.get_channel(SUBMIT_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(SUBMIT_CHANNEL_ID)
            except discord.HTTPException:
                return None
        return channel

    async def save_answer(self, dm_message: discord.Message, question_id: str, value: str) -> Optional[Dict[str, Any]]:
        """Store an answer and refresh the contract embed in the user's DMs."""
        result = await self.coll.update_one(
            {"_id": dm_message.id}, {"$set": {f"answers.{question_id}": value}}
        )
        if result.matched_count == 0:
            return None
        rec = await self.coll.find_one({"_id": dm_message.id})
        if rec is not None:
            try:
                await dm_message.edit(embed=build_dm_embed(rec, rec.get("answers", {})))
            except discord.HTTPException:
                logger.exception("charkill: could not refresh contract DM %s", dm_message.id)
        return rec

    def _error(self, text: str) -> discord.Embed:
        return discord.Embed(description=text, colour=self.bot.error_color)

    # -- command --------------------------------------------------------------

    @commands.command(name="ck")
    @checks.has_permissions(COMMAND_PERMISSION)
    async def ck(self, ctx: commands.Context, user: discord.User):
        """
        Send a Character Kill contract to a user's DMs.

        Usage: ck <@user>
        """
        if user.bot:
            await ctx.send(embed=self._error("You can't send a contract to a bot."))
            return

        if await self.get_submit_channel() is None:
            await ctx.send(
                embed=self._error(
                    "The submission channel isn't set up. Set `SUBMIT_CHANNEL_ID` in the plugin "
                    "to a channel the bot can see and post in."
                )
            )
            return

        rec: Dict[str, Any] = {
            "user_id": user.id,
            "user_name": user.name,
            "moderator_id": ctx.author.id,
            "moderator_name": ctx.author.name,
            "created_at": int(time.time()),
            "answers": {},
        }

        try:
            message = await user.send(embed=build_dm_embed(rec, {}), view=ContractPanel(self))
        except discord.Forbidden:
            await ctx.send(embed=self._error(f"I can't DM {user.mention}. Their DMs are closed."))
            return
        except discord.HTTPException as exc:
            await ctx.send(embed=self._error(f"Couldn't send the contract: `{exc}`"))
            return

        rec["_id"] = message.id
        try:
            await self.coll.insert_one(rec)
        except Exception:
            logger.exception("charkill: could not store the contract record")
            try:
                await message.delete()
            except discord.HTTPException:
                pass
            await ctx.send(embed=self._error("Couldn't save the contract to the database. Nothing was sent."))
            return

        await ctx.send(
            embed=discord.Embed(
                description=f"Character Kill contract sent to {user.mention}'s DMs.",
                colour=self.bot.main_color,
            )
        )


async def setup(bot):
    _validate_config()
    await bot.add_cog(CharacterKill(bot))
