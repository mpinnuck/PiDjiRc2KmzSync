import re
from datetime import datetime


def normalize_mtp_modify_date(raw_value: str) -> str:
    raw = (raw_value or "").strip()
    if not raw:
        return ""

    if raw.startswith("12/30/1899") or raw.startswith("30/12/1899") or raw.startswith("1899-12-30"):
        return ""

    for fmt in ("%Y-%m-%d %H:%M:%S",):
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue

        if parsed.year <= 1900:
            return ""
        return parsed.strftime("%d/%m/%Y %H:%M:%S")

    slash_match = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})\s+(.+)$", raw)
    if slash_match:
        first = int(slash_match.group(1))
        second = int(slash_match.group(2))

        if first > 12 >= second:
            ordered_formats = ["%d/%m/%Y %I:%M %p", "%d/%m/%Y %H:%M:%S"]
        elif second > 12 >= first:
            ordered_formats = ["%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M:%S"]
        elif first <= 12 and second <= 12:
            # Keep ambiguous dates unchanged instead of guessing wrong locale.
            return raw
        else:
            return raw

        for fmt in ordered_formats:
            try:
                parsed = datetime.strptime(raw, fmt)
            except ValueError:
                continue
            if parsed.year <= 1900:
                return ""
            return parsed.strftime("%d/%m/%Y %H:%M:%S")

    return raw
