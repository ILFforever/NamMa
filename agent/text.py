"""Message clean-up before routing: what people paste or type into LINE is not always plain text.

Zero-width and control characters, odd spaces, full-width Latin, a decomposed สระอำ, emoji between letters and
stretched letters ("ช่วยยยยด้วย") would otherwise hide a danger word from the keyword rules.
"""
import re
import unicodedata

_SPACES = re.compile(r"[\s\u00a0\u1680\u2000-\u200a\u202f\u205f\u3000]+")
# Pictographs, dingbats, variation selectors and keycaps. Thai (U+0E00 block) is outside these ranges.
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27bf\u2b00-\u2bff\ufe00-\ufe0f\u20e3]")
_SOS = re.compile("\U0001F198")                      # 🆘
_DIGITS = str.maketrans("\u0e50\u0e51\u0e52\u0e53\u0e54\u0e55\u0e56\u0e57\u0e58\u0e59\u0660\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669", "01234567890123456789")   # Thai and Arabic-Indic digits
# Cyrillic letters that look Latin (S, O, e in "SOS", "help"), and links (a URL with "sos" in it is not a call for help).
_HOMOGLYPHS = str.maketrans("\u0410\u0412\u0415\u041a\u041c\u041d\u041e\u0420\u0421\u0422\u0425\u0430\u0435\u043e\u0440\u0441\u0443\u0445\u0405\u0406\u0408", "ABEKMHOPCTXaeopcyxSIJ")
_URL = re.compile(r"https?://\S+|www\.\S+|</?[a-zA-Z][^>]{0,40}>")   # links and HTML tags
_STRETCH = re.compile(r"([^\d\W])\1{2,}|([\u0e31-\u0e3a\u0e47-\u0e4e])\2+")   # 3+ of a letter, 2+ of a mark


def clean(text: str) -> str:
    """The text the router reads. Thai stays as typed apart from the fixes above; digits are never collapsed."""
    t = _SOS.sub(" sos ", text)
    t = "".join(ch for ch in t if unicodedata.category(ch) not in ("Cf", "Cc") or ch in "\n\t")
    t = "".join(ch for ch in t if unicodedata.category(ch) != "Cs")                     # lone surrogates
    # Styled Latin (full-width, maths bold, circled) -> plain; Thai is left alone (NFKC would split สระอำ).
    t = "".join(ch if "\u0e00" <= ch <= "\u0e7f" else unicodedata.normalize("NFKC", ch) for ch in t)
    t = t.translate(_DIGITS).translate(_HOMOGLYPHS)
    t = _URL.sub(" ", t)
    t = t.replace("\u0e4d\u0e32", "\u0e33")           # decomposed nikhahit + sara aa -> one sara am
    t = re.sub("[\u0300-\u036f]", "", t)              # Latin combining marks stacked on Thai
    t = _EMOJI.sub(" ", t)
    t = _STRETCH.sub(lambda m: m.group(1) or m.group(2), t)
    return _SPACES.sub(" ", t).strip()
