from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from datetime import datetime
import redis
import osu
import discord
import dotenv
import os
import secrets
import logging
import asyncio
import sys

dotenv.load_dotenv(override=True)

redis_client = redis.asyncio.Redis(host=os.getenv('REDIS_HOST'), port=int(os.getenv('REDIS_PORT')), db=int(os.getenv('REDIS_DB')))
discord_client = discord.Client(intents=discord.Intents.default())
app = FastAPI()

logging.basicConfig(
    level=logging.DEBUG if "--debug" in sys.argv else logging.INFO,
    format='%(asctime)s - %(name)s [%(levelname)s]: %(message)s'
)
log = logging.getLogger(__name__)

OSU_CLIENT_ID = int(os.getenv('OSU_CLIENT_ID'))
OSU_CLIENT_SECRET = os.getenv('OSU_CLIENT_SECRET')
OSU_REDIRECT_URI = os.getenv('OSU_REDIRECT_URI')
SERVER_ID = int(os.getenv('SERVER_ID'))
CHANNEL_ID = int(os.getenv('CHANNEL_ID'))
ROLE_ID = int(os.getenv('ROLE_ID'))
JOIN_DATE = datetime.fromisoformat(os.getenv("JOIN_DATE"))


def get_auth_handler():
    return osu.AsynchronousAuthHandler(OSU_CLIENT_ID, OSU_CLIENT_SECRET, OSU_REDIRECT_URI, osu.Scope.identify())


def get_auth_url(identifier: str):
    return get_auth_handler().get_auth_url(identifier)


async def get_osu_user(code: str):
    auth = get_auth_handler()
    await auth.get_auth_token(code)
    client = osu.AsynchronousClient(auth)
    return await client.get_own_data()


class VerifyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.primary, emoji="✅")
    async def verify_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        identifier = secrets.token_urlsafe(32)
        await redis_client.set(identifier, str(interaction.user.id), ex=60 * 10)
        url = get_auth_url(identifier)

        await interaction.response.send_message(f"Go to {url} (expires after 10 minutes)", ephemeral=True)


def create_verify_msg_embed():
    return discord.Embed(
        title="Verify to access channels",
        description="Press the verify button and you will be sent a login link",
        color=discord.Color.green()
    )


@discord_client.event
async def on_ready():
    log.info("Bot is online")

    verify_msg = await redis_client.hgetall("verify_message")
    if verify_msg.get(b"message_id") and verify_msg.get(b"channel_id"):
        try:
            channel_id = int(verify_msg[b"channel_id"].decode())
            channel = discord_client.get_channel(channel_id) or (await discord_client.fetch_channel(channel_id))
            msg = await channel.fetch_message(int(verify_msg[b"message_id"].decode()))
            await msg.edit(embed=create_verify_msg_embed(), view=VerifyView())
            log.info("Found existing message")
            return
        except discord.NotFound:
            log.info("Message not found, creating new one")

    view = VerifyView()
    channel = discord_client.get_channel(CHANNEL_ID) or await discord_client.fetch_channel(CHANNEL_ID)
    msg = await channel.send(embed=create_verify_msg_embed(), view=view)
    await redis_client.hset("verify_message", mapping={
        "message_id": msg.id,
        "channel_id": channel.id,
    })
    log.info("Created new message")


@app.get("/")
async def index(code: str = "", state: str = ""):
    if code == "" or state == "":
        return HTMLResponse(b"Hi")

    discord_user_id = await redis_client.get(state)
    if discord_user_id is None:
        return HTMLResponse(b"Hi")
    discord_user_id = int(discord_user_id)

    try:
        osu_user = await get_osu_user(code)
    except:
        return HTMLResponse(b"Invalid code")

    if osu_user.is_restricted:
        return HTMLResponse(b"Restricted users cannot gain access")
    if osu_user.join_date >= JOIN_DATE:
        return HTMLResponse(b"Your account is too recent to gain access")

    guild = discord_client.get_guild(SERVER_ID)
    role = guild.get_role(ROLE_ID)
    member = await guild.fetch_member(discord_user_id)
    await member.add_roles(role)
    try:
        await member.edit(nick=osu_user.username)
    except discord.errors.Forbidden:
        log.info("Unable to change nickname, ignoring")
    await redis_client.delete(state)

    return HTMLResponse(b"You have been given access")


async def main():
    import uvicorn

    config = uvicorn.Config(app, host=os.getenv("SERVER_HOST"), port=int(os.getenv("SERVER_PORT")))
    server = uvicorn.Server(config)

    await asyncio.gather(
        discord_client.start(os.getenv("DISCORD_TOKEN")),
        server.serve()
    )


if __name__ == '__main__':
    loop = asyncio.new_event_loop()

    try:
        loop.run_until_complete(main())
    except BaseException as exc:
        log.exception("Closing on exception", exc_info=exc)
    finally:
        loop.run_until_complete(discord_client.close())
        loop.close()
