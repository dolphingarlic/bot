import asyncio
import logging
import typing as t
from enum import Enum

import discord
from discord.ext.commands import Cog

from bot.bot import Bot
from bot.constants import Channels, Emojis, Roles

log = logging.getLogger(__name__)


class Signal(Enum):
    """Recognized incident status signals."""

    ACTIONED = Emojis.incident_actioned
    NOT_ACTIONED = Emojis.incident_unactioned
    INVESTIGATING = Emojis.incident_investigating


ALLOWED_ROLES: t.Set[int] = {Roles.moderators, Roles.admins, Roles.owners}
ALLOWED_EMOJI: t.Set[str] = {signal.value for signal in Signal}


def is_incident(message: discord.Message) -> bool:
    """True if `message` qualifies as an incident, False otherwise."""
    conditions = (
        message.channel.id == Channels.incidents,  # Message sent in #incidents
        not message.author.bot,                    # Not by a bot
        not message.content.startswith("#"),       # Doesn't start with a hash
        not message.pinned,                        # And isn't header
    )
    return all(conditions)


def own_reactions(message: discord.Message) -> t.Set[str]:
    """Get the set of reactions placed on `message` by the bot itself."""
    return {str(reaction.emoji) for reaction in message.reactions if reaction.me}


def has_signals(message: discord.Message) -> bool:
    """True if `message` already has all `Signal` reactions, False otherwise."""
    missing_signals = ALLOWED_EMOJI - own_reactions(message)
    return not missing_signals


async def add_signals(incident: discord.Message) -> None:
    """Add `Signal` member emoji to `incident` as reactions."""
    existing_reacts = own_reactions(incident)

    for signal_emoji in Signal:

        # This will not raise, but it is a superfluous API call that can be avoided
        if signal_emoji.value in existing_reacts:
            log.debug(f"Skipping emoji as it's already been placed: {signal_emoji}")

        else:
            log.debug(f"Adding reaction: {signal_emoji}")
            await incident.add_reaction(signal_emoji.value)


class Incidents(Cog):
    """Automation for the #incidents channel."""

    def __init__(self, bot: Bot) -> None:
        """Prepare `event_lock` and schedule `crawl_task` on start-up."""
        self.bot = bot

        self.event_lock = asyncio.Lock()
        self.crawl_task = self.bot.loop.create_task(self.crawl_incidents())

    async def crawl_incidents(self) -> None:
        """
        Crawl #incidents and add missing emoji where necessary.

        This is to catch-up should an incident be reported while the bot wasn't listening.
        After adding reactions, we take a short break to avoid drowning in ratelimits.

        Once this task is scheduled, listeners that change messages should await it.
        The crawl assumes that the channel history doesn't change as we go over it.
        """
        await self.bot.wait_until_guild_available()
        incidents: discord.TextChannel = self.bot.get_channel(Channels.incidents)

        # Limit the query at 50 as in practice, there should never be this many messages,
        # and if there are, something has likely gone very wrong
        limit = 50

        # Seconds to sleep after each message
        sleep = 2

        log.debug(f"Crawling messages in #incidents: {limit=}, {sleep=}")
        async for message in incidents.history(limit=limit):

            if not is_incident(message):
                log.debug("Skipping message: not an incident")
                continue

            if has_signals(message):
                log.debug("Skipping message: already has all signals")
                continue

            await add_signals(message)
            await asyncio.sleep(sleep)

        log.debug("Crawl task finished!")

    async def resolve_message(self, message_id: int) -> t.Optional[discord.Message]:
        """
        Get `discord.Message` for `message_id` from cache, or API.

        We first look into the local cache to see if the message is present.

        If not, we try to fetch the message from the API. This is necessary for messages
        which were sent before the bot's current session.

        However, in an edge-case, it is also possible that the message was already deleted,
        and the API will return a 404. In such a case, None will be returned. This signals
        that the event for `message_id` should be ignored.
        """
        await self.bot.wait_until_guild_available()  # First make sure that the cache is ready
        log.debug(f"Resolving message for: {message_id=}")
        message: discord.Message = self.bot._connection._get_message(message_id)  # noqa: Private attribute

        if message is not None:
            log.debug("Message was found in cache")
            return message

        log.debug("Message not found, attempting to fetch")
        try:
            message = await self.bot.get_channel(Channels.incidents).fetch_message(message_id)
        except Exception as exc:
            log.debug(f"Failed to fetch message: {exc}")
            return None
        else:
            log.debug("Message fetched successfully!")
            return message

    @Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Pass `message` to `add_signals` if and only if it satisfies `is_incident`."""
        if is_incident(message):
            await add_signals(message)