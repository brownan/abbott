Ideas for the Future!
=====================

* Fix up logging. I want logging that is actually useful, tells which plugin
  it's coming from, the ability to turn logging on and off per plugin/module,
  colorized for different levels, etc. I'm thinking it may be good to just
  ditch the twisted logger and roll my own.

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

* ircop: Determine supported modes with and without parameters to do error
  checking in !mode command. This is related to the next point.

* ircop: Determine server's supported maximum number of modes that can be
  issued in a single MODE command, and change the ircop mode batching to match.
  The twisted irc client already gets this value and stores it in the
  clientobj.supported somewhere, I think. I just need to find the right value
  and use it.

* ircop: If a connector mode operation is submitted, but we're not op, but op
  is pending (for another request previously submitted), then we should not use
  the connector, so that less total lines are printed (since we're going to op
  and deop anyways and the mode operation can be batched with the deop).

  Actually it'd be better to go even further: I should buffer even requests
  that can be fulfilled by a connector. Then, in processing the buffer, it
  should only use the connector if ALL items in the buffer can be fulfilled
  with a connector.  Otherwise, OP and do them all ourself. This way the
  operations don't have to be submitted in any particular order for this to
  work.

  This is a good idea for another reason. Since it is recommended not to
  yield-wait for any but the final request when a plugin issues several, what
  if the first request requires op but the second is fulfilled with a
  connector? The caller would never get any errors.

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
