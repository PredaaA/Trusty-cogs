import asyncio
import discord
import logging
import aiohttp

from typing import Any, Optional
from datetime import datetime

from redbot.core import commands
from redbot.core.commands import Context
from redbot.core.i18n import Translator
from redbot.vendored.discord.ext import menus
from redbot.core.utils.chat_formatting import humanize_list

from .constants import TEAMS, HEADSHOT_URL, BASE_URL
from .standings import Standings
from .game import Game
from .helper import DATE_RE
from .errors import NoSchedule


_ = Translator("Hockey", __file__)
log = logging.getLogger("red.trusty-cogs.hockey")



class GamesMenu(menus.MenuPages, inherit_buttons=False):
    def __init__(
        self,
        source: menus.PageSource,
        clear_reactions_after: bool = True,
        delete_message_after: bool = False,
        timeout: int = 60,
        message: discord.Message = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            source,
            clear_reactions_after=clear_reactions_after,
            delete_message_after=delete_message_after,
            timeout=timeout,
            message=message,
            **kwargs,
        )

    async def update(self, payload):
        """|coro|

        Updates the menu after an event has been received.

        Parameters
        -----------
        payload: :class:`discord.RawReactionActionEvent`
            The reaction event that triggered this update.
        """
        button = self.buttons[payload.emoji]
        if not self._running:
            return

        try:
            if button.lock:
                async with self._lock:
                    if self._running:
                        await button(self, payload)
            else:
                await button(self, payload)
        except Exception as exc:
            log.debug("Ignored exception on reaction event", exc_info=exc)

    async def show_page(self, page_number, *, skip_next=False, skip_prev=False):
        try:
            page = await self._source.get_page(
                page_number, skip_next=skip_next, skip_prev=skip_prev
            )
        except NoSchedule:
            await self.message.edit(
                content=_("No Schedule could be found for that date and team."), embed=None
            )
            return
        self.current_page = page_number
        kwargs = await self._get_kwargs_from_page(page)
        await self.message.edit(**kwargs)

    async def send_initial_message(self, ctx, channel):
        """|coro|
        The default implementation of :meth:`Menu.send_initial_message`
        for the interactive pagination session.
        This implementation shows the first page of the source.
        """
        try:
            page = await self._source.get_page(0)
        except IndexError:
            if self.source.team:
                return await channel.send(
                    f"No schedule could be found for {humanize_list(self.source.team)} in date "
                    f"ranges {self.source._last_searched}."
                )
            else:
                return await channel.send(
                    f"No schedule could be found in date ranges {self.source._last_searched}"
                )
        kwargs = await self._get_kwargs_from_page(page)
        return await channel.send(**kwargs)

    async def show_checked_page(self, page_number: int) -> None:
        try:
            await self.show_page(page_number)
        except IndexError:
            # An error happened that can be handled, so ignore it.
            pass

    def reaction_check(self, payload):
        """Just extends the default reaction_check to use owner_ids"""
        if payload.message_id != self.message.id:
            return False
        if payload.user_id not in (*self.bot.owner_ids, self._author_id):
            return False
        return payload.emoji in self.buttons

    def _skip_single_arrows(self):
        max_pages = self._source.get_max_pages()
        if max_pages is None:
            return True
        return max_pages == 1

    @menus.button(
        "\N{BLACK LEFT-POINTING TRIANGLE}\N{VARIATION SELECTOR-16}", position=menus.First(1),
    )
    async def go_to_previous_page(self, payload):
        """go to the previous page"""
        await self.show_checked_page(self.current_page - 1)

    @menus.button(
        "\N{BLACK RIGHT-POINTING TRIANGLE}\N{VARIATION SELECTOR-16}", position=menus.Last(0),
    )
    async def go_to_next_page(self, payload):
        """go to the next page"""
        # log.info(f"Moving to next page, {self.current_page + 1}")
        await self.show_checked_page(self.current_page + 1)

    @menus.button(
        "\N{BLACK LEFT-POINTING DOUBLE TRIANGLE WITH VERTICAL BAR}\N{VARIATION SELECTOR-16}",
        position=menus.First(0),
    )
    async def go_to_first_page(self, payload):
        """go to the first page"""
        await self.show_page(0, skip_prev=True)

    @menus.button(
        "\N{BLACK RIGHT-POINTING DOUBLE TRIANGLE WITH VERTICAL BAR}\N{VARIATION SELECTOR-16}",
        position=menus.Last(1),
    )
    async def go_to_last_page(self, payload):
        """go to the last page"""
        # The call here is safe because it's guarded by skip_if
        await self.show_page(0, skip_next=True)

    @menus.button("\N{CROSS MARK}")
    async def stop_pages(self, payload: discord.RawReactionActionEvent) -> None:
        """stops the pagination session."""
        self.stop()
        await self.message.delete()

    @menus.button("\N{TEAR-OFF CALENDAR}")
    async def choose_date(self, payload: discord.RawReactionActionEvent) -> None:
        """stops the pagination session."""
        send_msg = await self.ctx.send(
            _("Enter the date you would like to see `YYYY-MM-DD` format is accepted.")
        )

        def check(m: discord.Message):
            return m.author == self.ctx.author and DATE_RE.search(m.clean_content)

        try:
            msg = await self.ctx.bot.wait_for("message", check=check, timeout=30)
        except asyncio.TimeoutError:
            await send_msg.delete()
            return
        search = DATE_RE.search(msg.clean_content)
        if search:
            date_str = f"{search.group(1)}-{search.group(3)}-{search.group(4)}"
            date = datetime.strptime(date_str, "%Y-%m-%d")
            log.info(date)
            self.source.date = date
            try:
                await self.source.prepare()
            except NoSchedule:
                return await self.ctx.send(
                    _("Sorry No schedule was found for the date range {date}").format(
                        date=date_str
                    )
                )
            await self.show_page(0)


class StandingsPages(menus.ListPageSource):
    def __init__(self, pages: list):
        super().__init__(pages, per_page=1)
        self.pages = pages

    def is_paginating(self):
        return False

    async def format_page(self, menu: menus.MenuPages, page):
        return await Standings.all_standing_embed(self.pages)


class TeamStandingsPages(menus.ListPageSource):
    def __init__(self, pages: list):
        super().__init__(pages, per_page=1)

    def is_paginating(self):
        return True

    async def format_page(self, menu: menus.MenuPages, page):
        return await Standings.make_team_standings_embed(page)


class ConferenceStandingsPages(menus.ListPageSource):
    def __init__(self, pages: list):
        super().__init__(pages, per_page=1)

    def is_paginating(self):
        return True

    async def format_page(self, menu: menus.MenuPages, page):
        return await Standings.make_conference_standings_embed(page)


class DivisionStandingsPages(menus.ListPageSource):
    def __init__(self, pages: list):
        super().__init__(pages, per_page=1)

    def is_paginating(self):
        return True

    async def format_page(self, menu: menus.MenuPages, page):
        return await Standings.make_division_standings_embed(page)


class LeaderboardPages(menus.ListPageSource):
    def __init__(self, pages: list, style: str):
        super().__init__(pages, per_page=1)
        self.style = style

    def is_paginating(self):
        return True

    async def format_page(self, menu: menus.MenuPages, page):
        em = discord.Embed(timestamp=datetime.utcnow())
        description = ""
        for msg in page:
            description += msg
        em.description = description
        em.set_author(
            name=menu.ctx.guild.name + _(" Pickems {style} Leaderboard").format(style=self.style),
            icon_url=menu.ctx.guild.icon_url,
        )
        em.set_thumbnail(url=menu.ctx.guild.icon_url)
        em.set_footer(text=f"Page {menu.current_page + 1}/{self.get_max_pages()}")
        return em


class RosterPages(menus.ListPageSource):
    def __init__(self, pages: list):
        super().__init__(pages, per_page=1)
        self.pages = pages

    def is_paginating(self):
        return True

    async def format_page(self, menu: menus.MenuPages, page):
        url = BASE_URL + page["person"]["link"] + "?expand=person.stats&stats=yearByYear"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                player_data = await resp.json()
        player = player_data["people"][0]
        year_stats: list = []
        try:
            year_stats = [
                league
                for league in player["stats"][0]["splits"]
                if league["league"]["name"] == "National Hockey League"
            ][-1]
        except IndexError:
            pass
        name = player["fullName"]
        number = player["primaryNumber"]
        position = player["primaryPosition"]["name"]
        headshot = HEADSHOT_URL.format(player["id"])
        team = player["currentTeam"]["name"]
        em = discord.Embed(colour=int(TEAMS[team]["home"].replace("#", ""), 16))
        em.set_author(
            name="{} #{}".format(name, number),
            url=TEAMS[team]["team_url"],
            icon_url=TEAMS[team]["logo"],
        )
        em.add_field(name="Position", value=position)
        em.set_thumbnail(url=headshot)
        em.set_footer(text=f"Page {menu.current_page + 1}/{self.get_max_pages()}")
        if not year_stats:
            return em
        if position != "Goalie":
            post_data = {
                _("Shots"): year_stats["stat"]["shots"],
                _("Goals"): year_stats["stat"]["goals"],
                _("Assists"): year_stats["stat"]["assists"],
                _("Hits"): year_stats["stat"]["hits"],
                _("Face Off Percent"): year_stats["stat"]["faceOffPct"],
                "+/-": year_stats["stat"]["plusMinus"],
                _("Blocked Shots"): year_stats["stat"]["blocked"],
                _("PIM"): year_stats["stat"]["pim"],
            }
            for key, value in post_data.items():
                if value != 0.0:
                    em.add_field(name=key, value=value)
        else:
            saves = year_stats["stat"]["saves"]
            save_percentage = year_stats["stat"]["savePercentage"]
            goals_against_average = year_stats["stat"]["goalAgainstAverage"]
            em.add_field(name=_("Saves"), value=saves)
            em.add_field(name=_("Save Percentage"), value=save_percentage)
            em.add_field(name=_("Goals Against Average"), value=goals_against_average)
        return em


class BaseMenu(menus.MenuPages, inherit_buttons=False):
    def __init__(
        self,
        source: menus.PageSource,
        cog: Optional[commands.Cog] = None,
        page_start: Optional[int] = 0,
        clear_reactions_after: bool = True,
        delete_message_after: bool = False,
        timeout: int = 60,
        message: discord.Message = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            source,
            clear_reactions_after=clear_reactions_after,
            delete_message_after=delete_message_after,
            timeout=timeout,
            message=message,
            **kwargs,
        )
        self.cog = cog
        self.page_start = page_start

    async def send_initial_message(self, ctx, channel):
        """|coro|
        The default implementation of :meth:`Menu.send_initial_message`
        for the interactive pagination session.
        This implementation shows the first page of the source.
        """
        page = await self._source.get_page(self.page_start)
        kwargs = await self._get_kwargs_from_page(page)
        return await channel.send(**kwargs)

    async def update(self, payload):
        """|coro|

        Updates the menu after an event has been received.

        Parameters
        -----------
        payload: :class:`discord.RawReactionActionEvent`
            The reaction event that triggered this update.
        """
        button = self.buttons[payload.emoji]
        if not self._running:
            return

        try:
            if button.lock:
                async with self._lock:
                    if self._running:
                        await button(self, payload)
            else:
                await button(self, payload)
        except Exception as exc:
            log.debug("Ignored exception on reaction event", exc_info=exc)

    async def show_checked_page(self, page_number: int) -> None:
        max_pages = self._source.get_max_pages()
        try:
            if max_pages is None:
                # If it doesn't give maximum pages, it cannot be checked
                await self.show_page(page_number)
            elif page_number >= max_pages:
                await self.show_page(0)
            elif page_number < 0:
                await self.show_page(max_pages - 1)
            elif max_pages > page_number >= 0:
                await self.show_page(page_number)
        except IndexError:
            # An error happened that can be handled, so ignore it.
            pass

    def reaction_check(self, payload):
        """Just extends the default reaction_check to use owner_ids"""
        if payload.message_id != self.message.id:
            return False
        if payload.user_id not in (*self.bot.owner_ids, self._author_id):
            return False
        return payload.emoji in self.buttons

    def _skip_single_arrows(self):
        max_pages = self._source.get_max_pages()
        if max_pages is None:
            return True
        return max_pages == 1

    def _skip_double_triangle_buttons(self):
        max_pages = self._source.get_max_pages()
        if max_pages is None:
            return True
        return max_pages <= 2

    @menus.button(
        "\N{BLACK LEFT-POINTING TRIANGLE}\N{VARIATION SELECTOR-16}",
        position=menus.First(1),
        skip_if=_skip_single_arrows,
    )
    async def go_to_previous_page(self, payload):
        """go to the previous page"""
        await self.show_checked_page(self.current_page - 1)

    @menus.button(
        "\N{BLACK RIGHT-POINTING TRIANGLE}\N{VARIATION SELECTOR-16}",
        position=menus.Last(0),
        skip_if=_skip_single_arrows,
    )
    async def go_to_next_page(self, payload):
        """go to the next page"""
        await self.show_checked_page(self.current_page + 1)

    @menus.button(
        "\N{BLACK LEFT-POINTING DOUBLE TRIANGLE WITH VERTICAL BAR}\N{VARIATION SELECTOR-16}",
        position=menus.First(0),
        skip_if=_skip_double_triangle_buttons,
    )
    async def go_to_first_page(self, payload):
        """go to the first page"""
        await self.show_page(0)

    @menus.button(
        "\N{BLACK RIGHT-POINTING DOUBLE TRIANGLE WITH VERTICAL BAR}\N{VARIATION SELECTOR-16}",
        position=menus.Last(1),
        skip_if=_skip_double_triangle_buttons,
    )
    async def go_to_last_page(self, payload):
        """go to the last page"""
        # The call here is safe because it's guarded by skip_if
        await self.show_page(self._source.get_max_pages() - 1)

    @menus.button("\N{CROSS MARK}")
    async def stop_pages(self, payload: discord.RawReactionActionEvent) -> None:
        """stops the pagination session."""
        self.stop()
        await self.message.delete()
