import discord
import asyncio
import aiohttp
import time
import logging

from typing import Tuple, Optional, List
from datetime import datetime, timedelta, timezone

from redbot.core import Config, commands, VersionInfo, version_info
from redbot.core.bot import Red
from .twitch_profile import TwitchProfile
from .twitch_follower import TwitchFollower
from .errors import TwitchError


log = logging.getLogger("red.Trusty-cogs.Twitch")

BASE_URL = "https://api.twitch.tv/helix"


class TwitchAPI:
    """
    Get twitch user information and post when a user gets new followers
    """

    config: Config
    bot: Red
    rate_limit_resets: set
    rate_limit_remaining: int

    def __init__(self, bot):
        self.config: Config
        self.bot: Red
        self.rate_limit_resets: set = set()
        self.rate_limit_remaining: int = 0

    #####################################################################################
    # Logic for accessing twitch API with rate limit checks                             #
    # https://github.com/tsifrer/python-twitch-client/blob/master/twitch/helix/base.py  #
    #####################################################################################

    async def _get_api_tokens(self):
        try:
            keys = await self.bot.get_shared_api_tokens("twitch")
        except AttributeError:
            keys = await self.bot.db.api_tokens.get_raw("twitch", default={})
        return keys

    async def get_header(self) -> dict:
        keys = await self._get_api_tokens()
        header = {"Client-ID": keys["client_id"]}
        access_token = await self.config.access_token()
        if access_token != {}:
            # Return bearer token if availavble for more access
            header["Authorization"] = "Bearer {}".format(access_token["access_token"])
        return header

    async def wait_for_rate_limit_reset(self) -> None:
        """Check rate limits in response header and ensure we're following them
        https://github.com/tsifrer/python-twitch-client/blob/master/twitch/helix/base.py
        """
        if self.rate_limit_remaining == 0:
            current_time = int(time.time())
            self.rate_limit_resets = set(x for x in self.rate_limit_resets if x > current_time)

            if len(self.rate_limit_resets) > 0:
                reset_time = list(self.rate_limit_resets)[0]
                # Calculate wait time and add 0.1s to the wait time to allow Twitch to reset
                # their counter
                wait_time = reset_time - current_time + 0.1
                log.debug(wait_time)
                await asyncio.sleep(wait_time)

    async def oauth_check(self) -> None:
        url = "https://id.twitch.tv/oauth2/token"
        keys = await self._get_api_tokens()
        if "client_secret" not in keys:
            # Can't get the app access token without the client secret being set
            return
        client_id = keys["client_id"]
        client_secret = keys["client_secret"]
        access_token = await self.config.access_token()
        if access_token == {}:
            # Attempts to acquire an app access token
            scope = [
                "analytics:read:extensions",
                "analytics:read:games",
                "bits:read",
                "clips:edit",
                "user:edit",
                "user:edit:broadcast",
            ]
            params = {
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "client_credentials",
                "scope": " ".join(s for s in scope),
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(url, params=params) as resp:
                    access_token = await resp.json()
            await self.config.access_token.set(access_token)
        else:
            if "access_token" not in access_token:
                # Tries to re-aquire access token if set one is incorrect
                await self.config.access_token.set({})
                return await self.oauth_check()
            header = {"Authorization": "OAuth {}".format(access_token["access_token"])}
            url = "https://id.twitch.tv/oauth2/validate"
            async with aiohttp.ClientSession() as session:
                resp = await session.get(url, headers=header)
            if resp.status == 200:
                # Validates the access token before use
                return
            else:
                await self.config.access_token.set({})
                return await self.oauth_check()

    async def get_response(self, url: str) -> dict:
        """Get responses from twitch after checking rate limits"""
        await self.oauth_check()
        header = await self.get_header()
        await self.wait_for_rate_limit_reset()
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, headers=header, timeout=aiohttp.ClientTimeout(total=None)
            ) as resp:
                remaining = resp.headers.get("Ratelimit-Remaining")
                if remaining:
                    self.rate_limit_remaining = int(remaining)
                reset = resp.headers.get("Ratelimit-Reset")
                if reset:
                    self.rate_limit_resets.add(int(reset))

                if resp.status == 429:
                    log.info("Trying again")
                    return await self.get_response(url)

                return await resp.json()

    #####################################################################################

    async def make_user_embed(self, profile: TwitchProfile) -> discord.Embed:
        # makes the embed for a twitch profile
        em = discord.Embed(colour=int("6441A4", 16))
        em.description = profile.description
        url = "https://twitch.tv/{}".format(profile.login)
        em.set_author(name=profile.display_name, url=url, icon_url=profile.profile_image_url)
        em.set_image(url=profile.offline_image_url)
        em.set_thumbnail(url=profile.profile_image_url)
        footer_text = "{} Viewer count".format(profile.view_count)
        em.set_footer(text=footer_text, icon_url=profile.profile_image_url)
        return em

    async def make_follow_embed(
        self, account: TwitchProfile, profile: TwitchProfile, total_followers: int
    ):
        # makes the embed for a twitch profile
        em = discord.Embed(colour=int("6441A4", 16))
        url = "https://twitch.tv/{}".format(profile.login)
        em.description = f"{profile.description}\n\n{url}"[:2048]
        em.set_author(
            name=f"{profile.display_name} has just followed {account.display_name}!",
            url=url,
            icon_url=profile.profile_image_url,
        )
        em.set_image(url=profile.offline_image_url)
        em.add_field(name="Viewer count", value=str(profile.view_count))
        em.set_thumbnail(url=profile.profile_image_url)
        footer_text = f"{account.display_name} has {total_followers} followers"
        em.timestamp = datetime.utcnow()
        em.set_footer(text=footer_text, icon_url=account.profile_image_url)
        return em

    async def get_all_followers(self, user_id: str) -> Tuple[list, dict]:
        # Get's first 100 users following user_id
        url = "{}/users/follows?to_id={}&first=100".format(BASE_URL, user_id)
        data = await self.get_response(url)
        follows = [x["from_id"] for x in data["data"]]
        total = data["total"]
        log.debug(f"{len(follows)} of {total}")
        return follows, total

    async def get_profile_from_name(self, twitch_name: str) -> TwitchProfile:
        url = "{}/users?login={}".format(BASE_URL, twitch_name)
        return TwitchProfile.from_json(await self.get_response(url))

    async def get_profile_from_id(self, twitch_id: str) -> TwitchProfile:
        url = "{}/users?id={}".format(BASE_URL, twitch_id)
        return TwitchProfile.from_json(await self.get_response(url))

    async def get_new_followers(self, user_id: str) -> Tuple[List[TwitchFollower], int]:
        # Gets the last 100 followers from twitch
        url = "{}/users/follows?to_id={}&first=100".format(BASE_URL, user_id)
        data = await self.get_response(url)
        follows = [TwitchFollower.from_json(x) for x in data["data"]]
        total = data["total"]
        return follows, total

    async def get_new_clips(self, user_id: str, started_at: Optional[datetime] = None) -> List[dict]:
        """
        Gets and returns the last 20 clips generated for a user
        """
        url = f"{BASE_URL}/clips?broadcaster_id={user_id}"
        if started_at:
            url += f"&started_at={started_at.isoformat()}Z"
            url += f"&ended_at={datetime.utcnow().isoformat()}Z"
        data = await self.get_response(url)
        clips = data["data"]
        return clips

    async def maybe_get_twitch_profile(
        self, ctx: commands.Context, twitch_name: str
    ) -> TwitchProfile:
        if twitch_name is not None:
            # Search for twitch login name
            try:
                profile = await self.get_profile_from_name(twitch_name)
            except Exception:
                log.error("{} is not a valid Twitch username".format(twitch_name))
                raise TwitchError("{} is not a valid Twitch username".format(twitch_name))
        else:
            # User has set their twitch ID on the bot
            twitch_id = await self.config.user(ctx.author).id()
            if twitch_id == "":
                raise TwitchError("You must set a twitch ID")
            else:
                profile = await self.get_profile_from_id(twitch_id)
        return profile

    async def check_account_added(
        self, account_list: list, profile: TwitchProfile
    ) -> Optional[dict]:
        # Checks if the account is in the config and returns only that one
        account_return = None
        for account in account_list:
            if account["id"] == profile.id:
                account_return = account
        return account_return

    async def check_followers(self, account: dict):
        followed = await self.get_profile_from_id(account["id"])
        followers, total = await self.get_new_followers(account["id"])
        for follow in reversed(followers):
            if follow.from_id not in account["followers"]:
                try:
                    profile = await self.get_profile_from_id(follow.from_id)
                except Exception:
                    log.error(
                        f"Error getting twitch profile {follow.from_id}", exc_info=True
                    )
                log.info(
                    f"{profile.login} Followed! {followed.display_name} "
                    f"has {total} followers now."
                )
                em = await self.make_follow_embed(followed, profile, total)
                for channel_id in account["channels"]:
                    channel = self.bot.get_channel(id=channel_id)
                    if not channel:
                        continue
                    if channel.permissions_for(channel.guild.me).embed_links:
                        await channel.send(embed=em)
                    else:
                        text_msg = (
                            f"{profile.display_name} has just "
                            f"followed {account.display_name}!"
                        )
                        await channel.send(text_msg)
                async with self.config.twitch_accounts() as check_accounts:
                    check_accounts.remove(account)
                    account["followers"].append(follow.from_id)
                    check_accounts.append(account)

    async def send_clips_update(self, clip: dict, clip_data: dict):
        tasks = []
        msg = f"{clip_data['display_name']} has a new clip! {clip['url']}"
        for channel in clip_data["channels"]:
            channel = self.bot.get_channel(channel)
            if channel and channel.permissions_for(channel.guild.me).send_messages:
                tasks.append(channel.send(msg))
        await asyncio.gather(*tasks)

    async def check_clips(self):
        followed = await self.config.twitch_clips()
        for user_id, clip_data in followed.items():
            try:
                now = datetime.utcnow() + timedelta(days=-8)
                clips = await self.get_new_clips(user_id, now)
            except Exception:
                log.exception(
                    f"Error getting twitch clips {user_id}", exc_info=True
                )
                continue
            for clip in clips:
                if clip["id"] not in clip_data["clips"]:
                    await self.send_clips_update(clip, clip_data)
                    async with self.config.twitch_clips() as saved:
                        saved[user_id]["clips"].append(clip["id"])


    async def check_for_new_followers(self) -> None:
        # Checks twitch every minute for new followers
        if version_info >= VersionInfo.from_str("3.2.0"):
            await self.bot.wait_until_red_ready()
        else:
            await self.bot.wait_until_ready()
        while self is self.bot.get_cog("Twitch"):
            follow_accounts = await self.config.twitch_accounts()
            clip_accounts = await self.config.twitch_clips()
            for account in follow_accounts:
                await self.check_followers(account)
            await self.check_clips()
            await asyncio.sleep(60)
