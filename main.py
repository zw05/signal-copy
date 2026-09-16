import asyncio
import logging
import os

from dotenv import load_dotenv

from database import get_message, init_db, insert_message
from pipeline import process_message


load_dotenv()
DISCORD_MODE = os.getenv('DISCORD_MODE', 'bot').strip().lower()
SIGNAL_CHANNEL_ID = os.getenv('SIGNAL_CHANNEL_ID')
RUNNER_ENABLED = os.getenv('RUNNER_ENABLED', 'false').strip().lower() == 'true'
RUNNER_INTERVAL = float(os.getenv('RUNNER_INTERVAL_SECONDS', '15'))
NOTIFY_CHANNEL_ID = os.getenv('NOTIFY_CHANNEL_ID')

if DISCORD_MODE == 'user':
    import selfcord as discord

    discord_token = os.getenv('DISCORD_USER_TOKEN')
    client = discord.Client()
elif DISCORD_MODE == 'bot':
    import discord

    discord_token = os.getenv('DISCORD_BOT_TOKEN') or os.getenv('DISCORD_TOKEN')
    intents = discord.Intents.default()
    intents.members = True
    intents.message_content = True
    client = discord.Client(intents=intents)
else:
    raise ValueError("DISCORD_MODE must be either 'bot' or 'user'")

if not discord_token:
    token_name = (
        'DISCORD_USER_TOKEN'
        if DISCORD_MODE == 'user'
        else 'DISCORD_BOT_TOKEN'
    )
    raise RuntimeError(f'{token_name} is not configured')

handler = logging.FileHandler(filename='stockbot.log', encoding='utf-8', mode='w')


def is_signal_message(message):
    if SIGNAL_CHANNEL_ID and str(message.channel.id) != SIGNAL_CHANNEL_ID:
        return False
    return True


def store_signal_message(message):
    if not is_signal_message(message):
        return

    reply_to_id = message.reference.message_id if message.reference else None
    guild_id = message.guild.id if message.guild else None
    attachment_urls = ','.join(a.url for a in message.attachments) or None

    message_id, inserted = insert_message(
        discord_id=message.id,
        guild_id=guild_id,
        channel_id=message.channel.id,
        author_id=message.author.id,
        author_name=str(message.author),
        content=message.content,
        reply_to_id=reply_to_id,
        created_at=message.created_at.isoformat(),
        attachment_urls=attachment_urls,
    )

    if inserted and message_id:
        result = process_message(get_message(message_id))
        if result['status'] != 'ignored':
            logging.getLogger('stockbot').info(
                'message %s -> %s', message_id, result,
            )


_runner_task = None


async def _notify(text):
    if not NOTIFY_CHANNEL_ID:
        return
    channel = client.get_channel(int(NOTIFY_CHANNEL_ID))
    if channel is None:
        return
    try:
        await channel.send(text[:1900])
    except Exception:  # noqa: BLE001
        logging.getLogger('stockbot').exception('notify failed')


async def _runner_loop():
    from execution.alpaca import AlpacaPaperBroker
    from rules import Rules
    from runner import Runner

    rules = Rules.from_env()
    runner = Runner(AlpacaPaperBroker(), rules)
    log = logging.getLogger('stockbot.runner')
    log.info('runner started: %s', rules)
    await _notify(f'runner online (paper) - {rules.contracts_per_entry} contracts/entry, '
                  f'max loss ${rules.max_daily_loss:g}')
    while not client.is_closed():
        try:
            events = await asyncio.to_thread(runner.tick)
        except Exception:  # noqa: BLE001
            log.exception('runner tick crashed')
            events = ['ERROR: runner tick crashed, see log']
        if events:
            await _notify('\n'.join(events))
        if runner.halted:
            await _notify(f'runner HALTED. Remove the {rules.kill_switch_file} file and restart to resume.')
            return
        await asyncio.sleep(RUNNER_INTERVAL)


@client.event
async def on_ready():
    global _runner_task
    init_db()
    print(f'{client.user} is ready in {DISCORD_MODE} mode.')
    if RUNNER_ENABLED and _runner_task is None:
        _runner_task = asyncio.create_task(_runner_loop())


@client.event
async def on_message(message):
    if message.author == client.user:
        return

    store_signal_message(message)


client.run(discord_token, log_handler=handler, log_level=logging.DEBUG)
