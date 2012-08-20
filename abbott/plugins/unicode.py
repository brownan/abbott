import unicodedata

from twisted.python import log

from ..command import CommandPluginSuperclass

class Unicoder(CommandPluginSuperclass):
    def start(self):
        super(Unicoder, self).start()

        self.install_command(
                cmdname="chr",
                argmatch=r"(?P<chr>.)$|(?:[uU]\+)?(?P<uni>[\da-fA-F]{1,6})$",
                callback=self.lookup_by_chr,
                helptext="Given a single character, prints out information about it."
                )

    def lookup_by_chr(self, event, match):
        c = match.groupdict()['chr'] or unichr(int(match.groupdict()['uni'],16))
        self._info_on_char(event.reply, c)

    def _info_on_char(self, reply, c):

        try:
            name = unicodedata.name(c)
        except ValueError:
            name = "(no name in database)"

        cat = unicodedata.category(c)

        replytxt = u"U+%04X" % (ord(c),)
        if not cat.startswith("C"):
            replytxt += " (%s)" % c
        replytxt += ": %s" % name

        cats = {
                "Cc": "Other, Control",
                "Cf": "Other, Format",
                "Cn": "Other, Not Assigned",
                "Co": "Other, Private Use",
                "Cs": "Other, Surrogate",
                "LC": "Letter, Cased",
                "Ll": "Letter, Lowercase",
                "Lm": "Letter, Modifier",
                "Lo": "Letter, Other",
                "Lt": "Letter, Titlecase",
                "Lu": "Letter, Uppercase",
                "Mc": "Mark, Spacing Combining",
                "Me": "Mark, Enclosing",
                "Mn": "Mark, Nonspacing",
                "Nd": "Number, Decimal Digit",
                "Nl": "Number, Letter",
                "No": "Number, Other",
                "Pc": "Punctuation, Connector",
                "Pd": "Punctuation, Dash",
                "Pe": "Punctuation, Close",
                "Pf": "Punctuation, Final quote",
                "Pi": "Punctuation, Initial quote",
                "Po": "Punctuation, Other",
                "Ps": "Punctuation, Open",
                "Sc": "Symbol, Currency",
                "Sk": "Symbol, Modifier",
                "Sm": "Symbol, Math",
                "So": "Symbol, Other",
                "Zl": "Separator, Line",
                "Zp": "Separator, Paragraph",
                "Zs": "Separator, Space",
                }
        try:
            replytxt += ", category: %s" % cats[cat]
        except KeyError:
            log.err("No category found for %s" % cat)

        try:
            replytxt += ", numeric value %s" % unicodedata.numeric(c)
        except ValueError:
            pass

        decomp = unicodedata.decomposition(c)
        if decomp:
            replytxt += ", decomposition: " + decomp

        reply(replytxt)
