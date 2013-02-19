Ideas for the Future!
=====================

* Optional maximum/default ban/quiet timer, for channels whose ban/quiet lists
  are prone to filling up.

* Get rid of per-command prefixes. They're unconfigurable and complicate the
  code and nobody uses them anyways. Similar: per-chanel prefixes instead of
  one global prefix. This may sort of depend on the command restructuring idea
  though (see below)

* Some kind of spam detector / rate limiter to quickly quiet users that are
  spamming. To negate false positives I think it could also take into account
  if the user is using webchat or not. Webchat users are more often spammers.
  Punishment should be a 10 second quiet, so even on a false positive it's not
  that bad. That should be plenty of time for an OP ot take additional action
  if it was legit.

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

* Add in a group abstraction for permissions. It was pretty stupid of me not to
  do this from the start. 4 years of security research on fancy abstractions
  for specifying authorization and authentication and I go and do a stupid
  access list for my bot.

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

  Maybe I just don't worry about the help system at all. I just declare that
  commands listed in the help listing may or may not work in every channel. I
  think I have more motivation for this change than the command system overhaul
  so this may be the best option considering the circumstances.
