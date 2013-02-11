Ideas for the Future!
=====================

* test ircop error contitions. Particularly: if chanserv never responds (due to
  netsplit, etc). We should get an error timeout after 20-30 seconds, but there
  may be a bug because this happened and it never errored. (operations sent via
  a connector we get no error, but operations which we need to op and then do
  something we timeout. Maybe we should follow through on various connector
  operations?)

  also if a connector is not loaded. Errors all around don't seem to be
  propagating back to their callers correctly.

* Optional maximum ban/quiet timer, for channels whose ban/quiet lists are
  prone to filling up.

* Get rid of per-command prefixes. They're unconfigurable and complicate the
  code and nobody uses them anyways.

* Fix up logging. I want logging that is actually useful, tells which plugin
  it's coming from, the ability to turn logging on and off per plugin/module,
  colorized for different levels, etc. I'm thinking it may be good to just
  ditch the twisted logger and roll my own based on python's standard logging
  library.

  Side note: it would be nice to have a log level between INFO and WARNING that
  is used to print a line not only to the console, but to a designated irc
  "control" channel. Perhaps it should emit an event that plugins can handle as
  they see fit, and the irc plugin can echo it to the control channel.

* Make a decorator to declare a command for command plugins. I don't like
  having to declare commands in the start() method and then have the
  implementation elsewhere in the file. Those two should be near each other. I
  think I could figure something out with metaclasses and class decorators that
  is compatible with the existing code.

  On the other hand, having all the commands declared in one place has its
  advantages. The declarations serve as sort of a reference all in one place,
  instead of having to comb through all the code. Maybe the right solution is
  to declare the commands in some kind of data structure instead of a bunch of
  calls to install_command() (if only this were lisp, rite? lol)

* votd: remember nickserv account names and host masks to re-voice people that
  have left

* Have the bot identify itself. This is probably easy and just a matter of
  specifying an instance attribute on the bot plugin object to specify a server
  username and password.

* Recognize other punctuation as part of the command prefix. <botname>: as well
  as <botname>, at least

* ircop: Determine which modes take a parameter so we can do error checking in
  !mode command. This is related to the next point.

* ircop: Determine the irc server's supported maximum number of modes that can
  be issued in a single MODE command, and change the ircop mode batching to
  match.  The twisted irc client already gets this value and stores it in the
  clientobj.supported somewhere, I think. I just need to find the right value
  and use it.

* Generalize the timed actions in the admin plugin. I should be able to set +c
  or +t or +r and then set a timer to set -t or -c or -r again. Besides that,
  it bugs me that the timed operations are hard coded for only -q and -b. This
  seems like an easy opportunity for generalization.

  TL;DR: every admin command should accept the "for" or "until" keyword to set
  a time to undo the operation.

* Long term: redo the command plugin workflow. Instead of having command
  plugins inherit from a special base class which takes care of parsing
  incoming irc lines for commands, have a single IRCCommand plugin that listens
  for irc lines, parses them, and emits a separate command event to any plugins
  that care to receive them.
 
  This would make it easy to add in new backends besides irc that work with the
  same set of commands, which is something hesperus does better right now but
  only because of how I chose to implement commands. (not because of any
  inherit difference in the frameworks)

  The only difficulty would be in how to handle help text, permissions, and the
  meta-help that lists available commands. Not to mention command groups and
  sub commands. I'm not sure how that would work, but I'm sure something could
  be worked out. Perhaps a decorator that checks permissions and registers the
  command and help text with some overall help plugin would work. Plugins could
  raise a special exception if the parameters are bad to have help text
  displayed. (but that would require command plugins to inherit from a special
  command base class, which is something I'd like to avoid if possible).
  Conclusion: this is long term and may not happen and in any case needs more
  thought.

* Also for the distant future: the ability to load multiple instances of a
  plugin would be nice. For this I'd have to re-think how plugins are named. I
  also need to think of a way to set which channels each responds to (maybe a
  filter) for plugins that aren't aware of channel, and prevent commands from
  clashing in channels they both apply in. See next bullet.

* If we can have multiple instances of some plugins, then along with that
  should be a way to set up per-plugin filters on incoming events. Some plugins
  can be installed globally—they get full reign over all commands and events
  sent from every channel. Some plugins are installed with a filter—they only
  receive events that match a criteria.

  This will help better support multi-channel bots where I want some set of
  silly plugins in one channel but a different set of plugins that respond in
  another channel.

  I should also think about how the help system will work in this situation. If
  you ask for help, does the help system know which commands you can execute
  and where? Obviously some plugins can still run globally and have channel
  access restricted by the permission system, like the admin plugins. Others,
  like votd need multiple copies to run and depending on which channel you are
  in, is routed to a different instance. But for a plugin like the
  music/shoutcast plugin that we only want available in one channel, it would
  still show up in the global help. hmm. Maybe the command restructuring would
  help here, since we could filter on commands instead of incoming messages,
  and the help system would be aware of commands and could hook into that.
