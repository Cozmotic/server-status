import asyncio
import json

import discord
import requests

import os

intents = discord.Intents.default()
client = discord.Client(intents=intents)

id2 = "95338"

refresh = 30

TOKEN = os.getenv("THEATORS_BOT_TOKEN")

@client.event
async def on_ready():

  print(f"{client.user} is now online")

  while True:
    s = requests.get(f'https://api.scplist.kr/api/servers/{id2}').text
    data = json.loads(s)

    player_count = data['players']

    sl_pc = int(player_count.split('/')[0])
    
    if sl_pc == 0:
      await client.change_presence(activity=discord.CustomActivity(name=f"Online: {player_count}"),
                                   status=discord.Status.idle)
    elif sl_pc >= int(player_count.split('/')[1]):
      await client.change_presence(activity=discord.CustomActivity(name=f"Online: {player_count}"),
                                   status=discord.Status.dnd)
    else:
      await client.change_presence(activity=discord.CustomActivity(name=f"Online: {player_count}"),
                                   status=discord.Status.online)

    await asyncio.sleep(refresh)

client.run(TOKEN)

