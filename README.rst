Abbott
======

A generalized event/plugin framework and a set of plugins for an IRC bot, using
Twisted, by Andrew Brown.

Okay so this isn't anything special or ground breaking, it was just for fun.
Despite being intended for IRC, this is a general framework that could find use
in other applications. (well, not that easily tbh. you could use this for
non-irc purposes, but all the plugins right now are designed around the IRC
plugin, so you'd need a whole new set of plugins.)

Interesting or Notable Features
-------------------------------

* All functionality is implemented as plugins except for a mechanism for
  loading plugins and a mechanism for communication between plugins
* Online loading and reloading of plugins
* Features a generalized event and request framework for communication among
  plugins
* Full unicode support
* Includes a command system with Nickserv-based identification, a robust-ish
  authentication/permission system, built-in !help, and regular
  expression-based argument parsing
* Completely single-threaded with cooperative coroutines using Twisted
  deferreds. No locks or unexpected state mutations that come with threads!
* Persistent data / config files for plugins
* Automatically reconnects when disconnected
* Well-commented code and clear, well-defined separation between layers of
  abstraction. (well, it could always be better, but I think it's pretty good)

Requirements
------------

To run the bot you need Python 3.3 or later. You also need Twisted installed.
Support for Twisted's IRC library may not have made it to release yet, so you
will need to install the latest Twisted from SVN.

Additionally, the following third party libraries are needed for select
plugins. You can `pip install` most of these.

* For the icecast plugin, you need the `beautifulsoup4` package.

* Far the admin plugin, you need the `parsedatetime` package. The version in
  PYPI doesn't support python 3, but the latest in SVN does:
  http://parsedatetime.googlecode.com/svn/trunk/

* For the useful.URLShortener plugin you need the `python-googl` package.

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
