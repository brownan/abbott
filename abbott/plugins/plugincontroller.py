from ..command import CommandPluginSuperclass

class PluginController(CommandPluginSuperclass):

    def start(self):
        super(PluginController, self).start()

        plugingroup = self.install_cmdgroup(
                grpname="plugin",
                permission="plugin.control",
                helptext="Plugin manipulation commands"
                )

        plugingroup.install_command(
                cmdname="load",
                argmatch=r"(?P<plugin>[\w.]+)$",
                callback=self.load_plugin,
                cmdusage="<plugin name>",
                helptext="Loads the given plugin. Assumes format modulename.classname for some module in the plugins package",
                )

        plugingroup.install_command(
                cmdname="unload",
                cmdmatch="unload|remove",
                argmatch=r"(?P<plugin>[\w.]+)$",
                callback=self.unload_plugin,
                cmdusage="<plugin name>",
                helptext="Unloads the given plugin",
                )

        plugingroup.install_command(
                cmdname="reload",
                argmatch=r"(?P<plugin>[\w.]+)$",
                callback=self.reload_plugin,
                cmdusage="<plugin name>",
                helptext="Reloads the plugin's module and starts it, unloading it first if necessary",
                )

        plugingroup.install_command(
                cmdname="reloadall",
                callback=self.reload_all,
                helptext="reloads all plugins except the IRC plugin",
                )

        plugingroup.install_command(
                cmdname="chkconfig on",
                argmatch=r"(?P<plugin>[\w.]+)$",
                callback=self.set_on_startup,
                cmdusage="<plugin name>",
                helptext="Adds the plugin to the startup configuration",
                )
        plugingroup.install_command(
                cmdname="chkconfig off",
                argmatch=r"(?P<plugin>[\w.]+)$",
                callback=self.remove_from_startup,
                cmdusage="<plugin name>",
                helptext="Adds the plugin to the startup configuration",
                )

        plugingroup.install_command(
                cmdname="list",
                permission=None,
                callback=self.list_plugins,
                helptext="Lists all currently loaded plugins",
                )

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
