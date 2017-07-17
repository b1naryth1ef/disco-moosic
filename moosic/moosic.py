from __future__ import print_function

import gevent
import tempfile
import humanize

from datetime import timedelta

from disco.bot import Plugin, Config
from disco.types.user import Status, GameType, Game
from disco.types.message import MessageEmbed
from disco.bot.command import CommandError

from disco.voice import (
    Player, VoiceException, YoutubeDLInput, OpusFilePlayable, BufferedOpusEncoderPlayable,
    FileProxyPlayable
)
from disco.voice.queue import PlayableQueue

from .cache import LRUDiskCache


NEXT_TRACK = u'\U000023ED'
PLAY_PAUSE = u'\U000023EF'
SHUFFLE = u'\U0001F500'
CLEAR = u'\U0001F5D1'
STOP = u'\U0000274C'

ALL_EMOJIS = (STOP, PLAY_PAUSE, NEXT_TRACK, SHUFFLE, CLEAR)


class MusicQueue(PlayableQueue):
    def __init__(self, parent, on_next=None):
        super(MusicQueue, self).__init__()
        self.parent = parent
        self._on_next = on_next

    def get(self):
        item = super(MusicQueue, self)._get()

        item = item.pipe(BufferedOpusEncoderPlayable)

        if self._on_next and callable(self._on_next):
            gevent.spawn(self._on_next, item)

        if self.parent.cache:
            # If the item exists in our cache, just replace the playable entirely
            #  with it.
            if self.parent.cache.has(item.metadata['id']):
                self.parent.log.info('[CACHE] item %s exists in cache, playing from it', item.metadata['id'])
                new_item = OpusFilePlayable(self.parent.cache.get(item.metadata['id']))
                new_item.metadata = item.metadata
                item = new_item
            else:
                tf = tempfile.NamedTemporaryFile()

                # To prevent songs that get skipped, or whose playback is not
                #  fully completed from being cached, we first write the output
                #  to a tempfile, and then observe the `on_complete` event, which
                #  will tell us if the playback FULL completed (or appeard to at
                #  least, there are still edge cases where something breaks).
                def commit():
                    self.parent.log.info('[CACHE] storing completed item %s', item.metadata['id'])
                    self.parent.cache.put_from_path(item.metadata['id'], tf.name)

                item = item.pipe(FileProxyPlayable, tf, on_complete=commit)
        return item


class ChannelPlayer(object):
    def __init__(self, parent, client, channel):
        self.parent = parent
        self.channel = channel
        self.queue = MusicQueue(self.parent, on_next=self.on_next)

        self._player = Player(client, queue=self.queue)
        self._message = None
        self._listener = self.channel.client.events.on(
            'MessageReactionAdd', self.on_reaction_add, conditional=self.is_relevant_reaction
        )
        self._player.events.on(self._player.Events.DISCONNECT, self.on_disconnect)

    def __del__(self):
        # Explicitly remove the event listener
        self._listener.remove()

    def is_relevant_reaction(self, event):
        return (
            event.channel_id == self.channel.id and
            event.emoji.name in ALL_EMOJIS and
            event.user_id != self.channel.client.state.me.id and
            event.message_id == self._message.id
        )

    def on_reaction_add(self, event):
        self._message.async_chain().delete_reaction(event.emoji.name, event.user_id)

        if event.emoji.name == NEXT_TRACK:
            self._player.skip()
        elif event.emoji.name == PLAY_PAUSE:
            self._player.resume() if self._player.paused else self._player.pause()
        elif event.emoji.name == SHUFFLE:
            self.queue.shuffle()
        elif event.emoji.name == CLEAR:
            self.queue.clear()
        elif event.emoji.name == STOP:
            self._player.disconnect()

    def on_disconnect(self):
        del self.parent.guilds[self.channel.guild.id]
        del self

    def on_next(self, item):
        embed = self._get_embed_for_item(item.metadata)

        if self._message:
            self._message.edit(embed=embed)
        else:
            self._message = self.channel.send_message(embed=embed)
            gevent.spawn(lambda: [self._message.add_reaction(reaction) for reaction in ALL_EMOJIS])

        if len(self.parent.guilds) == 1:
            self.parent.client.update_presence(
                Status.ONLINE,
                Game(type=GameType.DEFAULT, name=item.metadata['title']),
            )

    def _get_embed_for_item(self, info):
        embed = MessageEmbed()
        embed.title = u'{}'.format(info['title'])
        embed.url = info['webpage_url']
        embed.color = 0x77dd77
        embed.set_image(url=info['thumbnail'])
        embed.add_field(name='Uploader', value=info['uploader'])

        if 'view_count' in info:
            embed.add_field(name='Views', value=info['view_count'])

        if 'duration' in info:
            embed.add_field(name='Duration', value=humanize.naturaldelta(timedelta(seconds=info['duration'])))

        return embed


class MoosicPluginConfig(Config):
    cache_enabled = True
    cache_folder = 'cache'
    cache_max_size = '1G'


@Plugin.with_config(MoosicPluginConfig)
class MoosicPlugin(Plugin):
    def load(self, ctx):
        super(MoosicPlugin, self).load(ctx)
        self.guilds = ctx.get('guilds', {})

        self.cache = None
        if self.config.cache_enabled:
            self.cache = LRUDiskCache(self.config.cache_folder, self.config.cache_max_size)

    def unload(self, ctx):
        ctx['guilds'] = self.guilds
        super(MoosicPlugin, self).unload(ctx)

    @Plugin.command('join', '[channel:channel]')
    def cmd_join(self, event, channel=None):
        if event.guild.id in self.guilds:
            return event.msg.reply("I'm already playing music in this server!")

        if not channel:
            state = event.guild.get_member(event.author).get_voice_state()
            if not state:
                return event.msg.reply('Invalid channel!')

            channel = state.channel

        self.log.info('Connecting to channel %s', channel)

        try:
            client = channel.connect()
        except VoiceException as e:
            self.log.exception('Failed to connect to %s: ', channel)
            return event.msg.reply('Failed to connect to voice: `{}`'.format(e))

        self.log.info('Connected to channel %s', channel)
        self.guilds[event.guild.id] = ChannelPlayer(self, client, event.channel)

    def get_state(self, event):
        if event.guild.id not in self.guilds:
            raise CommandError("I'm not currently playing music here.")
        return self.guilds[event.guild.id]

    @Plugin.command('play', '<url:str>')
    def cmd_play(self, event, url):
        player = self.get_state(event)

        event.msg.delete()

        msg = event.msg.reply(':alarm_clock: ok hold on while I grab some information on that url...')

        try:
            items = list(YoutubeDLInput.many(url))
        except:
            msg.edit(':warning: whelp I really tried my hardest but I couldn\'t load any music from that url!')
            raise
            return

        msg.edit(':ok_hand: adding {} items to the playlist'.format(len(items))).after(5).delete()

        for item in items:
            print('adding item')
            player.queue.append(item)
