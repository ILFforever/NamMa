"""Message clean-up before routing: what people paste or type into LINE is not always plain text.

Zero-width and control characters, odd spaces, full-width Latin, a decomposed สระอำ, emoji between letters and
stretched letters ("ช่วยยยยด้วย") would otherwise hide a danger word from the keyword rules.
"""
import re
import unicodedata

_SPACES = re.compile(r"[\s   -   　]+")
# Pictographs, dingbats, variation selectors and keycaps. Thai (U+0E00 block) is outside these ranges.
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿︀-️⃣]")
_SOS = re.compile("\U0001F198")                      # 🆘
_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙٠١٢٣٤٥٦٧٨٩", "01234567890123456789")   # Thai and Arabic-Indic digits
_STRETCH = re.compile(r"([^\d\W])\1{2,}|([ั-ฺ็-๎])\2+")   # 3+ of a letter, 2+ of a mark


def clean(text: str) -> str:
    """The text the router reads. Thai stays as typed apart from the fixes above; digits are never collapsed."""
    t = _SOS.sub(" sos ", text)
    t = "".join(ch for ch in t if unicodedata.category(ch) not in ("Cf", "Cc") or ch in "\n\t")
    t = "".join(ch for ch in t if unicodedata.category(ch) != "Cs")                     # lone surrogates
    # Styled Latin (full-width, 𝐒𝐎𝐒, ⓢⓞⓢ) → plain; Thai is left alone (NFKC would split สระอำ).
    t = "".join(ch if "฀" <= ch <= "๿" else unicodedata.normalize("NFKC", ch) for ch in t)
    t = t.translate(_DIGITS)
    t = t.replace("ํา", "ำ")           # น + ํ + า → น้ำ as one สระอำ
    t = re.sub("[̀-ͯ]", "", t)              # Latin combining marks stacked on Thai
    t = _EMOJI.sub(" ", t)
    t = _STRETCH.sub(lambda m: m.group(1) or m.group(2), t)
    return _SPACES.sub(" ", t).strip()
