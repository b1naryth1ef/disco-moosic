from __future__ import print_function

from disco.bot import Plugin, Config
from disco.types.message import MessageEmbed
from disco.bot.command import CommandLevels, CommandError
from disco.voice import Player, VoiceException, create_youtube_dl_playable
from disco.voice.opus import DCADOpusEncoder, BufferedOpusEncoder


class MusicState(object):
    def __init__(self, text_channel, voice_channel, player):
        self.guild = text_channel.guild
        self.text_channel = text_channel
        self.voice_channel = voice_channel
        self.player = player
        self.player.events.on(Player.Events.START_PLAY, self.on_play)

    def on_play(self, playable):
        embed = MessageEmbed()
        embed.title = u'Now Playing - {}'.format(playable.info['title'])
        embed.color = 0x77dd77
        embed.set_image(url=playable.info['thumbnail'])
        self.text_channel.send_message('', embed=embed)

    def wait(self):
        return self.player.complete.wait()


class MoosicPluginConfig(Config):
    use_dcad = True


@Plugin.with_config(MoosicPluginConfig)
class MoosicPlugin(Plugin):
    def load(self, ctx):
        super(MoosicPlugin, self).load(ctx)
        self.guilds = ctx.get('guilds', {})

    def unload(self, ctx):
        ctx['guilds'] = self.guilds
        super(MoosicPlugin, self).unload(ctx)

    @Plugin.command('join', '[channel:channel]', level=CommandLevels.TRUSTED)
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
        self.guilds[event.guild.id] = MusicState(event.channel, channel, Player(client))
        self.guilds[event.guild.id].wait()
        self.log.info('Player completed in channel %s', channel)
        del self.guilds[event.guild.id]

    def get_state(self, event):
        if event.guild.id not in self.guilds:
            raise CommandError("I'm not currently playing music here.")
        return self.guilds[event.guild.id]

    @Plugin.command('leave', level=CommandLevels.TRUSTED)
    def cmd_leave(self, event):
        state = self.get_state(event)
        state.player.disconnect()

    @Plugin.command('play', '<url:str>')
    def cmd_play(self, event, url):
        state = self.get_state(event)

        if self.config.use_dcad:
            cls = DCADOpusEncoder
        else:
            cls = BufferedOpusEncoder

        playable = create_youtube_dl_playable(url, cls=cls)
        state.player.queue.put(playable)

    @Plugin.command('skip')
    def cmd_skip(self, event):
        state = self.get_state(event)
        state.player.skip()

    @Plugin.command('pause')
    def cmd_pause(self, event):
        state = self.get_state(event)
        state.player.pause()

    @Plugin.command('resume')
    def cmd_resume(self, event):
        state = self.get_state(event)
        state.player.resume()

    @Plugin.command('list')
    def cmd_list(self, event):
        pass
