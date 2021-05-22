import io

import aiohttp
import discord
import logging
import asyncio
import functools
import re

import requests
from TikTokApi.exceptions import TikTokCaptchaError
from redbot.core import commands, Config, checks
from TikTokApi import TikTokApi
from redbot.core.utils.chat_formatting import pagify
from urllib3.exceptions import NewConnectionError, ProxyError, MaxRetryError
from requests.exceptions import ConnectionError
from asyncio.exceptions import TimeoutError
from webdriver_manager.chrome import ChromeDriverManager
from datetime import datetime, timedelta
from PIL import Image
from colorhash import ColorHash
from dateutil.parser import parse as parsedate

UNIQUE_ID = 0x696969669


class MaximumProxyRequests(Exception):
    pass


class TikTok(commands.Cog):
    def __init__(self, bot, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.bot = bot
        self.log = logging.getLogger("tiktok")
        self.log.setLevel(logging.DEBUG)
        self.proxy = None
        self.api = None

        self.config = Config.get_conf(self, identifier=UNIQUE_ID, force_registration=True)
        self.config.register_guild(subscriptions=[], cache=[])
        self.config.register_global(interval=300, cache_size=500, proxy=[], proxies=[])
        self.main_task = self.bot.loop.create_task(self.initialize())
        self.background_task = self.bot.loop.create_task(self.background_get_new_videos())

    async def initialize(self):
        await self.bot.wait_until_red_ready()
        try:
            self.proxy = await self.config.proxy()
        except:
            self.proxy = None
            pass

        self.api = TikTokApi.get_instance(use_test_endpoints=False, use_selenium=True,
                                          custom_verifyFp="verify_kox6wops_bqKwq1Wc_OhSG_4O03_9CG2_t8CvbVmI3gZn",
                                          logging_level=logging.DEBUG, executablePath=ChromeDriverManager().install(),
                                          proxy=self.proxy)

        self.log.debug(f"Proxy: {await self.config.proxy()}")

    def get_tiktok_by_name(self, username, count):
        data = self.api.byUsername(username, count=count)
        return data

    def get_tiktok_dynamic_cover(self, post):
        image_data = self.api.getBytes(url=post['video']['dynamicCover'], proxy=None)

        im = Image.open(io.BytesIO(image_data))
        im.info.pop('background', None)

        with io.BytesIO() as image_binary:
            im.save(image_binary, 'gif', save_all=True)
            im.save(f"{post['id']}.gif", 'gif', save_all=True)
            image_binary.seek(0)
            file = discord.File(fp=image_binary, filename=f"{post['id']}.gif")
            self.log.debug(f"Saved {post['id']}.gif")
            return file

    async def get_new_proxy(self, proxies, truncate=False):
        url = 'http://pubproxy.com/api/proxy?limit=5&format=txt&type=http'
        self.log.debug("Attempting to get new proxy..")

        if len(proxies) > 0:
            self.log.debug(f"Cached proxies: {len(proxies['list'])}")
            self.log.debug(f"Last update: {proxies['last-updated']}")

        # More than 24 hours or empty
        if len(proxies) == 0 or \
                (datetime.now() - datetime.strptime(proxies['last-updated'], '%Y-%m-%d %H:%M:%S.%f')) > timedelta(1):
            self.log.debug(f'Updating proxy list..')
            proxies_list = []
            r = requests.get(url=url)
            res = r.text

            if 'You reached the maximum 50 requests for today.' in res:
                url = 'https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt'
                self.log.debug(f'Switched proxy database to {url}')

                r = requests.get(url=url)
                res = r.text

            for lines in res.split('\n'):
                proxy = ''.join(lines)
                proxies_list.append(proxy)

            proxies = {'last-updated': str(datetime.now()), 'list': proxies_list}

            await self.config.proxies.set(proxies)
            self.log.debug(f"Proxies list updated: {proxies_list}")
        else:
            self.log.debug(f"Proxies list update skipped..")

        if truncate:
            try:
                proxies['list'].remove(self.api.proxy)
                await self.config.proxies.set(proxies)
                self.log.debug(f"Removed from proxies list: {self.api.proxy}")
            except ValueError:
                pass

        self.api.proxy = next(iter(proxies['list']))
        self.log.debug(f"New proxy acquired: {self.api.proxy}")
        await self.config.proxy.set(self.api.proxy)

    async def get_new_videos(self):
        for guild in self.bot.guilds:
            try:
                subs = await self.config.guild(guild).subscriptions()
                cache = await self.config.guild(guild).cache()
            except:
                self.log.debug("Unable to fetch data, config is empty..")
                return
            for i, sub in enumerate(subs):
                self.log.debug(f"Fetching data of {sub['id']} from guild channel: {sub['channel']['name']}")
                channel = self.bot.get_channel(int(sub["channel"]["id"]))
                while True:
                    try:
                        task = functools.partial(self.get_tiktok_by_name, sub["id"], 3)
                        task = self.bot.loop.run_in_executor(None, task)
                        tiktoks = await asyncio.wait_for(task, timeout=30)
                    except TimeoutError:
                        self.log.error("Takes too long, retrying..")
                        await self.get_new_proxy(await self.config.proxies(), True)
                        continue
                    except TikTokCaptchaError:
                        self.log.error("Captcha error, retrying..")
                        await self.get_new_proxy(await self.config.proxies(), True)
                        continue
                    except ConnectionError as e:
                        self.log.error(f"Connection error, retrying: {str(e)}")
                        await self.get_new_proxy(await self.config.proxies(), True)
                        continue
                    else:
                        break

                self.log.debug("Response: " + str(tiktoks))
                if not channel:
                    self.log.debug("Channel not found: " + sub["channel"]["name"])
                    continue

                if tiktoks is None:
                    continue

                for post in tiktoks:
                    self.log.debug("Post ID: " + post["id"])
                    if not post["id"] in cache:
                        gif = True
                        try:
                            self.log.debug("Sending data to channel: " + sub["channel"]["name"])
                            task = functools.partial(self.get_tiktok_dynamic_cover, post)
                            task = self.bot.loop.run_in_executor(None, task)
                            cover_file = await asyncio.wait_for(task, timeout=60)
                        except TimeoutError:
                            gif = False
                            self.log.warning("GIF processing too long..")
                        finally:
                            color = int(hex(int(ColorHash(post['author']['uniqueId']).hex.replace("#", ""), 16)), 0)
                            self.log.debug("Unique color: " + str(color))

                            # Send embed and post in channel
                            embed = discord.Embed(color=color, url=f"https://www.tiktok.com/@{post['author']['uniqueId']}/video/{post['id']}")
                            embed.timestamp = datetime.utcfromtimestamp(post['createTime'])
                            embed.description = re.sub(r'#(\w+)', r'[#\1](https://www.tiktok.com/tag/\1)', f"{post['desc']}")
                            embed.add_field(name=f"<:music:845585013327265822> {post['music']['title']} - {post['music']['authorName']}", value=f"[Click to see full video!](https://www.tiktok.com/@{post['author']['uniqueId']}/video/{post['id']})", inline=False)
                            embed.set_author(name=post['author']['nickname'], url=f"https://www.tiktok.com/@{post['author']['uniqueId']}", icon_url=post['author']['avatarMedium'])
                            self.log.debug("Arranging embed..")

                            if not gif:
                                cover_file = None
                                embed.set_image(url=post['video']['cover'])
                                self.log.debug(f"Cover link: {post['video']['cover']}")
                            else:
                                embed.set_image(url=f"attachment://{post['id']}.gif")

                            self.log.debug("Sending to channel..")
                            await self.bot.get_channel(sub["channel"]["id"]).send(embed=embed, file=cover_file)

                            # Add id to published cache
                            cache.append(post["id"])
                            await self.config.guild(guild).cache.set(cache)
                            self.log.debug("Saved cache data: " + str(cache))
                    else:
                        self.log.debug("Skipping..")

                self.log.debug("Sleeping 5 seconds..")
                await asyncio.sleep(5)

    async def background_get_new_videos(self):
        await self.bot.wait_until_red_ready()
        while True:
            await self.get_new_videos()
            interval = await self.config.interval()
            self.log.debug(f"Sleeping {interval} seconds..")
            await asyncio.sleep(interval)

    def cog_unload(self):
        self.log.debug("Shutting down TikTok service..")
        self.background_task.cancel()
        self.main_task.cancel()

    @commands.group()
    @commands.guild_only()
    async def tiktok(self: commands.Cog, ctx: commands.Context) -> None:
        """
        Role tools commands
        """
        pass

    @tiktok.command()
    @commands.guild_only()
    @checks.admin_or_permissions(manage_guild=True)
    async def add(self, ctx, tiktokId, channelDiscord: discord.TextChannel = None):
        """Subscribe a Discord channel to a TikTok Channel

        If no discord channel is specified, the current channel will be subscribed
        """
        if not channelDiscord:
            channelDiscord = ctx.channel

        subs = await self.config.guild(ctx.guild).subscriptions()
        newSub = {'id': tiktokId,
                  'channel': {"name": channelDiscord.name,
                              "id": channelDiscord.id}}

        subs.append(newSub)
        await self.config.guild(ctx.guild).subscriptions.set(subs)

        color = int(hex(int(ColorHash(tiktokId).hex.replace("#", ""), 16)), 0)
        embed = discord.Embed(color=color)
        embed.description = f'TikTok feeds of user [{tiktokId}](https://www.tiktok.com/@{tiktokId}) added to <#{channelDiscord.id}>.'
        await ctx.send(embed=embed)

    @tiktok.command()
    @commands.guild_only()
    @checks.admin_or_permissions(manage_guild=True)
    async def remove(self, ctx: commands.Context, tiktokId, channelDiscord: discord.TextChannel = None):
        """Unsubscribe a Discord channel from a TikTok channel

        If no Discord channel is specified, the subscription will be removed from all channels"""
        subs = await self.config.guild(ctx.guild).subscriptions()
        unsubbed = []
        if channelDiscord:
            newSub = {'id': tiktokId,
                      'channel': {"name": channelDiscord.name,
                                  "id": channelDiscord.id}}

            for i, sub in enumerate(subs):
                if sub['uid'] == newSub['uid']:
                    unsubbed.append(subs.pop(i))
                    break
            else:
                await ctx.send("Subscription not found")
                return
        else:
            for i, sub in enumerate(subs):
                if sub['id'] == tiktokId:
                    unsubbed.append(subs.pop(i))
            if not len(unsubbed):
                await ctx.send("Subscription not found")
                return
        await self.config.guild(ctx.guild).subscriptions.set(subs)

        channels = f'<#{channelDiscord.id}>' if channelDiscord else 'all channels'
        color = int(hex(int(ColorHash(tiktokId).hex.replace("#", ""), 16)), 0)
        embed = discord.Embed(color=color)
        embed.description = f'TikTok feeds of user [{tiktokId}](https://www.tiktok.com/@{tiktokId}) no longer be subscriped to {channels}'
        await ctx.send(embed=embed)

    @tiktok.command()
    @checks.admin_or_permissions(manage_guild=True)
    @commands.guild_only()
    async def clear(self, ctx):
        """Clear cached tiktok posts"""
        await self.config.guild(ctx.guild).cache.set([])

    @tiktok.command()
    @checks.is_owner()
    async def resetproxy(self, ctx):
        """Clear proxies database"""
        await self.config.proxies.set([])
        await ctx.send("Proxy database cleared!")

    @tiktok.command()
    @checks.is_owner()
    async def update(self, ctx):
        """Manually force feed update"""
        await self.get_new_videos()

    @tiktok.command()
    @checks.is_owner()
    async def setinterval(self, ctx: commands.Context, interval: int):
        """Set the interval in seconds at which to check for updates

        Very low values will probably get you rate limited"""
        await self.config.interval.set(interval)
        await ctx.send(f"Interval set to {await self.config.interval()}")

    @commands.guild_only()
    @tiktok.command()
    async def list(self, ctx: commands.Context):
        """List current subscriptions"""
        await self._showsubs(ctx, ctx.guild)

    async def _showsubs(self, ctx: commands.Context, guild: discord.Guild):
        subs = await self.conf.guild(guild).subscriptions()
        if not len(subs):
            await ctx.send("No subscriptions yet - try adding some!")
            return
        subs_by_channel = {}
        for sub in subs:
            # Channel entry must be max 124 chars: 103 + 2 + 18 + 1
            channel = f'{sub["channel"]["name"][:103]} ({sub["channel"]["id"]})' # Max 124 chars
            subs_by_channel[channel] = [
                # Sub entry must be max 100 chars: 45 + 2 + 24 + 4 + 25 = 100
                f"{sub.get('name', sub['id'][:45])} ({sub['id']}) - {sub.get('previous', 'Never')}",
                # Preserve previous entries
                *subs_by_channel.get(channel, [])
            ]
        if ctx.channel.permissions_for(guild.me).embed_links:
            for channel, sub_ids in subs_by_channel.items():
                page_count = (len(sub_ids) // 9) + 1
                page = 1
                while len(sub_ids) > 0:
                    # Generate embed with max 1024 chars
                    embed = discord.Embed()
                    title = f"Tiktok Subscriptions for {channel}"
                    embed.description = "\n".join(sub_ids[0:9])
                    if page_count > 1:
                        title += f" ({page}/{page_count})"
                        page += 1
                    embed.title = title
                    await ctx.send(embed=embed)
                    del(sub_ids[0:9])
        else:
            subs_string = ""
            for channel, sub_ids in subs_by_channel.items():
                subs_string += f"\n\n{channel}"
                for sub in sub_ids:
                    subs_string += f"\n{sub}"
            pages = pagify(subs_string, delims=["\n\n"], shorten_by=12)
            for i, page in enumerate(pages):
                title = "**Tiktok Subscriptions**"
                if len(pages) > 1:
                    title += f" ({i}/{len(pages)})"
                await ctx.send(f"{title}\n{page}")

    @tiktok.command()
    @checks.is_owner()
    async def setproxy(self, ctx: commands.Context, proxy):
        """Manually set HTTP proxy address"""
        self.api.proxy = proxy

        await self.config.proxy.set(proxy)
        await ctx.send(f"Proxy set to {await self.config.proxy()}")
        self.log.debug(f"Proxy set to {await self.config.proxy()}")