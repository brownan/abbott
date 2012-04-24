from ..command import CommandPluginSuperclass

class PluginController(CommandPluginSuperclass):

    def start(self):
        super(PluginController, self).start()

        self.install_command(r"plugins? load (?P<plugin>[\w.]+)$",
                "plugin.control",
                self.load_plugin)
        self.help_msg("plugins? load",
                "plugin.control",
                "'plugin load <plugin name>' Loads the given plugin. Assumes format modulename.classname for some module in the plugins package")

        self.install_command(r"plugins? (unload|remove) (?P<plugin>[\w.]+)$",
                "plugin.control",
                self.unload_plugin)
        self.help_msg("plugins? (unload|remove)",
                "plugin.control",
                "'plugin unload <plugin name>' Unloads the given plugin")

        self.install_command(r"plugins? reload (?P<plugin>[\w.]+)$",
                "plugin.control",
                self.reload_plugin)
        self.help_msg("plugins? reload",
                "plugin.control",
                "'plugin reload <plugin name>' Reloads the plugin's module and starts it, unloading first if necessary")

        self.install_command(r"plugins? reloadall$",
                "plugin.control",
                self.reload_all)
        self.help_msg("plugins? reloadall",
                "plugin.control",
                "'plugin reloadall' reloads all plugins except the IRC plugin",
                )

        self.install_command(r"plugins? chkconfig on (?P<plugin>[\w.]+)$",
                "plugin.control",
                self.set_on_startup)
        self.install_command(r"plugins? chkconfig off (?P<plugin>[\w.]+)$",
                "plugin.control",
                self.remove_from_startup)
        self.help_msg("plugins? chkconfig",
                "plugin.control",
                "'plugin chkconfig (on|off) <plugin name>' Adds or removes the plugin from the startup configuration")


        self.install_command(r"plugins? list$",
                None,
                self.list_plugins)
        self.help_msg("plugins? list",
                None,
                "'plugin list' Lists all currently loaded plugins")

        self.help_msg("plugin",
                None,
                "'plugin <command> [options]' Possible plugin commands: load, unload, reload, reloadall, chkconfig on, chkconfig off, list")

        self.define_command("plugin")

    def load_plugin(self, event, match):
        plugin_name = match.groupdict()['plugin']
        if plugin_name in self.pluginboss.loaded_plugins:
            event.reply("Plugin %s is already loaded and running." % plugin_name)
            return

        try:
            self.pluginboss.load_plugin(plugin_name)
        except Exception:
            event.reply("Something went wrong loading the plugin. Check the error log for a traceback")
            raise
        event.reply("Plugin %s has been loaded." % plugin_name)

    def unload_plugin(self, event, match):
        plugin_name = match.groupdict()['plugin']

        if plugin_name not in self.pluginboss.loaded_plugins:
            event.reply("Plugin %s is not loaded." % plugin_name)
            return

        try:
            self.pluginboss.unload_plugin(plugin_name)
        except Exception:
            event.reply("Something went wrong unloading the plugin. Check the error log for a traceback")
            raise
        event.reply("Plugin %s has been unloaded." % plugin_name)

    def reload_plugin(self, event, match):
        plugin_name = match.groupdict()['plugin']

        if plugin_name in self.pluginboss.loaded_plugins:

            try:
                self.pluginboss.unload_plugin(plugin_name)
            except Exception:
                event.reply("Something went wrong unloading the plugin. Check the error log for a traceback")
                raise

        try:
            self.pluginboss.load_plugin(plugin_name, reload_first=True)
        except Exception:
            event.reply("Something went wrong reloading the plugin. Check the error log for a traceback")
            raise

        event.reply("Plugin %s reloaded and running" % plugin_name)

    def reload_all(self, event, match):
        for plugin_name in self.pluginboss.loaded_plugins.keys():
            if plugin_name == "irc.IRCBotPlugin":
                continue

            try:
                self.pluginboss.unload_plugin(plugin_name)
            except Exception:
                event.reply("Something went wrong unloading %s. Check the error log for a traceback" % plugin_name)
                log.err("Error unloading %s" % plugin_name)

            try:
                self.pluginboss.load_plugin(plugin_name, reload_first=True)
            except Exception:
                event.reply("Something went wrong reloading %s. Check the error log for a traceback" % plugin_name)
                log.err("Error reloading %s" % plugin_name)
        event.reply("Done")



    def set_on_startup(self, event, match):
        plugin_name = match.groupdict()['plugin']

        pluginlist = self.pluginboss.config['core']['plugins']
        if plugin_name in pluginlist:
            event.reply("The plugin %s is already set to start on startup" % plugin_name)
            return

        if plugin_name not in self.pluginboss.loaded_plugins:
            event.reply("This plugin is not currently loaded. Please load it first")
            return

        pluginlist.append(plugin_name)
        self.pluginboss.save()
        event.reply("Plugin %s added to plugins to launch on boot" % plugin_name)

    def remove_from_startup(self, event, match):
        plugin_name = match.groupdict()['plugin']

        pluginlist = self.pluginboss.config['core']['plugins']
        if plugin_name not in pluginlist:
            event.reply("The plugin %s not in the startup list" % plugin_name)
            return

        pluginlist.remove(plugin_name)
        self.pluginboss.save()
        event.reply("Plugin %s removed from startup list" % plugin_name)

    def list_plugins(self, event, match):
        plugins = self.pluginboss.loaded_plugins.keys()

        plugins.sort()
        event.reply("Plugins currently running: %s" % ", ".join(plugins))
