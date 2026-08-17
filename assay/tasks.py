"""Thirty small programming tasks, hand-written, with tests.

Not HumanEval, and not MBPP, on purpose. Both are in every model's training
data by now, so a model can pass them from memory and the result measures recall
rather than generation. That matters more here than usual: the question this
benchmark exists to answer is *what kind of mistakes a small model makes*, and a
task it has memorised produces no mistakes to classify.

So these are written from scratch. They are deliberately ordinary rather than
clever, because ordinary is what the claim is about: a small model writing the
sort of function someone actually needs, not solving a puzzle.

Each task carries a reference solution. That is not decoration. A test suite
nobody has run against a known-good answer is a suite that can quietly be wrong,
and a wrong test would show up in the results as a model failure, which is the
single most misleading thing this file could do. `python -m assay.tasks` runs
every reference against every test and refuses to pass unless all thirty are
green.

Fields:
    id        stable identifier, used in results
    name      the function the model must write
    signature the exact def line, so the harness can find it
    spec      what the model is told
    tests     source defining check(fn), raising AssertionError on failure
    reference a known-good implementation, used to validate the tests
    needs     stdlib surface the task genuinely requires, or ()
"""

from __future__ import annotations

TASKS = [
    # ------------------------------------------------------------- strings
    {
        "id": "T01",
        "name": "initials",
        "signature": "def initials(full_name: str) -> str:",
        "spec": "Return the uppercase initials of a person's name, separated by "
                "full stops, with a trailing full stop. 'ada lovelace' gives "
                "'A.L.'. Collapse repeated whitespace. An empty or whitespace-only "
                "name gives ''.",
        "reference": (
            "def initials(full_name):\n"
            "    parts = full_name.split()\n"
            "    return ''.join(p[0].upper() + '.' for p in parts)\n"
        ),
        "tests": (
            "def check(fn):\n"
            "    assert fn('ada lovelace') == 'A.L.'\n"
            "    assert fn('Grace  Brewster   Murray Hopper') == 'G.B.M.H.'\n"
            "    assert fn('cher') == 'C.'\n"
            "    assert fn('') == ''\n"
            "    assert fn('   ') == ''\n"
            "    assert fn('  leading space') == 'L.S.'\n"
        ),
        "needs": (),
    },
    {
        "id": "T02",
        "name": "truncate_words",
        "signature": "def truncate_words(text: str, limit: int) -> str:",
        "spec": "Return the longest run of whole words from the start of text "
                "that fits in limit characters, joined by single spaces. Never "
                "split a word. If the very first word is longer than limit, "
                "return that word in full anyway. Empty text, or a limit below "
                "1, gives ''.",
        "reference": (
            "def truncate_words(text, limit):\n"
            "    if limit < 1:\n"
            "        return ''\n"
            "    words = text.split()\n"
            "    if not words:\n"
            "        return ''\n"
            "    out = words[0]\n"
            "    if len(out) > limit:\n"
            "        return out\n"
            "    for w in words[1:]:\n"
            "        if len(out) + 1 + len(w) > limit:\n"
            "            break\n"
            "        out += ' ' + w\n"
            "    return out\n"
        ),
        "tests": (
            "def check(fn):\n"
            "    assert fn('hello world', 20) == 'hello world'\n"
            "    assert fn('the quick brown fox', 9) == 'the quick'\n"
            "    assert fn('the quick brown fox', 10) == 'the quick'\n"
            "    assert fn('a bb ccc', 5) == 'a bb'\n"
            "    assert fn('supercalifragilistic', 5) == 'supercalifragilistic'\n"
            "    assert fn('', 10) == ''\n"
            "    assert fn('abc', 0) == ''\n"
            "    assert fn('abc', -3) == ''\n"
        ),
        "needs": (),
    },
    {
        "id": "T03",
        "name": "slugify",
        "signature": "def slugify(title: str) -> str:",
        "spec": "Turn a title into a URL slug: lowercase, alphanumerics kept, "
                "every other run of characters becomes a single hyphen, and no "
                "leading or trailing hyphen. 'Hello, World!' gives 'hello-world'.",
        "reference": (
            "def slugify(title):\n"
            "    out = []\n"
            "    prev_dash = False\n"
            "    for ch in title.lower():\n"
            "        if ch.isalnum():\n"
            "            out.append(ch)\n"
            "            prev_dash = False\n"
            "        elif not prev_dash:\n"
            "            out.append('-')\n"
            "            prev_dash = True\n"
            "    return ''.join(out).strip('-')\n"
        ),
        "tests": (
            "def check(fn):\n"
            "    assert fn('Hello, World!') == 'hello-world'\n"
            "    assert fn('  Spaces   everywhere  ') == 'spaces-everywhere'\n"
            "    assert fn('a--b') == 'a-b'\n"
            "    assert fn('!!!') == ''\n"
            "    assert fn('Already-slugged') == 'already-slugged'\n"
            "    assert fn('Version 2.0 Release') == 'version-2-0-release'\n"
        ),
        "needs": (),
    },
    {
        "id": "T04",
        "name": "common_prefix",
        "signature": "def common_prefix(words: list) -> str:",
        "spec": "Return the longest string that every word in the list starts "
                "with. An empty list gives ''.",
        "reference": (
            "def common_prefix(words):\n"
            "    if not words:\n"
            "        return ''\n"
            "    first = words[0]\n"
            "    for i in range(len(first)):\n"
            "        for w in words[1:]:\n"
            "            if i >= len(w) or w[i] != first[i]:\n"
            "                return first[:i]\n"
            "    return first\n"
        ),
        "tests": (
            "def check(fn):\n"
            "    assert fn(['flower', 'flow', 'flight']) == 'fl'\n"
            "    assert fn(['dog', 'racecar']) == ''\n"
            "    assert fn([]) == ''\n"
            "    assert fn(['same', 'same']) == 'same'\n"
            "    assert fn(['a']) == 'a'\n"
            "    assert fn(['abc', 'ab']) == 'ab'\n"
        ),
        "needs": (),
    },
    {
        "id": "T05",
        "name": "wrap_columns",
        "signature": "def wrap_columns(text: str, width: int) -> list:",
        "spec": "Split text into a list of lines, each at most width characters, "
                "breaking only at spaces. A single word longer than width goes on "
                "its own line unbroken. Empty text gives [].",
        "reference": (
            "def wrap_columns(text, width):\n"
            "    words = text.split()\n"
            "    if not words:\n"
            "        return []\n"
            "    lines, current = [], words[0]\n"
            "    for w in words[1:]:\n"
            "        if len(current) + 1 + len(w) <= width:\n"
            "            current += ' ' + w\n"
            "        else:\n"
            "            lines.append(current)\n"
            "            current = w\n"
            "    lines.append(current)\n"
            "    return lines\n"
        ),
        "tests": (
            "def check(fn):\n"
            "    assert fn('a bb ccc', 5) == ['a bb', 'ccc']\n"
            "    assert fn('', 10) == []\n"
            "    assert fn('   ', 10) == []\n"
            "    assert fn('supercalifragilistic', 5) == ['supercalifragilistic']\n"
            "    r = fn('one two three four', 9)\n"
            "    assert all(len(l) <= 9 or ' ' not in l for l in r), r\n"
            "    assert ' '.join(fn('one two three', 100)) == 'one two three'\n"
        ),
        "needs": (),
    },
    {
        "id": "T06",
        "name": "caesar",
        "signature": "def caesar(text: str, shift: int) -> str:",
        "spec": "Shift every ASCII letter by shift places, wrapping within its "
                "own case. Non-letters are unchanged. shift may be negative or "
                "larger than 26.",
        "reference": (
            "def caesar(text, shift):\n"
            "    out = []\n"
            "    for ch in text:\n"
            "        if 'a' <= ch <= 'z':\n"
            "            out.append(chr((ord(ch) - 97 + shift) % 26 + 97))\n"
            "        elif 'A' <= ch <= 'Z':\n"
            "            out.append(chr((ord(ch) - 65 + shift) % 26 + 65))\n"
            "        else:\n"
            "            out.append(ch)\n"
            "    return ''.join(out)\n"
        ),
        "tests": (
            "def check(fn):\n"
            "    assert fn('abc', 1) == 'bcd'\n"
            "    assert fn('xyz', 3) == 'abc'\n"
            "    assert fn('Hello, World!', 13) == 'Uryyb, Jbeyq!'\n"
            "    assert fn('abc', -1) == 'zab'\n"
            "    assert fn('abc', 27) == 'bcd'\n"
            "    assert fn('123', 5) == '123'\n"
        ),
        "needs": (),
    },
    {
        "id": "T07",
        "name": "is_balanced",
        "signature": "def is_balanced(text: str) -> bool:",
        "spec": "Return True if every bracket in the text is closed in the right "
                "order. Brackets are (), [] and {}. Other characters are ignored.",
        "reference": (
            "def is_balanced(text):\n"
            "    pairs = {')': '(', ']': '[', '}': '{'}\n"
            "    stack = []\n"
            "    for ch in text:\n"
            "        if ch in '([{':\n"
            "            stack.append(ch)\n"
            "        elif ch in pairs:\n"
            "            if not stack or stack.pop() != pairs[ch]:\n"
            "                return False\n"
            "    return not stack\n"
        ),
        "tests": (
            "def check(fn):\n"
            "    assert fn('(a[b]{c})') is True\n"
            "    assert fn('([)]') is False\n"
            "    assert fn('(') is False\n"
            "    assert fn(')(') is False\n"
            "    assert fn('') is True\n"
            "    assert fn('no brackets here') is True\n"
        ),
        "needs": (),
    },
    {
        "id": "T08",
        "name": "count_words",
        "signature": "def count_words(text: str) -> dict:",
        "spec": "Return a dict mapping each lowercased word to how often it "
                "appears. A word is a run of letters and apostrophes; everything "
                "else separates words.",
        "reference": (
            "def count_words(text):\n"
            "    counts = {}\n"
            "    current = ''\n"
            "    for ch in text.lower() + ' ':\n"
            "        if ch.isalpha() or ch == \"'\":\n"
            "            current += ch\n"
            "        else:\n"
            "            if current:\n"
            "                counts[current] = counts.get(current, 0) + 1\n"
            "            current = ''\n"
            "    return counts\n"
        ),
        "tests": (
            "def check(fn):\n"
            "    assert fn('the cat the') == {'the': 2, 'cat': 1}\n"
            "    assert fn('') == {}\n"
            "    assert fn('Hello, hello!') == {'hello': 2}\n"
            "    assert fn(\"don't stop\") == {\"don't\": 1, 'stop': 1}\n"
            "    assert fn('a1b') == {'a': 1, 'b': 1}\n"
        ),
        "needs": (),
    },

    # --------------------------------------------------------- collections
    {
        "id": "T09",
        "name": "chunk",
        "signature": "def chunk(items: list, size: int) -> list:",
        "spec": "Split a list into consecutive chunks of at most size items. The "
                "last chunk may be shorter. size below 1 raises ValueError.",
        "reference": (
            "def chunk(items, size):\n"
            "    if size < 1:\n"
            "        raise ValueError('size must be positive')\n"
            "    return [items[i:i + size] for i in range(0, len(items), size)]\n"
        ),
        "tests": (
            "def check(fn):\n"
            "    assert fn([1,2,3,4,5], 2) == [[1,2],[3,4],[5]]\n"
            "    assert fn([], 3) == []\n"
            "    assert fn([1,2,3], 5) == [[1,2,3]]\n"
            "    assert fn([1,2,3,4], 2) == [[1,2],[3,4]]\n"
            "    try:\n"
            "        fn([1], 0)\n"
            "        raise AssertionError('expected ValueError')\n"
            "    except ValueError:\n"
            "        pass\n"
        ),
        "needs": (),
    },
    {
        "id": "T10",
        "name": "dedupe_stable",
        "signature": "def dedupe_stable(items: list) -> list:",
        "spec": "Remove duplicates while keeping the first occurrence of each "
                "item in its original position.",
        "reference": (
            "def dedupe_stable(items):\n"
            "    seen, out = set(), []\n"
            "    for x in items:\n"
            "        if x not in seen:\n"
            "            seen.add(x)\n"
            "            out.append(x)\n"
            "    return out\n"
        ),
        "tests": (
            "def check(fn):\n"
            "    assert fn([3,1,3,2,1]) == [3,1,2]\n"
            "    assert fn([]) == []\n"
            "    assert fn(['a','a','a']) == ['a']\n"
            "    assert fn([1,2,3]) == [1,2,3]\n"
            "    src = [1,1,2]\n"
            "    fn(src)\n"
            "    assert src == [1,1,2], 'must not mutate the input'\n"
        ),
        "needs": (),
    },
    {
        "id": "T11",
        "name": "group_by_length",
        "signature": "def group_by_length(words: list) -> dict:",
        "spec": "Group words by their length. Return a dict from length to the "
                "list of words of that length, in the order they appeared.",
        "reference": (
            "def group_by_length(words):\n"
            "    out = {}\n"
            "    for w in words:\n"
            "        out.setdefault(len(w), []).append(w)\n"
            "    return out\n"
        ),
        "tests": (
            "def check(fn):\n"
            "    assert fn(['a','bb','cc','d']) == {1: ['a','d'], 2: ['bb','cc']}\n"
            "    assert fn([]) == {}\n"
            "    assert fn(['']) == {0: ['']}\n"
            "    r = fn(['xx','y'])\n"
            "    assert r[2] == ['xx'] and r[1] == ['y']\n"
        ),
        "needs": (),
    },
    {
        "id": "T12",
        "name": "merge_sorted",
        "signature": "def merge_sorted(a: list, b: list) -> list:",
        "spec": "Merge two already-sorted lists into one sorted list, keeping "
                "duplicates. Do not sort; merge in linear time.",
        "reference": (
            "def merge_sorted(a, b):\n"
            "    out, i, j = [], 0, 0\n"
            "    while i < len(a) and j < len(b):\n"
            "        if a[i] <= b[j]:\n"
            "            out.append(a[i]); i += 1\n"
            "        else:\n"
            "            out.append(b[j]); j += 1\n"
            "    out.extend(a[i:]); out.extend(b[j:])\n"
            "    return out\n"
        ),
        "tests": (
            "def check(fn):\n"
            "    assert fn([1,3,5], [2,4]) == [1,2,3,4,5]\n"
            "    assert fn([], [1,2]) == [1,2]\n"
            "    assert fn([1,2], []) == [1,2]\n"
            "    assert fn([], []) == []\n"
            "    assert fn([1,1], [1]) == [1,1,1]\n"
            "    assert fn([5], [1]) == [1,5]\n"
        ),
        "needs": (),
    },
    {
        "id": "T13",
        "name": "rotate",
        "signature": "def rotate(items: list, n: int) -> list:",
        "spec": "Return a new list rotated left by n positions. n may exceed the "
                "length or be negative (rotating right). An empty list stays "
                "empty.",
        "reference": (
            "def rotate(items, n):\n"
            "    if not items:\n"
            "        return []\n"
            "    n = n % len(items)\n"
            "    return items[n:] + items[:n]\n"
        ),
        "tests": (
            "def check(fn):\n"
            "    assert fn([1,2,3,4], 1) == [2,3,4,1]\n"
            "    assert fn([1,2,3,4], 0) == [1,2,3,4]\n"
            "    assert fn([1,2,3,4], 5) == [2,3,4,1]\n"
            "    assert fn([1,2,3,4], -1) == [4,1,2,3]\n"
            "    assert fn([], 3) == []\n"
            "    assert fn([1], 7) == [1]\n"
        ),
        "needs": (),
    },
    {
        "id": "T14",
        "name": "flatten_depth",
        "signature": "def flatten_depth(nested: list, depth: int) -> list:",
        "spec": "Flatten a nested list by at most depth levels. depth 0 returns a "
                "shallow copy. Non-list items are left alone.",
        "reference": (
            "def flatten_depth(nested, depth):\n"
            "    if depth <= 0:\n"
            "        return list(nested)\n"
            "    out = []\n"
            "    for x in nested:\n"
            "        if isinstance(x, list):\n"
            "            out.extend(flatten_depth(x, depth - 1))\n"
            "        else:\n"
            "            out.append(x)\n"
            "    return out\n"
        ),
        "tests": (
            "def check(fn):\n"
            "    assert fn([1,[2,[3]]], 1) == [1,2,[3]]\n"
            "    assert fn([1,[2,[3]]], 2) == [1,2,3]\n"
            "    assert fn([1,[2,[3]]], 0) == [1,[2,[3]]]\n"
            "    assert fn([], 5) == []\n"
            "    assert fn([[[1]]], 9) == [1]\n"
        ),
        "needs": (),
    },
    {
        "id": "T15",
        "name": "top_n",
        "signature": "def top_n(counts: dict, n: int) -> list:",
        "spec": "Return the n keys with the highest values, highest first. Break "
                "ties alphabetically by key. n larger than the dict returns "
                "everything.",
        "reference": (
            "def top_n(counts, n):\n"
            "    items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))\n"
            "    return [k for k, _ in items[:n]]\n"
        ),
        "tests": (
            "def check(fn):\n"
            "    assert fn({'a': 3, 'b': 1, 'c': 3}, 2) == ['a','c']\n"
            "    assert fn({}, 3) == []\n"
            "    assert fn({'x': 1}, 5) == ['x']\n"
            "    assert fn({'a': 1, 'b': 2}, 0) == []\n"
            "    assert fn({'b': 2, 'a': 2}, 2) == ['a','b']\n"
        ),
        "needs": (),
    },
    {
        "id": "T16",
        "name": "invert_mapping",
        "signature": "def invert_mapping(mapping: dict) -> dict:",
        "spec": "Swap keys and values. Where several keys share a value, the new "
                "value is the sorted list of those keys. Where a value is unique, "
                "the new value is the single key itself, not a list.",
        "reference": (
            "def invert_mapping(mapping):\n"
            "    grouped = {}\n"
            "    for k, v in mapping.items():\n"
            "        grouped.setdefault(v, []).append(k)\n"
            "    return {v: (ks[0] if len(ks) == 1 else sorted(ks))\n"
            "            for v, ks in grouped.items()}\n"
        ),
        "tests": (
            "def check(fn):\n"
            "    assert fn({'a': 1, 'b': 2}) == {1: 'a', 2: 'b'}\n"
            "    assert fn({'a': 1, 'b': 1}) == {1: ['a','b']}\n"
            "    assert fn({}) == {}\n"
            "    assert fn({'z': 9, 'y': 9, 'x': 8}) == {9: ['y','z'], 8: 'x'}\n"
        ),
        "needs": (),
    },

    # ------------------------------------------------------------- numeric
    {
        "id": "T17",
        "name": "running_total",
        "signature": "def running_total(values: list) -> list:",
        "spec": "Return the cumulative sums: element i is the sum of values up to "
                "and including i.",
        "reference": (
            "def running_total(values):\n"
            "    out, total = [], 0\n"
            "    for v in values:\n"
            "        total += v\n"
            "        out.append(total)\n"
            "    return out\n"
        ),
        "tests": (
            "def check(fn):\n"
            "    assert fn([1,2,3]) == [1,3,6]\n"
            "    assert fn([]) == []\n"
            "    assert fn([5]) == [5]\n"
            "    assert fn([1,-1,1]) == [1,0,1]\n"
            "    assert fn([0,0]) == [0,0]\n"
        ),
        "needs": (),
    },
    {
        "id": "T18",
        "name": "median",
        "signature": "def median(values: list) -> float:",
        "spec": "Return the median. For an even count, the mean of the two middle "
                "values. An empty list raises ValueError. Do not mutate the input.",
        "reference": (
            "def median(values):\n"
            "    if not values:\n"
            "        raise ValueError('empty')\n"
            "    s = sorted(values)\n"
            "    mid = len(s) // 2\n"
            "    if len(s) % 2:\n"
            "        return float(s[mid])\n"
            "    return (s[mid - 1] + s[mid]) / 2\n"
        ),
        "tests": (
            "def check(fn):\n"
            "    assert fn([3,1,2]) == 2\n"
            "    assert fn([4,1,3,2]) == 2.5\n"
            "    assert fn([7]) == 7\n"
            "    src = [3,1,2]\n"
            "    fn(src)\n"
            "    assert src == [3,1,2], 'must not mutate the input'\n"
            "    try:\n"
            "        fn([])\n"
            "        raise AssertionError('expected ValueError')\n"
            "    except ValueError:\n"
            "        pass\n"
        ),
        "needs": (),
    },
    {
        "id": "T19",
        "name": "clamp_all",
        "signature": "def clamp_all(values: list, low: float, high: float) -> list:",
        "spec": "Clamp every value into the range low to high inclusive. If low is "
                "greater than high, raise ValueError.",
        "reference": (
            "def clamp_all(values, low, high):\n"
            "    if low > high:\n"
            "        raise ValueError('low above high')\n"
            "    return [low if v < low else high if v > high else v\n"
            "            for v in values]\n"
        ),
        "tests": (
            "def check(fn):\n"
            "    assert fn([1,5,10], 2, 8) == [2,5,8]\n"
            "    assert fn([], 0, 1) == []\n"
            "    assert fn([5], 5, 5) == [5]\n"
            "    assert fn([-3, 3], 0, 10) == [0, 3]\n"
            "    try:\n"
            "        fn([1], 5, 1)\n"
            "        raise AssertionError('expected ValueError')\n"
            "    except ValueError:\n"
            "        pass\n"
        ),
        "needs": (),
    },
    {
        "id": "T20",
        "name": "gcd_list",
        "signature": "def gcd_list(numbers: list) -> int:",
        "spec": "Return the greatest common divisor of all the numbers. An empty "
                "list gives 0. Negative numbers are treated by absolute value.",
        "reference": (
            "def gcd_list(numbers):\n"
            "    def g(a, b):\n"
            "        while b:\n"
            "            a, b = b, a % b\n"
            "        return a\n"
            "    result = 0\n"
            "    for n in numbers:\n"
            "        result = g(result, abs(n))\n"
            "    return result\n"
        ),
        "tests": (
            "def check(fn):\n"
            "    assert fn([12, 18]) == 6\n"
            "    assert fn([]) == 0\n"
            "    assert fn([7]) == 7\n"
            "    assert fn([-12, 18]) == 6\n"
            "    assert fn([5, 7]) == 1\n"
            "    assert fn([0, 5]) == 5\n"
        ),
        "needs": (),
    },
    {
        "id": "T21",
        "name": "percent_change",
        "signature": "def percent_change(old: float, new: float) -> float:",
        "spec": "Return the percentage change from old to new, rounded to two "
                "decimal places. If old is zero and new is zero, return 0.0. If "
                "old is zero and new is not, raise ZeroDivisionError.",
        "reference": (
            "def percent_change(old, new):\n"
            "    if old == 0:\n"
            "        if new == 0:\n"
            "            return 0.0\n"
            "        raise ZeroDivisionError('old is zero')\n"
            "    return round((new - old) / old * 100, 2)\n"
        ),
        "tests": (
            "def check(fn):\n"
            "    assert fn(100, 150) == 50.0\n"
            "    assert fn(100, 50) == -50.0\n"
            "    assert fn(0, 0) == 0.0\n"
            "    assert fn(3, 4) == 33.33\n"
            "    try:\n"
            "        fn(0, 5)\n"
            "        raise AssertionError('expected ZeroDivisionError')\n"
            "    except ZeroDivisionError:\n"
            "        pass\n"
        ),
        "needs": (),
    },
    {
        "id": "T22",
        "name": "digits_sum",
        "signature": "def digits_sum(n: int) -> int:",
        "spec": "Return the sum of the decimal digits of n. Negative numbers use "
                "their absolute value.",
        "reference": (
            "def digits_sum(n):\n"
            "    n = abs(n)\n"
            "    total = 0\n"
            "    while n:\n"
            "        total += n % 10\n"
            "        n //= 10\n"
            "    return total\n"
        ),
        "tests": (
            "def check(fn):\n"
            "    assert fn(123) == 6\n"
            "    assert fn(0) == 0\n"
            "    assert fn(-45) == 9\n"
            "    assert fn(1000000) == 1\n"
            "    assert fn(9) == 9\n"
        ),
        "needs": (),
    },

    # --------------------------------------------------- parsing/validation
    {
        "id": "T23",
        "name": "parse_duration",
        "signature": "def parse_duration(text: str) -> int:",
        "spec": "Parse a duration like '1h30m' or '45s' or '2h' into total "
                "seconds. Units are h, m and s, always in that order, each "
                "optional but at least one present. Anything else raises "
                "ValueError.",
        "reference": (
            "def parse_duration(text):\n"
            "    total, number, seen = 0, '', False\n"
            "    units = {'h': 3600, 'm': 60, 's': 1}\n"
            "    order = []\n"
            "    for ch in text:\n"
            "        if ch.isdigit():\n"
            "            number += ch\n"
            "        elif ch in units:\n"
            "            if not number:\n"
            "                raise ValueError('unit without a number')\n"
            "            order.append(ch)\n"
            "            total += int(number) * units[ch]\n"
            "            number, seen = '', True\n"
            "        else:\n"
            "            raise ValueError('bad character')\n"
            "    if number or not seen:\n"
            "        raise ValueError('trailing number or empty')\n"
            "    if order != sorted(order, key=lambda c: -units[c]):\n"
            "        raise ValueError('units out of order')\n"
            "    if len(set(order)) != len(order):\n"
            "        raise ValueError('repeated unit')\n"
            "    return total\n"
        ),
        "tests": (
            "def check(fn):\n"
            "    assert fn('1h30m') == 5400\n"
            "    assert fn('45s') == 45\n"
            "    assert fn('2h') == 7200\n"
            "    assert fn('1h1m1s') == 3661\n"
            "    for bad in ['', 'abc', '10', '30m1h', 'h', '1x']:\n"
            "        try:\n"
            "            fn(bad)\n"
            "            raise AssertionError('expected ValueError for ' + repr(bad))\n"
            "        except ValueError:\n"
            "            pass\n"
        ),
        "needs": (),
    },
    {
        "id": "T24",
        "name": "split_csv_line",
        "signature": "def split_csv_line(line: str) -> list:",
        "spec": "Split one CSV line on commas, honouring double quotes. A quoted "
                "field may contain commas, and a doubled quote inside a quoted "
                "field means one literal quote. Quotes are stripped from the "
                "result.",
        "reference": (
            "def split_csv_line(line):\n"
            "    fields, current, in_quotes, i = [], '', False, 0\n"
            "    while i < len(line):\n"
            "        ch = line[i]\n"
            "        if in_quotes:\n"
            "            if ch == '\"':\n"
            "                if i + 1 < len(line) and line[i + 1] == '\"':\n"
            "                    current += '\"'; i += 1\n"
            "                else:\n"
            "                    in_quotes = False\n"
            "            else:\n"
            "                current += ch\n"
            "        elif ch == '\"':\n"
            "            in_quotes = True\n"
            "        elif ch == ',':\n"
            "            fields.append(current); current = ''\n"
            "        else:\n"
            "            current += ch\n"
            "        i += 1\n"
            "    fields.append(current)\n"
            "    return fields\n"
        ),
        "tests": (
            "def check(fn):\n"
            "    assert fn('a,b,c') == ['a','b','c']\n"
            "    assert fn('a,\"b,c\",d') == ['a','b,c','d']\n"
            "    assert fn('') == ['']\n"
            "    assert fn('a,,b') == ['a','','b']\n"
            "    assert fn('\"he said \"\"hi\"\"\"') == ['he said \"hi\"']\n"
            "    assert fn('trailing,') == ['trailing','']\n"
        ),
        "needs": (),
    },
    {
        "id": "T25",
        "name": "valid_identifier",
        "signature": "def valid_identifier(name: str) -> bool:",
        "spec": "Return True if name is a valid identifier: starts with a letter "
                "or underscore, then letters, digits or underscores, and is not "
                "one of the reserved words 'if', 'else', 'while', 'return'.",
        "reference": (
            "def valid_identifier(name):\n"
            "    if not name:\n"
            "        return False\n"
            "    if name in ('if', 'else', 'while', 'return'):\n"
            "        return False\n"
            "    first = name[0]\n"
            "    if not (first.isalpha() or first == '_'):\n"
            "        return False\n"
            "    return all(c.isalnum() or c == '_' for c in name[1:])\n"
        ),
        "tests": (
            "def check(fn):\n"
            "    assert fn('total') is True\n"
            "    assert fn('_x1') is True\n"
            "    assert fn('1abc') is False\n"
            "    assert fn('') is False\n"
            "    assert fn('if') is False\n"
            "    assert fn('iffy') is True\n"
            "    assert fn('has space') is False\n"
        ),
        "needs": (),
    },
    {
        "id": "T26",
        "name": "normalise_phone",
        "signature": "def normalise_phone(number: str) -> str:",
        "spec": "Normalise an Australian mobile number to '+61' followed by nine "
                "digits. Accept forms like '0412 345 678', '0412345678', "
                "'+61412345678' and '61412345678'. Spaces, hyphens and brackets "
                "are ignored. Anything that is not a valid mobile raises "
                "ValueError.",
        "reference": (
            "def normalise_phone(number):\n"
            "    digits = ''.join(c for c in number if c.isdigit())\n"
            "    plus = number.strip().startswith('+')\n"
            "    if digits.startswith('61') and len(digits) == 11:\n"
            "        rest = digits[2:]\n"
            "    elif digits.startswith('0') and len(digits) == 10 and not plus:\n"
            "        rest = digits[1:]\n"
            "    else:\n"
            "        raise ValueError('not a mobile number')\n"
            "    if not rest.startswith('4') or len(rest) != 9:\n"
            "        raise ValueError('not a mobile number')\n"
            "    return '+61' + rest\n"
        ),
        "tests": (
            "def check(fn):\n"
            "    assert fn('0412 345 678') == '+61412345678'\n"
            "    assert fn('0412345678') == '+61412345678'\n"
            "    assert fn('+61412345678') == '+61412345678'\n"
            "    assert fn('61412345678') == '+61412345678'\n"
            "    assert fn('(04) 1234-5678') == '+61412345678'\n"
            "    for bad in ['', '12345', '0312345678', '041234567']:\n"
            "        try:\n"
            "            fn(bad)\n"
            "            raise AssertionError('expected ValueError for ' + repr(bad))\n"
            "        except ValueError:\n"
            "            pass\n"
        ),
        "needs": (),
    },
    {
        "id": "T27",
        "name": "roman_to_int",
        "signature": "def roman_to_int(roman: str) -> int:",
        "spec": "Convert an uppercase Roman numeral to an integer. Handle "
                "subtractive pairs such as IV and CM. Assume the input is well "
                "formed.",
        "reference": (
            "def roman_to_int(roman):\n"
            "    values = {'I':1,'V':5,'X':10,'L':50,'C':100,'D':500,'M':1000}\n"
            "    total = 0\n"
            "    for i, ch in enumerate(roman):\n"
            "        v = values[ch]\n"
            "        if i + 1 < len(roman) and v < values[roman[i + 1]]:\n"
            "            total -= v\n"
            "        else:\n"
            "            total += v\n"
            "    return total\n"
        ),
        "tests": (
            "def check(fn):\n"
            "    assert fn('III') == 3\n"
            "    assert fn('IV') == 4\n"
            "    assert fn('MCMXCIV') == 1994\n"
            "    assert fn('') == 0\n"
            "    assert fn('MMXXV') == 2025\n"
            "    assert fn('XL') == 40\n"
        ),
        "needs": (),
    },

    # ------------------------------------------------------------- stateful
    {
        "id": "T28",
        "name": "apply_discounts",
        "signature": "def apply_discounts(price: float, discounts: list) -> float:",
        "spec": "Apply each percentage discount in turn to the price, each one "
                "applied to the already-discounted amount, and round the final "
                "result to two decimal places. A discount outside 0 to 100 raises "
                "ValueError.",
        "reference": (
            "def apply_discounts(price, discounts):\n"
            "    for d in discounts:\n"
            "        if d < 0 or d > 100:\n"
            "            raise ValueError('discount out of range')\n"
            "        price = price * (1 - d / 100)\n"
            "    return round(price, 2)\n"
        ),
        "tests": (
            "def check(fn):\n"
            "    assert fn(100, [10]) == 90.0\n"
            "    assert fn(100, [10, 10]) == 81.0\n"
            "    assert fn(100, []) == 100.0\n"
            "    assert fn(50, [100]) == 0.0\n"
            "    try:\n"
            "        fn(100, [110])\n"
            "        raise AssertionError('expected ValueError')\n"
            "    except ValueError:\n"
            "        pass\n"
        ),
        "needs": (),
    },
    {
        "id": "T29",
        "name": "seat_allocator",
        "signature": "def seat_allocator(rows: int, per_row: int, requests: list) -> list:",
        "spec": "Allocate seats left to right, row by row, one row at a time. Each "
                "request is a party size; a party must sit together in one row or "
                "be refused. Return a list the same length as requests, holding "
                "either a (row, start_seat) pair with zero-based indices, or None "
                "if the party could not be seated.",
        "reference": (
            "def seat_allocator(rows, per_row, requests):\n"
            "    filled = [0] * rows\n"
            "    out = []\n"
            "    for size in requests:\n"
            "        placed = None\n"
            "        for r in range(rows):\n"
            "            if per_row - filled[r] >= size:\n"
            "                placed = (r, filled[r])\n"
            "                filled[r] += size\n"
            "                break\n"
            "        out.append(placed)\n"
            "    return out\n"
        ),
        # A pair may come back as a tuple or a list. Failing one of those would
        # measure how the model spells a pair rather than whether it allocated
        # the seats, and pedantry of that kind pollutes the taxonomy this
        # benchmark exists to produce.
        "tests": (
            "def norm(result):\n"
            "    return [tuple(x) if isinstance(x, (list, tuple)) else x\n"
            "            for x in result]\n"
            "def check(fn):\n"
            "    assert norm(fn(2, 4, [2, 2, 2])) == [(0,0),(0,2),(1,0)]\n"
            "    assert norm(fn(1, 3, [4])) == [None]\n"
            "    assert norm(fn(1, 3, [2, 2])) == [(0,0), None]\n"
            "    assert norm(fn(2, 2, [])) == []\n"
            "    assert norm(fn(2, 2, [1,1,1,1])) == [(0,0),(0,1),(1,0),(1,1)]\n"
        ),
        "needs": (),
    },
    {
        "id": "T30",
        "name": "reconcile",
        "signature": "def reconcile(expected: dict, actual: dict) -> dict:",
        "spec": "Compare two mappings of item name to quantity. Return a dict with "
                "three keys: 'missing' for items in expected but not actual or "
                "with a lower actual quantity, mapped to the shortfall; 'extra' "
                "for items in actual but not expected or with a higher quantity, "
                "mapped to the surplus; and 'matched' for the sorted list of "
                "names whose quantities agree.",
        "reference": (
            "def reconcile(expected, actual):\n"
            "    missing, extra, matched = {}, {}, []\n"
            "    for name in set(expected) | set(actual):\n"
            "        want = expected.get(name, 0)\n"
            "        have = actual.get(name, 0)\n"
            "        if have < want:\n"
            "            missing[name] = want - have\n"
            "        elif have > want:\n"
            "            extra[name] = have - want\n"
            "        else:\n"
            "            matched.append(name)\n"
            "    return {'missing': missing, 'extra': extra,\n"
            "            'matched': sorted(matched)}\n"
        ),
        "tests": (
            "def check(fn):\n"
            "    r = fn({'a': 2, 'b': 1}, {'a': 2, 'c': 3})\n"
            "    assert r['missing'] == {'b': 1}, r\n"
            "    assert r['extra'] == {'c': 3}, r\n"
            "    assert r['matched'] == ['a'], r\n"
            "    r = fn({}, {})\n"
            "    assert r == {'missing': {}, 'extra': {}, 'matched': []}\n"
            "    r = fn({'x': 5}, {'x': 2})\n"
            "    assert r['missing'] == {'x': 3}\n"
        ),
        "needs": (),
    },
]


def validate() -> int:
    """Run every reference solution against its own tests.

    A benchmark whose tests have never been checked against a known-good answer
    measures the tests as much as the model, and a wrong test surfaces in the
    results as a model failure. That is the most misleading thing this file
    could do, so it refuses to be trusted until this passes.
    """
    failures = []
    for task in TASKS:
        namespace: dict = {}
        try:
            exec(task["reference"], namespace)
            exec(task["tests"], namespace)
            namespace["check"](namespace[task["name"]])
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{task['id']} {task['name']}: "
                            f"{type(exc).__name__}: {exc}")
    ids = [t["id"] for t in TASKS]
    if len(set(ids)) != len(ids):
        failures.append("duplicate task ids")
    names = [t["name"] for t in TASKS]
    if len(set(names)) != len(names):
        failures.append("duplicate function names")

    print(f"{len(TASKS)} tasks, {len(TASKS) - len(failures)} references pass")
    for line in failures:
        print(f"  FAIL  {line}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(validate())
