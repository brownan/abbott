Abbott
======

An IRC bot framework using Twisted by Andrew Brown.

Okay so this isn't anything special or ground breaking, it was just for fun.
Despite being intended for IRC, this is a general framework that could find use
in other applications.

Interesting or Notable Features
-------------------------------

* Full unicode support
* Features a generalized event and request framework for communication among
  plugins
* Includes a command system with Nickserv-based identification, a robust-ish
  authentication/permission system, built-in !help, and regular
  expression-based argument parsing
* Completely single-threaded with cooperative coroutines using Twisted
  deferreds. No locks or unexpected state mutations that come with threads!
* Persistent data / config files for plugins
* Automatically reconnects when disconnected

Requirements
------------

To run the bot you need python 2.7. 2.6 may work but I've probably used some
features added in 2.7. I haven't really been keeping track. You also need
Twisted installed. Some plugins also need other external python libraries, too.

Launching
---------

Usage:

    python main.py <config dir>

Ensure that the abbott package is on the python path. If the directory does not
exist, it will be created. The config dir is where the configuration is stored,
one json file per plugin, plus an overall config.json. The first time you run
the bot, it will ask a few questions and configure itself with the minimal set
of plugins and configuration it needs to launch and connect to an IRC server.

Getting Started
---------------

Look through the plugins offered by various modules in the plugins directory.
Ask the bot in a direct message for help with the 'help' command. Load new
plugins with the 'plugin load <modulename>.<pluginname>' command. Set
permissions with the 'permission add <nickserv account> <permission> [channel]'
command. See the definition of the commands in the code for what permission
they require.

Understanding the code
----------------------

If you want to understand the code, I suggest you start reading in
transport.py. The docstring and transport object in there describe the plugin
communication method, which is most all of the "framework" (the rest being the
mechanism for loading plugins. After that all that's left is the plugins
themselves!)

Then start reading in pluginbase.py to understand the interfaces provided to
plugins. Don't worry about command.py because that contains a touch of black
magic that may be hard to understand; it's better to understand command plugins
by looking at examples and maybe the docstrings in command.py.

You'll also want to read up on twisted deferred objects if you aren't familiar;
they are used extensively all throughout the framework. Also make sure you know
what the defer.inlineCallbacks decorator does and how to use it.

After that, start reading the plugins or write your own. I like to think my
code is pretty well commented and documented, but you still need to be able to
read and write code to understand it. I love answering questions and explaining
my work so please feel free to email me or find me on IRC.
