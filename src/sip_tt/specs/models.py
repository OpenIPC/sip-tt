"""The shape of one ETSI test purpose, and what we derive from its identifier."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# SIP_<area>_<entity>[_<function>]*_<class>_<nnn>
#
# Three things about this grammar were each learnt by losing purposes to it:
#
# * The class token has four values, not three: V valid, I invalid, O
#   inopportune, and **TI timer**. The timer family is 234 identifiers — two
#   fifths of the document — and a grammar without it drops them silently.
# * The function part is not one token. Proxy purposes carry two
#   (SIP_CC_PR_MP_RQ_V_001: message processing, request), so it repeats.
# * pdftotext puts stray spaces inside identifiers — the OPTIONS family comes
#   out as "SIP_QC_OE_ V_001" — so a candidate is squeezed before matching.
TP_ID = re.compile(
    r"^SIP_(?P<area>[A-Z]{2})_(?P<entity>[A-Z]{2})"
    r"(?P<function>(?:_[A-Z]{2})*)_(?P<klass>V|I|O|TI)_(?P<n>\d+)$")

_SPACE = re.compile(r"\s+")


def normalise_id(raw: str) -> str:
    """An identifier as the document meant it, not as pdftotext emitted it."""
    return _SPACE.sub("", raw.strip())


AREA = {"RG": "registration", "CC": "call control",
        "MG": "message processing", "QC": "capability query (OPTIONS)"}

ENTITY = {
    "RT": "registrant", "RR": "registrar",
    "OE": "originating endpoint", "TE": "terminating endpoint",
    "PR": "proxy", "RD": "redirect server",
}

FUNCTION = {"CE": "call establishment", "CR": "call release",
            "SM": "session modification", "MP": "message processing",
            "RQ": "request", "RS": "response", "TR": "transaction",
            "SE": "server", "CL": "client"}

KLASS = {"V": "valid", "I": "invalid", "O": "inopportune", "TI": "timer"}

# Which entities a user agent can be asked to be. A camera registers, places
# calls and receives them; it is never the registrar, the proxy or the
# redirect server, so those families are not applicable rather than failing.
UA_ENTITIES = {"RT", "OE", "TE"}


@dataclass
class TestPurpose:
    """One numbered purpose, as the document states it."""

    # pytest collects any class named Test*, and warns when it cannot. The
    # name comes from the specification's own vocabulary and is worth keeping,
    # so opt out explicitly rather than renaming around the collector.
    __test__ = False

    id: str
    status: str = ""          # "Mandatory" | "Recommended" | "Void" | "PICS: ..."
    ref: str = ""             # the RFC 3261 section(s) it tests
    purpose: str = ""         # ETSI's own wording — never redistributed
    area: str = ""
    entity: str = ""
    function: str = ""
    klass: str = ""
    number: int = 0

    @classmethod
    def from_id(cls, tp_id: str, **kw) -> "TestPurpose | None":
        tp_id = normalise_id(tp_id)
        m = TP_ID.match(tp_id)
        if not m:
            return None
        g = m.groupdict()
        return cls(id=tp_id, area=g["area"], entity=g["entity"],
                   function=(g["function"] or "").lstrip("_"),
                   klass=g["klass"], number=int(g["n"]), **kw)

    @property
    def family(self) -> str:
        """The identifier with its number stripped: the group it belongs to."""
        return self.id.rsplit("_", 1)[0]

    @property
    def applies_to_ua(self) -> bool:
        return self.entity in UA_ENTITIES

    @property
    def mandatory(self) -> bool:
        return self.status.strip().lower().startswith("mandatory")

    @property
    def void(self) -> bool:
        return self.status.strip().lower().startswith("void")

    @property
    def pics(self) -> str:
        """The PICS expression gating this purpose, or '' when unconditional.

        `Status` is not a severity label but an applicability predicate:
        `Mandatory`, `Recommended`, `Void`, or a reference into the ICS tables
        of TS 102 027-1 such as `PICS: A.77/1.2 AND A.77/3.2`. A device
        profile answers those, which is how the tool decides that a purpose
        does not apply rather than that the device failed it.
        """
        s = self.status.strip()
        i = s.upper().find("PICS:")
        return s[i + 5:].strip() if i >= 0 else ""

    def describe(self) -> str:
        bits = [AREA.get(self.area, self.area), ENTITY.get(self.entity, self.entity)]
        for f in self.function.split("_") if self.function else []:
            bits.append(FUNCTION.get(f, f))
        bits.append(KLASS.get(self.klass, self.klass))
        return ", ".join(bits)

    def to_dict(self) -> dict:
        return {"id": self.id, "status": self.status, "ref": self.ref,
                "purpose": self.purpose, "area": self.area,
                "entity": self.entity, "function": self.function,
                "klass": self.klass, "number": self.number}
