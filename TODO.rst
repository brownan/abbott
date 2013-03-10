Ideas for the Future!
=====================

* Optional maximum/default ban/quiet timer, for channels whose ban/quiet lists
  are prone to filling up.

* Get rid of per-command prefixes. They're unconfigurable and complicate the
  code and nobody uses them anyways. Similar: per-chanel prefixes instead of
  one global prefix. This may sort of depend on the command restructuring idea
  though (see below)

  Note: I've already disabled the per-command prefixes on the few commands that
  used it, but still to be done are to remove the feature entirely.

* This point is more abstract, but it would be nice to have a generalized way
  to say "I want this to happen in X seconds". The admin plugin does this, but
  only for modes, and it does all the heavy lifting itself right in the plugin.
  I'd like to abstract this and generalize it. Perhaps as its own plugin, or
  maybe add the functionality as a mixin. I say it's abstract because I don't
  have yet a good idea of the best way to accomplish this goal, just the goal
  itself.

  Thoughts: a mixin is probably a better idea. It would give plugins more
  control over the process. If it were a standalone plugin that other plugins
  could make a "later request" to then it seems like that would be much more
  restrictive of a workflow.  How would I report errors? How would I cancel
  something previously scheduled? Would I have to deal with serialization? A
  mixin could simply call some dispatch method for laters and the plugin could
  do whatever it wants.

  More thoughts: the plugins could statically define a mapping of callback
  names to callback methods, in a class variable. Avoiding the problem of
  methods being unserializable, you'd just serialize the callback name and
  parameters.

* Fix up logging. I want logging that is actually useful, tells which plugin
  it's coming from, the ability to turn logging on and off per plugin/module,
  colorized for different levels, etc. I'm thinking it may be good to just
  ditch the twisted logger and roll my own based on python's standard logging
  library.

  Side note: it would be nice to have a log level between INFO and WARNING that
  is used to print a line not only to the console, but to a designated irc
  "control" channel. Perhaps it should emit an event that plugins can handle as
  they see fit, and the irc plugin can echo it to the control channel.

* votd: remember nickserv account names and host masks to re-voice people that
  have left

* Have the bot identify itself. This is probably easy and just a matter of
  specifying an instance attribute on the bot plugin object to specify a server
  username and password.

* Recognize other punctuation as part of the command prefix. <botname>: as well
  as <botname>, at least

* ircop: Determine the irc server's supported maximum number of modes that can
  be issued in a single MODE command, and change the ircop mode batching to
  match.  The twisted irc client already gets this value and stores it in the
  clientobj.supported somewhere, I think. I just need to find the right value
  and use it.

* Delete or update the docs. Inaccurate docs are worse than no docs. Plus I
  think the code is mostly self explanatory. My original intent was to have the
  docs be a reference of sorts that I could use to remind myself e.g. of what
  permissions did what, and other pieces of the internal apis. I should have
  known I wouldn't keep it up to date.

* The ability to load multiple instances of a plugin would be nice. For this
  I'd have to re-think how plugins are named. I'm thinking the smallest delta
  from current code to achieve this feature would be the concept of a "plugin
  alias". When you launch a plugin, you can optionally give it an alias, which
  identifies that instance both to control the plugin and to name its config
  file.

* If we can have multiple instances of some plugins, then along with that there
  needs to be a way to set up per-plugin filters on incoming events. Some
  plugins can be installed globally—they get full reign over all commands and
  events sent from every channel. Some plugins I want to install with a
  filter—they only receive events that match a criteria, so that I can run them
  in just one channel. Also, some channel-unaware plugins I want to run in two
  channels, so there needs to be a way to run two instances of the same plugin
  (the above point) AND have them only respond to their respective channels.

  I should also think about how the help system will work in this situation. If
  you ask for help, does the help system know which commands you can execute
  and where? Some plugins are "channel unaware", in that they perform a simple
  function and respond to wherever the incomming message came from. I can use
  these filters to restrict which channels they apply in. But do such commands
  still exist in the global help listing? Maybe the solution is to have these
  filters not only define a filter for incoming events, but also a filter for
  the help system. Somehow.

  Or maybe I just don't worry about the help system at all. I just declare that
  commands listed in the help listing may or may not work in every channel. The
  command system overhaul (next bullet) may be a better solution and may make
  it eaiser to implement these kind of filters, but I think I have more
  motivation for this change than the command system overhaul so this may be
  the best option considering the circumstances.

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
