"""Indian states/UTs with their GST state codes, for place-of-supply comparison.

Place of supply decides CGST+SGST (intra-state) vs IGST (inter-state), so free-text
state strings must be compared on a canonical form: "RJ", "rajasthan.", "08" and
"Rajasthan" are the same state. Unknown strings are kept as typed (and compared
case/punctuation-insensitively) rather than rejected, so legacy data still works.
"""
import re

# code -> (canonical name, common abbreviations)
_STATES: dict[str, tuple[str, tuple[str, ...]]] = {
    "01": ("Jammu and Kashmir", ("JK", "J&K")),
    "02": ("Himachal Pradesh", ("HP",)),
    "03": ("Punjab", ("PB",)),
    "04": ("Chandigarh", ("CH",)),
    "05": ("Uttarakhand", ("UK", "UT", "Uttaranchal")),
    "06": ("Haryana", ("HR",)),
    "07": ("Delhi", ("DL", "New Delhi", "NCT of Delhi")),
    "08": ("Rajasthan", ("RJ",)),
    "09": ("Uttar Pradesh", ("UP",)),
    "10": ("Bihar", ("BR",)),
    "11": ("Sikkim", ("SK",)),
    "12": ("Arunachal Pradesh", ("AR",)),
    "13": ("Nagaland", ("NL",)),
    "14": ("Manipur", ("MN",)),
    "15": ("Mizoram", ("MZ",)),
    "16": ("Tripura", ("TR",)),
    "17": ("Meghalaya", ("ML",)),
    "18": ("Assam", ("AS",)),
    "19": ("West Bengal", ("WB",)),
    "20": ("Jharkhand", ("JH",)),
    "21": ("Odisha", ("OD", "OR", "Orissa")),
    "22": ("Chhattisgarh", ("CG", "Chattisgarh")),
    "23": ("Madhya Pradesh", ("MP",)),
    "24": ("Gujarat", ("GJ",)),
    "26": ("Dadra and Nagar Haveli and Daman and Diu", ("DN", "DD", "DNHDD")),
    "27": ("Maharashtra", ("MH",)),
    "29": ("Karnataka", ("KA",)),
    "30": ("Goa", ("GA",)),
    "31": ("Lakshadweep", ("LD",)),
    "32": ("Kerala", ("KL",)),
    "33": ("Tamil Nadu", ("TN",)),
    "34": ("Puducherry", ("PY", "Pondicherry")),
    "35": ("Andaman and Nicobar Islands", ("AN",)),
    "36": ("Telangana", ("TS", "TG")),
    "37": ("Andhra Pradesh", ("AP",)),
    "38": ("Ladakh", ("LA",)),
}


def _key(s: str) -> str:
    s = s.lower().replace("&", " and ")
    return re.sub(r"[^a-z0-9]", "", s)


_LOOKUP: dict[str, str] = {}
for _code, (_name, _abbrs) in _STATES.items():
    for _alias in (_code, str(int(_code)), _name, *_abbrs):
        _LOOKUP[_key(_alias)] = _code


def state_code(raw: str | None) -> str | None:
    """GST state code for a free-text state ("RJ", "rajasthan", "08"), else None."""
    if not raw:
        return None
    return _LOOKUP.get(_key(raw))


def canonical(raw: str | None) -> str | None:
    """Canonical state name if recognised, else the stripped input (None if blank)."""
    if raw is None or not raw.strip():
        return None
    code = state_code(raw)
    return _STATES[code][0] if code else raw.strip()


def same_state(a: str | None, b: str | None) -> bool:
    ca, cb = state_code(a), state_code(b)
    if ca and cb:
        return ca == cb
    return _key(a or "") == _key(b or "")
