"""
Standardized menu selection helpers.

A terminal has no real GUI dropdown, so these numbered menus are the
standard equivalent: you pick from a fixed list, which keeps the
vocabulary consistent (no free-typed experience levels or source names).
"""


def pick_one(title, options, default_index=None):
    """
    Show a numbered menu; return the chosen option value.

    options: list of (label, value) tuples.
    default_index: 0-based index used when the user just presses Enter.
    """
    print(f"\n>> {title}")
    for i, (label, _value) in enumerate(options, 1):
        marker = "  (default)" if default_index == i - 1 else ""
        print(f"   {i}. {label}{marker}")
    while True:
        raw = input("   Pick a number: ").strip()
        if not raw and default_index is not None:
            return options[default_index][1]
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][1]
        print(f"   Please enter a number from 1 to {len(options)}.")


def pick_many(title, options, default_all=True):
    """
    Show a numbered checklist; return a list of chosen option values.

    Accepts comma/space-separated numbers (e.g. "1,3,4"), or "all",
    or Enter for the default (all when default_all=True).
    options: list of (label, value) tuples.
    """
    print(f"\n>> {title}")
    for i, (label, _value) in enumerate(options, 1):
        print(f"   {i}. {label}")
    hint = "all" if default_all else "none"
    while True:
        raw = input(f"   Pick numbers (comma-separated), or 'all' "
                    f"[Enter = {hint}]: ").strip().lower()
        if not raw:
            return [v for _l, v in options] if default_all else []
        if raw == "all":
            return [v for _l, v in options]
        if raw == "none":
            return []
        tokens = [t for t in raw.replace(",", " ").split() if t]
        if all(t.isdigit() and 1 <= int(t) <= len(options) for t in tokens) and tokens:
            picked, seen = [], set()
            for t in tokens:
                idx = int(t) - 1
                if idx not in seen:
                    seen.add(idx)
                    picked.append(options[idx][1])
            return picked
        print(f"   Enter numbers 1-{len(options)} (e.g. '1,3'), 'all', or 'none'.")


def ask_text(prompt, default=""):
    val = input(f"   {prompt}").strip()
    return val or default


def ask_yes_no(prompt, default_yes=True):
    suffix = " [Y/n] " if default_yes else " [y/N] "
    ans = input(prompt + suffix).strip().lower()
    if not ans:
        return default_yes
    return ans in ("y", "yes")


def ask_yes_no_strict(prompt, default_yes=True):
    """
    Like ask_yes_no, but NEVER treats an unclear answer as "no".

    Only "y"/"yes" (or Enter when default_yes) count as yes; only "n"/"no"
    (or Enter when not default_yes) count as no. Anything else — a typo, a
    stray paste, a mis-click — reprompts instead of silently discarding the
    run. Use this for high-stakes gates like "write these results to disk?".
    """
    suffix = " [Y/n] " if default_yes else " [y/N] "
    while True:
        ans = input(prompt + suffix).strip().lower()
        if not ans:
            return default_yes
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        print("   Please type 'y' or 'n' (or press Enter for the default).")
