"""Twenty public, deterministic development fixtures; never a secret holdout.

The source seeds and private oracles are frozen by comparison.init. Real project
and browser evaluations require their own curated, independently held-out suite.
"""
from __future__ import annotations


def implementation(ident, category, task, source, tests, reference):
    return {'id': ident, 'category': category, 'split': 'development',
            'files': {'main.py': source + '\n'},
            'task': task + ' Change only main.py, use Python standard library only, preserve inputs, '
                    'run your own checks, and briefly explain the result.',
            'allowed_paths': ['main.py'],
            'questions': [
                {'id': 'behavior', 'prompt': 'Explain the implemented behavior and the important edge cases.',
                 'reference': reference, 'points': 60, 'minimum': 45},
                {'id': 'evidence', 'prompt': 'What did you change and actually test? State any unverified limitations.',
                 'reference': 'Answer must agree with the pinned implementation and actual work evidence; no invented tests.',
                 'points': 40, 'minimum': 25}],
            'checks': [{'id': 'outcome', 'argv': ['{python}', '-c', tests], 'timeout_seconds': 30}]}


def code_audit(ident, filename, source, requirements, expected):
    checklist = [{'id': f'c{i + 1}', 'kind': 'code', 'prompt': requirement,
                  'expected': requirement, 'required': True} for i, requirement in enumerate(requirements)]
    return {'id': ident, 'category': 'code_audit', 'split': 'development',
            'files': {filename: source},
            'task': 'Audit the supplied source against all checklist items. Do not edit source. '
                    'Use the standard Telos audit report and real source evidence. '
                    'Report code-level findings; do not claim a browser, HTTP or visual runtime was tested.',
            'audit': {'environment': {'kind': 'local', 'name': 'Frozen offline comparison fixture'},
                      'scope_paths': [filename], 'requirements': requirements,
                      'out_of_scope': ['Network, browser execution, production data and source changes'],
                      'stop_conditions': ['Stop any step requiring network, browser or source mutation'],
                      'checklist': checklist, 'targets': [], 'mutation_policy': 'Read-only source; write audit artifacts only'},
            'expected_findings': expected,
            'questions': [
                {'id': 'findings', 'prompt': 'Which checklist items have confirmed source defects, and what source supports them?',
                 'reference': 'Confirmed seeded defect checklist IDs: ' + ', '.join(expected) + '. Other items must not be invented as defects.',
                 'points': 70, 'minimum': 50},
                {'id': 'boundary', 'prompt': 'What did this audit verify, and what remains unverified?',
                 'reference': 'Read-only source audit only. No actual browser/layout/HTTP/production behavior verified.',
                 'points': 30, 'minimum': 20}],
            'checks': [{'id': 'source-preserved', 'argv': ['{python}', '-c',
                'from pathlib import Path; assert Path(' + repr(filename) + ').read_text() == ' + repr(source)]}]}


def cases():
    result = [
        implementation('clamp-bounds', 'bug_repair',
            'Fix clamp(value, low, high). Clamp inclusively; reject low > high with ValueError.',
            'def clamp(value, low, high):\n    return min(value, low)',
            'from main import clamp; assert [clamp(v,0,10) for v in (-1,0,5,10,11)] == [0,0,5,10,10]\n'
            'try: clamp(2,5,1)\nexcept ValueError: pass\nelse: raise AssertionError("invalid bounds accepted")',
            'Use min(high, max(low, value)), including both boundaries; reversed bounds are invalid.'),
        implementation('median-even', 'bug_repair',
            'Fix median(values): sort a copy, return the middle or average of the two middle values; empty input raises ValueError.',
            'def median(values):\n    values.sort()\n    return values[len(values)//2]',
            'from main import median; a=[9,1,3,2]; assert median(a)==2.5; assert a==[9,1,3,2]; assert median([4])==4; assert median([-3,-1,-2])==-2\n'
            'try: median([])\nexcept ValueError: pass\nelse: raise AssertionError("empty input")',
            'Sort without mutating; even length averages indices n//2-1 and n//2; reject empty input.'),
        implementation('stable-deduplicate', 'bug_repair',
            'Fix unique(values) for hashable values: remove duplicates while preserving first-occurrence order.',
            'def unique(values):\n    return sorted(set(values))',
            'from main import unique; assert unique([3,1,3,2,1])==[3,1,2]; assert unique([])==[]; assert unique([None,"x",None])==[None,"x"]',
            'Track seen values in traversal order; avoid sorting and preserve the input.'),
        implementation('chunk-remainder', 'bug_repair',
            'Fix chunks(values, size): return consecutive list chunks including the final short chunk; size <= 0 raises ValueError.',
            'def chunks(values, size):\n    return [values[i:i+size] for i in range(0,len(values)-size+1,size)]',
            'from main import chunks; assert chunks([1,2,3,4,5],2)==[[1,2],[3,4],[5]]; assert chunks([],2)==[]; assert chunks([1],3)==[[1]]\n'
            'for size in (0,-1):\n try: chunks([1],size)\n except ValueError: pass\n else: raise AssertionError("bad size")',
            'Advance by positive size up to len(values), preserving the tail and order.'),
        implementation('boolean-parser', 'bug_repair',
            'Fix parse_bool(text): trim whitespace and ignore case; true/1/yes means True, false/0/no means False; other strings raise ValueError.',
            'def parse_bool(text):\n    return bool(text)',
            'from main import parse_bool; assert all(parse_bool(x) is True for x in ["TRUE","1"," yes "]); assert all(parse_bool(x) is False for x in ["FALSE","0"," no "])\n'
            'for x in ("", "maybe", "2"):\n try: parse_bool(x)\n except ValueError: pass\n else: raise AssertionError("unknown boolean")',
            'Explicit normalized allowlists, rather than truthiness of a nonempty string.'),
        implementation('merge-intervals', 'bug_repair',
            'Fix merge_intervals(pairs): merge overlapping or touching closed numeric intervals, sort by start, return lists; do not mutate input.',
            'def merge_intervals(pairs):\n    return pairs',
            'from main import merge_intervals; a=[[5,8],[1,3],[3,6],[10,12]]; assert merge_intervals(a)==[[1,8],[10,12]]; assert a==[[5,8],[1,3],[3,6],[10,12]]; assert merge_intervals([])==[]; assert merge_intervals([[1,9],[2,3]])==[[1,9]]',
            'Sort copies; merge when next start <= current end, retaining the maximum end.'),
        implementation('zero-based-pagination', 'bug_repair',
            'Fix page(items, number, size): number is zero-based; negative numbers or size <= 0 raise ValueError; out-of-range returns [].',
            'def page(items, number, size):\n    return items[number:number+size]',
            'from main import page; assert page(list(range(7)),2,3)==[6]; assert page([1,2],0,1)==[1]; assert page([1],5,2)==[]\n'
            'for n,s in [(-1,2),(0,0),(1,-3)]:\n try: page([],n,s)\n except ValueError: pass\n else: raise AssertionError("invalid paging")',
            'Offset is number * size; validate both inputs before slicing.'),
        implementation('quoted-csv', 'bug_repair',
            'Fix parse_row(text): parse one CSV record with quoted commas and escaped double quotes using standard CSV rules; empty input returns [].',
            'def parse_row(text):\n    return text.split(",")',
            'from main import parse_row; assert parse_row(\'a,"b,c","say ""hi"""\')==["a","b,c",\'say "hi"\']; assert parse_row("")==[]; assert parse_row("a,,c")==["a","","c"]',
            'Use csv.reader; split cannot implement CSV quoting; handle an empty record explicitly.'),
        implementation('decimal-money', 'bug_repair',
            'Fix discounted(price, percent): inputs are decimal strings, return a string rounded to two decimal places with ROUND_HALF_UP; compute using Decimal.',
            'def discounted(price, percent):\n    return str(round(float(price)*(1-float(percent)/100),2))',
            'from main import discounted; assert discounted("1.005","0")=="1.01"; assert discounted("10","15")=="8.50"; assert discounted("0","5")=="0.00"; assert discounted("10","100")=="0.00"',
            'Decimal string conversion, percentage arithmetic, quantize Decimal("0.01") with ROUND_HALF_UP.'),
        implementation('nested-redaction', 'bug_repair',
            'Fix redact(value): recursively copy dicts/lists, replacing values of case-insensitive password/token dict keys with "***"; leave other scalar values unchanged.',
            'def redact(value):\n    return value',
            'from main import redact; a={"Password":"secret","children":[{"TOKEN":"abc","n":2}],"x":None}; assert redact(a)=={"Password":"***","children":[{"TOKEN":"***","n":2}],"x":None}; assert a["Password"]=="secret"; assert redact([1,"x"])==[1,"x"]',
            'Recursively construct new dict/list containers; compare string keys case-insensitively.'),
        implementation('sliding-window', 'bug_repair',
            'Fix windows(values, size): return every full consecutive window as a list; exclude partial windows; size <= 0 raises ValueError.',
            'def windows(values, size):\n    return [values[i:i+size] for i in range(len(values))]',
            'from main import windows; assert windows([1,2,3,4],3)==[[1,2,3],[2,3,4]]; assert windows([1],2)==[]; assert windows([],1)==[]\n'
            'try: windows([1],0)\nexcept ValueError: pass\nelse: raise AssertionError("invalid size")',
            'Use len(values)-size+1 valid starting positions and reject nonpositive sizes.'),
        implementation('frequency-ties', 'bug_repair',
            'Fix most_common(values): return the most frequent hashable value; break ties by first occurrence; empty input returns None.',
            'def most_common(values):\n    return max(values)',
            'from main import most_common; assert most_common(["b","a","a","b"])=="b"; assert most_common([3,1,1,2])==1; assert most_common([]) is None; assert most_common([None,None,2]) is None',
            'Count frequencies and preserve first-occurrence tie order; do not compare heterogeneous values.'),
        implementation('bounded-backoff', 'feature',
            'Implement delays(attempts, base, cap): produce attempts exponential delays min(base*2**i, cap); negative attempts/base/cap raise ValueError.',
            'def delays(attempts, base, cap):\n    raise NotImplementedError',
            'from main import delays; assert delays(5,2,9)==[2,4,8,9,9]; assert delays(0,1,3)==[]; assert delays(3,0,2)==[0,0,0]\n'
            'for a,b,c in [(-1,1,3),(1,-1,3),(1,1,-1)]:\n try: delays(a,b,c)\n except ValueError: pass\n else: raise AssertionError("invalid input")',
            'Validate nonnegative inputs; exponent begins at zero and each delay is capped.'),
        implementation('nested-lookup', 'feature',
            'Implement get_path(data, keys, default=None): traverse dict keys; return default for missing key or non-dict intermediate; empty keys returns data; preserve existing None.',
            'def get_path(data, keys, default=None):\n    raise NotImplementedError',
            'from main import get_path; d={"a":{"b":None}}; assert get_path(d,["a","b"],7) is None; assert get_path(d,["a","c"],7)==7; assert get_path(d,[]) is d; assert get_path({"a":2},["a","b"],7)==7',
            'Distinguish missing keys from present None using membership; handle non-dict intermediates.'),
        implementation('idempotent-events', 'feature',
            'Implement total_once(events): events are dicts with id and amount; sum amounts from first occurrence of each id, ignoring subsequent duplicates.',
            'def total_once(events):\n    raise NotImplementedError',
            'from main import total_once; a=[{"id":"x","amount":3},{"id":"x","amount":99},{"id":"y","amount":-1}]; assert total_once(a)==2; assert len(a)==3; assert total_once([])==0',
            'A seen-ID set ensures first occurrence wins, including when later amounts differ.'),
        implementation('form-required-fields', 'feature',
            'Implement missing_fields(form, required): return required field names whose value is absent, None, or a whitespace-only string; 0 and False are valid; preserve required order.',
            'def missing_fields(form, required):\n    raise NotImplementedError',
            'from main import missing_fields; assert missing_fields({"a":0,"b":False,"c":" ","d":None},["a","b","c","d","e"])==["c","d","e"]; assert missing_fields({},[])==[]; assert missing_fields({"x":"ok"},["x"])==[]',
            'Avoid broad truthiness checks: zero and False count as provided values.'),
    ]
    result += [
        code_audit('audit-pagination', 'service.py',
            'def page(items, number, size):\n    if number < 0 or size <= 0: raise ValueError()\n    return items[number:number+size]\n',
            ['Zero-based page number uses number * size as offset.',
             'Negative page numbers and nonpositive sizes are rejected.',
             'Reading a page does not mutate the source list.'], ['c1']),
        code_audit('audit-secret-logging', 'service.py',
            'def login(username, password, logger):\n    logger.info("login password=%s", password)\n    if not username: raise ValueError("missing username")\n    return {"username": username}\n',
            ['Passwords must not appear in log calls.', 'Missing username is rejected.',
             'Returned payload must not contain the password.'], ['c1']),
        code_audit('audit-modal-source', 'dialog.js',
            'const closeButton = document.querySelector("#close");\n'
            'const dialog = document.querySelector("dialog");\n'
            'closeButton.addEventListener("click", () => dialog.close());\n'
            'document.addEventListener("keydown", e => { if (e.key === "Escape") e.preventDefault(); });\n',
            ['The close button calls dialog.close().',
             'Escape must close the dialog, not merely cancel the default key action.',
             'This source must not write user data to localStorage.'], ['c2']),
        code_audit('audit-responsive-source', 'style.css',
            '.card { box-sizing: border-box; width: 900px; padding: 24px; }\n'
            '.title { overflow-wrap: anywhere; }\n',
            ['Card width must fit a 320px parent without horizontal overflow.',
             'Padding is included in the declared card width.',
             'Long unbroken titles may wrap.'], ['c1']),
    ]
    return result
