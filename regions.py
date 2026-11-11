"""Supported selling regions: India (INR), US (USD), UK (GBP), the eurozone (EUR) and the UAE (AED), detected from the phone's country code."""
from typing import Optional

CURRENCIES = ("INR", "USD", "GBP", "EUR", "AED")

# Calling code -> (ISO country, currency). Longest prefix wins.
CALLING_CODES = {
    "91": ("IN", "INR"),
    "1": ("US", "USD"),
    "971": ("AE", "AED"),
    "44": ("GB", "GBP"),
    "49": ("DE", "EUR"), "33": ("FR", "EUR"), "39": ("IT", "EUR"), "34": ("ES", "EUR"), "31": ("NL", "EUR"),
    "32": ("BE", "EUR"), "43": ("AT", "EUR"), "353": ("IE", "EUR"), "351": ("PT", "EUR"), "358": ("FI", "EUR"),
    "30": ("GR", "EUR"), "352": ("LU", "EUR"), "421": ("SK", "EUR"), "386": ("SI", "EUR"), "372": ("EE", "EUR"),
    "371": ("LV", "EUR"), "370": ("LT", "EUR"), "356": ("MT", "EUR"), "357": ("CY", "EUR"), "385": ("HR", "EUR"),
}


def region_for_phone(phone: str) -> Optional[dict]:
    """{'country','currency'} for an E.164 number, or None if the country isn't supported."""
    digits = phone.lstrip("+")
    for n in (3, 2, 1):
        hit = CALLING_CODES.get(digits[:n])
        if hit:
            return {"country": hit[0], "currency": hit[1]}
    return None
