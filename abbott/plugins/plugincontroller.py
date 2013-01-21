from twisted.python.reflect import namedModule
from twisted.python import log

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
                argmatch=r"(?P<module>[\w.]+)$",
                callback=self.reload_module,
                cmdusage="<module name>",
                helptext="Reloads a plugin module. This will unload and load all plugins in this module, re-loading the module into the interpreter in the process.",
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
        for dep in self.pluginboss.loaded_plugins[plugin_name].REQUIRES:
            if dep not in self.pluginboss.loaded_plugins:
                event.reply("Warning: {0} depends on {1}, but {1} is not loaded".format(plugin_name, dep))

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

    def reload_module(self, event, match):
        module_name = match.groupdict()['module']

        # okay, here's what's going to go down. First, we gonna find all the
        # plugins that use this module. Then we gonna unload them, see? Then we
        # gonna reload the module. Yeah? That's good. Then we's gonna get them
        # modules loaded back up. That should do it, ya think? I think it
        # should. Solid.

        plugins = [x for x in self.pluginboss.loaded_plugins.keys() if x.startswith(module_name+".")]
        log.msg("Request to reload module %s. Unloading these plugins: %s" % (
            module_name,
            ", ".join(plugins),
            ))

        for plugin_name in plugins:
            try:
                self.pluginboss.unload_plugin(plugin_name)
            except Exception:
                event.reply("Something went wrong unloading %s. Some plugins may have been unloaded. Check the error log!" % plugin_name)
                raise
            else:
                log.msg("%s unloaded" % plugin_name)

        module = namedModule("abbott.plugins." + module_name)
        try:
            reload(module)
        except Exception:
            event.reply("There was an error reloading the module. None of the plugins were reloaded. Check the log")
            raise

        log.msg("%s reloaded" % module_name)

        for plugin_name in plugins:
            try:
                self.pluginboss.load_plugin(plugin_name)
            except Exception:
                event.reply("Something went wrong loading %s. Please see the error log" % plugin_name)
            else:
                log.msg("%s loaded" % plugin_name)

        event.reply("Finished")

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
        for dep in self.pluginboss.loaded_plugins[plugin_name].REQUIRES:
            if dep not in pluginlist:
                event.reply("Warning: {0} depends on {1}, but {1} is not set to load on startup".format(plugin_name, dep))

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
