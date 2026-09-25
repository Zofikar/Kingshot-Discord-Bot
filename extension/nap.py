"""Pure NAP logic: ranking, lookback parsing, tag aliases, academies, exclusions.

Ports the pure (non-Discord) methods from the pre-migration monolith so they can
be unit-tested directly. The NAP cog composes this logic with ``storage`` and
``discord`` channel I/O; nothing here touches Discord.
"""

import re
from datetime import datetime, timedelta, timezone


class NapLogic:
    """Pure NAP computation over in-memory state."""

    NAP_RANKING_LINE_RE = re.compile(
        r"^(\d{1,3})\. \[([^\]]+)\] (.+?) - ([\d,]+)(?: \(ac: \[([^\]]+)\]\))?$"
    )

    def __init__(self, *, nap_alliances_count=10, alliances=None, nap_tag_aliases=None,
                 academies=None, nap_exclusions=None):
        self.NAP_ALLIANCES_COUNT = nap_alliances_count
        self.alliances = alliances if alliances is not None else {}
        self.nap_tag_aliases = nap_tag_aliases if nap_tag_aliases is not None else {}
        self.academies = academies if academies is not None else {}
        self.nap_exclusions = nap_exclusions if nap_exclusions is not None else {}

    def save_data(self):
        """No-op here; the cog persists via storage after each mutation."""

    def get_tag_for_aid(self, aid):
        stored = self.alliances.get(str(aid)) or {}
        return stored.get("abbr")

    def _resolve_aid_for_tag(self, tag, *, exact_only=False):
        if not tag:
            return None
        wanted = str(tag).strip()
        if not wanted:
            return None
        wanted_upper = wanted.upper()

        # 1. Exact tag among current alliances.
        for aid_key, info in self.alliances.items():
            abbr = info.get("abbr")
            if abbr is not None and str(abbr).strip() == wanted:
                try:
                    return int(aid_key)
                except (TypeError, ValueError):
                    continue

        # 2. Exact historical alias.
        aliased = self.nap_tag_aliases.get(wanted)
        if aliased is not None:
            try:
                return int(aliased)
            except (TypeError, ValueError):
                pass

        if exact_only:
            return None

        # 3. Case-insensitive alliances (only when unambiguous).
        ci_aids = set()
        for aid_key, info in self.alliances.items():
            abbr = info.get("abbr")
            if abbr and str(abbr).strip().upper() == wanted_upper:
                try:
                    ci_aids.add(int(aid_key))
                except (TypeError, ValueError):
                    continue
        if len(ci_aids) == 1:
            return ci_aids.pop()
        if len(ci_aids) > 1:
            return None

        # 4. Case-insensitive aliases, same unambiguity rule.
        ci_alias_aids = set()
        for alias_key, alias_val in self.nap_tag_aliases.items():
            if str(alias_key).strip().upper() == wanted_upper:
                try:
                    ci_alias_aids.add(int(alias_val))
                except (TypeError, ValueError):
                    continue
        if len(ci_alias_aids) == 1:
            return ci_alias_aids.pop()

        return None

    def record_tag_alias(self, tag, aid):
        if tag is None or aid is None:
            return False
        tag_key = str(tag).strip()
        if not tag_key:
            return False
        try:
            aid_val = int(aid)
        except (TypeError, ValueError):
            return False

        wanted_upper = tag_key.upper()
        for existing_key, existing_val in self.nap_tag_aliases.items():
            try:
                if (str(existing_key).strip().upper() == wanted_upper
                        and int(existing_val) == aid_val):
                    return False
            except (TypeError, ValueError):
                continue

        self.nap_tag_aliases[tag_key] = aid_val
        return True

    def is_nap_excluded(self, *, aid=None, tag=None):
        aid_key = str(aid) if aid is not None else None
        if aid_key is None and tag:
            resolved = self._resolve_aid_for_tag(str(tag).strip())
            aid_key = str(resolved) if resolved is not None else None
        if aid_key is not None and aid_key in self.nap_exclusions:
            return True, self.nap_exclusions[aid_key]
        return False, None

    def get_nap_exclusion_lines(self):
        lines = []
        for aid_key, record in sorted(
                self.nap_exclusions.items(),
                key=lambda item: item[1].get("added_utc", ""),
        ):
            info = self.alliances.get(str(aid_key)) or {}
            label = info.get("abbr") or record.get("tag") or f"aid {aid_key}"
            reason = record.get("reason") or "no reason given"
            lines.append(f"⛔ [{label}] is not part of NAP because of {reason}")
        return lines

    def set_nap_exclusion(self, aid, *, reason, added_by, tag=None):
        aid_key = str(aid)
        if aid_key in self.nap_exclusions:
            return False
        self.nap_exclusions[aid_key] = {
            "reason": reason,
            "added_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "added_by": added_by,
            "tag": tag,
        }
        self.save_data()
        return True

    def remove_nap_exclusion(self, aid):
        removed = self.nap_exclusions.pop(str(aid), None) is not None
        if removed:
            self.save_data()
        return removed

    def set_alliance_academy(self, main_aid, academy_aid):
        main_key = str(main_aid)
        if academy_aid is None:
            if main_key in self.academies:
                del self.academies[main_key]
                return True
            return False
        try:
            academy_val = int(academy_aid)
            if int(main_aid) == academy_val:
                return False
        except (TypeError, ValueError):
            return False
        changed = False
        for other_key, other_val in list(self.academies.items()):
            try:
                if int(other_val) == academy_val and other_key != main_key:
                    del self.academies[other_key]
                    changed = True
            except (TypeError, ValueError):
                continue
        if self.academies.get(main_key) == academy_val:
            return changed
        self.academies[main_key] = academy_val
        return True

    def get_main_aid_for_academy(self, academy_aid):
        try:
            wanted = int(academy_aid)
        except (TypeError, ValueError):
            return None
        for main_key, academy_val in self.academies.items():
            try:
                if int(academy_val) == wanted:
                    return main_key
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def build_nap_message(ranked, academy_tags=None):
        lines = []
        for idx, info in enumerate(ranked, start=1):
            tag = info.get("abbr") or "???"
            name = info.get("name") or tag
            power = int(info.get("power", 0) or 0)
            line = f"{idx}. [{tag}] {name} - {power:,}"
            if academy_tags:
                ac_tag = academy_tags.get(str(info.get("aid")))
                if ac_tag:
                    line += f" (ac: [{ac_tag}])"
            lines.append(line)
        return "\n".join(lines)

    @staticmethod
    def build_nap_tag_message(tags):
        lines = []
        for i in range(0, len(tags), 3):
            lines.append(" ".join(tags[i:i + 3]))
        return "\n".join(lines)

    def build_nap_tag_list(self, ranked, academy_tags=None, fallen_protected=None):
        tags = []
        seen = set()

        def add_tag(tag):
            tag = str(tag or "").strip()
            if not tag:
                return
            if tag in seen:
                return
            seen.add(tag)
            tags.append(tag)

        academy_tags = academy_tags or {}
        for row in ranked:
            if self.is_nap_excluded(aid=row.get("aid"), tag=row.get("abbr"))[0]:
                continue
            add_tag(row.get("abbr"))
            ac_tag = academy_tags.get(str(row.get("aid")))
            if ac_tag:
                add_tag(ac_tag)

        for entry in fallen_protected or []:
            if self.is_nap_excluded(aid=entry.get("aid"), tag=entry.get("abbr"))[0]:
                continue
            add_tag(entry.get("abbr"))
            ac_aid = self.academies.get(str(entry.get("aid")))
            if ac_aid is not None:
                add_tag(self.get_tag_for_aid(ac_aid))

        return tags

    @classmethod
    def parse_nap_ranking_message(cls, content):
        if not content:
            return None
        rows = []
        expected_index = 1
        block_started = False
        for raw_line in str(content).splitlines():
            line = raw_line.strip()
            if not line:
                if block_started:
                    break
                continue
            match = cls.NAP_RANKING_LINE_RE.match(line)
            if not match:
                if not block_started:
                    return None
                break
            try:
                index = int(match.group(1))
                power = int(match.group(4).replace(",", ""))
            except ValueError:
                return None
            if index != expected_index:
                return None
            rows.append({
                "abbr": match.group(2).strip(),
                "name": match.group(3).strip(),
                "power": power,
                "academy_tag": (match.group(5) or "").strip() or None,
            })
            expected_index += 1
            block_started = True
        return rows or None

    async def get_nap_ranking(self):
        ranked = []
        academy_aids = set()
        for academy_val in self.academies.values():
            try:
                academy_aids.add(int(academy_val))
            except (TypeError, ValueError):
                continue

        for aid_key, info in self.alliances.items():
            try:
                aid = int(aid_key)
                power = int(info.get("power", 0) or 0)
            except (TypeError, ValueError):
                continue
            if aid in academy_aids:
                continue
            tag = info.get("abbr")
            if not tag or power <= 0:
                continue
            ranked.append({
                "aid": aid, "abbr": tag, "name": info.get("name") or tag,
                "power": power, "role_id": info.get("role_id"),
            })

        ranked.sort(key=lambda item: item["power"], reverse=True)
        if len(ranked) < self.NAP_ALLIANCES_COUNT:
            return None

        top = []
        for row in ranked:
            if str(row["aid"]) in self.nap_exclusions:
                continue
            top.append(row)
            if len(top) >= self.NAP_ALLIANCES_COUNT:
                break
        return top

    def get_nap_protection_snapshot(self):
        """Current top-N snapshot for protection checks (never returns None)."""
        ranked = []
        academy_aids = set()
        for academy_val in self.academies.values():
            try:
                academy_aids.add(int(academy_val))
            except (TypeError, ValueError):
                continue

        for aid_key, info in self.alliances.items():
            try:
                aid = int(aid_key)
                power = int(info.get("power", 0) or 0)
            except (TypeError, ValueError):
                continue
            if aid in academy_aids:
                continue
            tag = info.get("abbr")
            if not tag or power <= 0:
                continue
            ranked.append({"aid": aid, "abbr": tag, "name": info.get("name") or tag, "power": power})

        ranked.sort(key=lambda item: item["power"], reverse=True)
        top = []
        for row in ranked:
            if str(row["aid"]) in self.nap_exclusions:
                continue
            top.append(row)
            if len(top) >= self.NAP_ALLIANCES_COUNT:
                break
        return top


def filter_lookback(logic, lookback_entries):
    """Drop academy + excluded entries from a lookback list."""
    result = []
    for entry in lookback_entries:
        aid = entry.get("aid")
        if aid is not None and logic.get_main_aid_for_academy(aid) is not None:
            continue
        if logic.is_nap_excluded(aid=aid, tag=entry.get("abbr"))[0]:
            continue
        result.append(entry)
    return result


def nap_protection_for_alliance(logic, lookback_entries, lookback_days, *, aid=None, tag=None, nap_chain=None):
    """(protected, status) using snapshot + exclusions + academy + lookback."""
    aid_str = str(aid) if aid is not None else None
    tag_upper = str(tag).strip().upper() if tag else None
    if not aid_str and not tag_upper:
        return False, "no alliance"

    nap_chain = nap_chain or set()
    academy_main = None
    if aid_str is not None:
        academy_main = logic.get_main_aid_for_academy(aid_str)
    if academy_main is None and tag_upper:
        resolved = logic._resolve_aid_for_tag(tag_upper)
        if resolved is not None:
            academy_main = logic.get_main_aid_for_academy(resolved)
    if academy_main is not None and academy_main not in nap_chain:
        protected, status = nap_protection_for_alliance(
            logic, lookback_entries, lookback_days,
            aid=academy_main, nap_chain=nap_chain | {academy_main},
        )
        main_tag = logic.get_tag_for_aid(academy_main) or f"aid {academy_main}"
        return protected, f"{status} (academy of [{main_tag}])"

    excluded, rec = logic.is_nap_excluded(aid=aid, tag=tag)
    if excluded:
        return False, f"⛔ not part of NAP (excluded: {(rec or {}).get('reason') or 'no reason given'})"

    snapshot = logic.get_nap_protection_snapshot()
    snap_aids = {str(r["aid"]) for r in snapshot}
    snap_tags = {str(r["abbr"]).strip().upper() for r in snapshot}
    if (aid_str and aid_str in snap_aids) or (tag_upper and tag_upper in snap_tags):
        return True, "✅ protected (current snapshot)"

    for entry in filter_lookback(logic, lookback_entries):
        entry_aid = str(entry["aid"]) if entry.get("aid") is not None else None
        entry_tag = str(entry.get("abbr") or "").strip().upper()
        if (aid_str and entry_aid == aid_str) or (tag_upper and entry_tag == tag_upper):
            until = entry["last_seen_utc"] + timedelta(days=lookback_days)
            return True, (
                f"🛡️ protected via {lookback_days}-day lookback until "
                f"{until.strftime('%Y-%m-%d %H:%M')} UTC"
            )
    return False, "❌ not NAP-protected"


def get_nap_protected_alliances(logic, lookback_entries, lookback_days):
    """Union of the current snapshot + still-protected lookback entries."""
    current = logic.get_nap_protection_snapshot()
    lookback = filter_lookback(logic, lookback_entries)
    current_aids = {str(r["aid"]) for r in current}
    current_tags = {str(r["abbr"]).strip().upper() for r in current}
    fallen = []
    for entry in lookback:
        in_current = (
            (entry.get("aid") is not None and str(entry["aid"]) in current_aids)
            or str(entry.get("abbr") or "").strip().upper() in current_tags
        )
        if not in_current:
            fallen.append(entry)
    return {
        "lookback_days": lookback_days,
        "current": current,
        "lookback": lookback,
        "fallen_protected": fallen,
        "protected_aids": current_aids | {str(e["aid"]) for e in lookback if e.get("aid") is not None},
        "protected_tags": current_tags | {str(e.get("abbr") or "").strip().upper() for e in lookback},
    }
