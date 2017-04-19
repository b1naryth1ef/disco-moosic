from __future__ import print_function

import gevent
import random
import tempfile
import humanize

from datetime import timedelta
from gevent.event import Event

from disco.bot import Plugin, Config
from disco.types.user import Status, GameType, Game
from disco.types.message import MessageEmbed
from disco.bot.command import CommandError

from disco.voice import (
    Player, VoiceException, YoutubeDLInput, OpusFilePlayable, BufferedOpusEncoderPlayable,
    FileProxyPlayable, BasePlayable, AbstractOpus
)

from .cache import LRUDiskCache


NEXT_TRACK = u'\U000023ED'
PLAY_PAUSE = u'\U000023EF'
SHUFFLE = u'\U0001F500'


class MusicQueuePlayable(BasePlayable, AbstractOpus):
    def __init__(self, parent, player, channel, *args, **kwargs):
        super(MusicQueuePlayable, self).__init__(*args, **kwargs)
        self.parent = parent
        self.player = player
        self.channel = channel
        self.msg = None
        self._current = None
        self._entries = []
        self._event = None

        def reaction_matcher(e):
            return (
                e.channel_id == self.channel.id and
                e.emoji.name in (NEXT_TRACK, PLAY_PAUSE, SHUFFLE) and
                e.user_id != self.channel.client.state.me.id)

        self._listener = self.channel.client.events.on(
            'MessageReactionAdd', self._on_reaction, conditional=reaction_matcher)

    def __del__(self):
        self._listener.remove()

    def _on_reaction(self, event):
        if event.emoji.name == NEXT_TRACK:
            self.skip()
        elif event.emoji.name == PLAY_PAUSE:
            if self.player.paused:
                self.player.resume()
            else:
                self.player.pause()
        elif event.emoji.name == SHUFFLE:
            self.shuffle()

        self.msg.delete_reaction(event.emoji.name, event.user_id)

    def _on_play(self, info):
        # If only one guild is playing music, update our status
        if len(self.parent.guilds) == 1:
            self.parent.client.update_presence(
                Status.ONLINE,
                Game(
                    type=GameType.DEFAULT,
                    name=info['title'],
                ))

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

        if not self.msg:
            self.msg = self.channel.send_message(embed=embed)

            # Do this in a greenlet because its fairly heavily rate-limited
            def add_reactions():
                self.msg.create_reaction(PLAY_PAUSE)
                self.msg.create_reaction(NEXT_TRACK)
                self.msg.create_reaction(SHUFFLE)
            gevent.spawn(add_reactions)
        else:
            self.msg.edit(embed=embed)

    def shuffle(self):
        random.shuffle(self._entries)

    def add(self, item):
        self._entries.append(item)
        if self._event:
            self._event.set()

    def skip(self):
        self._current = None

    def _get_next_playable(self):
        item = self._entries.pop(0)

        # Pipe the item into the encoder
        item = item.pipe(BufferedOpusEncoderPlayable)

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

    @property
    def now_playing(self):
        if self._current:
            return self._current

        if not self._entries:
            return

        try:
            self._current = self._get_next_playable()
        except:
            self.parent.log.exception('Failed to get a playble, retrying')
            return self.now_playing

        self._on_play(self._current.metadata)
        return self._current

    def next_frame(self):
        if self.now_playing:
            frame = self.now_playing.next_frame()
            if frame:
                return frame
            self._current = None
            return self.next_frame()

        self._event = Event()
        self._event.wait()
        self._event = None
        return self.next_frame()


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
        player = Player(client)
        self.guilds[event.guild.id] = MusicQueuePlayable(self, player, event.channel)
        player.play(self.guilds[event.guild.id])
        self.log.info('Player completed in channel %s', channel)
        del self.guilds[event.guild.id]

    def get_state(self, event):
        if event.guild.id not in self.guilds:
            raise CommandError("I'm not currently playing music here.")
        return self.guilds[event.guild.id]

    @Plugin.command('leave')
    def cmd_leave(self, event):
        queue = self.get_state(event)
        queue.player.disconnect()

    @Plugin.command('play', '<url:str>')
    def cmd_play(self, event, url):
        queue = self.get_state(event)

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
            queue.add(item)
