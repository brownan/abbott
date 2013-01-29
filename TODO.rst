Ideas for the Future!
=====================

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

* ircop: If a connector mode operation is submitted, but we're not op, but op
  is pending (for another request previously submitted), then we should skip
  the connector and batch it in with the rest of the requests when we get OP so
  that less total lines are printed.

  Actually it'd be better to go even further: I should buffer connector
  requests as well instead of submitting them as soon as they come in. Then, in
  processing the buffer, it should use the connector only if ALL items in the
  buffer can be fulfilled with a connector.  Otherwise, OP and do them all
  ourself in one batch. This way the operations don't have to be submitted in
  any particular order for this to work.

  This is a good idea for another reason. Since it is recommended not to
  yield-wait for any but the final request when a plugin issues several, what
  if the first request requires op but the second is fulfilled with a
  connector? The caller would never get any errors.

* Related to the above: error reporting in ircop is kind weird right now and
  requires plugins to wait for OP to be acquired before they regain control if
  they want to know about errors in the process. An alternative may be to pass
  an error handler in to the request as a parameter. This way the request
  handlers can return control to the caller immediately, so the caller can
  issue more requests into the batch, but the caller can still have control
  over how to handle errors.

  I need to think about this a bit more. Is this really much/any different from
  returning a Deferred object and attaching an errback to it?

  This will be a problem if we want to buffer connector-handled requests.  They
  may error right away if they need op, or may error later when they try to
  call the connector but it isn't loaded. I think I need a way to pass an
  error-reporting function around so that errors that come in now or later can
  all go to the same place.

  Here's what I think: the deferreds returned from the requests currently
  convey two pieces of information: if an error occurs acquiring op (the
  errback is called), and *when* op is acquired (the callback is called).  This
  is what it should do: it should errback for *any* errors along the chain, and
  callback when the requested operation finishes completely. It would then be
  obvious that callers shouldn't wait for the first of several operations, but
  to submit them all then wait for one or more of them. This would mean some
  restructuring of the entire ircop plugin though. Not that there's anything
  wrong with that; it sounds like fun!

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
