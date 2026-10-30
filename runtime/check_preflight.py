"""Non-executing preflight for frozen check configurations.

Only syntax/semantic facts that can be proven statically are hard errors:
Python parse/compile failures and direct constant lookups on the documented
audit report top level.  Missing paths and dynamic expressions are advisory:
the implementation itself may create a path, and Python execution is never
performed here.
"""
import ast
import os
from pathlib import Path
import re

from audit_schema import REPORT_TOP_LEVEL

PYTHON_FORM = ('{python}', '-c')


def _diag(check_id, severity, code, message):
    return {'check_id': check_id, 'severity': severity, 'code': code, 'message': message}


def _program(check):
    argv = check.get('argv')
    if (isinstance(argv, list) and len(argv) >= 3 and argv[0] == '{python}'
            and argv[1] == '-c' and isinstance(argv[2], str)):
        return argv[2]
    return None


def _report_bindings(tree):
    """Recognize direct standard-library report loads; uncertain flow is advisory.

    A path/env literal alone is never a report. Only top-level assignments using
    imported json.load(s) and a proven report stream/text can create a binding.
    Reassigned names are not treated as proven, even if one assignment is a load.
    """
    imports, values, assigned = {}, {}, {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            assigned[node.id] = assigned.get(node.id, 0) + 1
        if isinstance(node, (ast.Attribute, ast.Subscript)) and isinstance(node.ctx, (ast.Store, ast.Del)):
            base = node.value
            while isinstance(base, (ast.Attribute, ast.Subscript)):
                base = base.value
            if isinstance(base, ast.Name):
                assigned[base.id] = assigned.get(base.id, 0) + 2
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            for alias in node.names:
                imports[alias.asname or alias.name] = (node.module or '') + '.' + alias.name

    def symbol(node):
        if isinstance(node, ast.Name) and assigned.get(node.id, 0) == 0:
            return imports.get(node.id, 'open' if node.id == 'open' else None)
        if isinstance(node, ast.Attribute):
            base = symbol(node.value)
            return base + '.' + node.attr if base else None
        return None

    def kind(node):
        if isinstance(node, ast.Name):
            return values.get(node.id) if assigned.get(node.id) == 1 else None
        if (isinstance(node, ast.Subscript) and symbol(node.value) == 'os.environ'
                and isinstance(node.slice, ast.Constant) and node.slice.value == 'TELOS_AUDIT_REPORT'):
            return 'path'
        if not isinstance(node, ast.Call):
            return None
        name = symbol(node.func)
        first = node.args[0] if node.args else None
        if name in ('os.getenv', 'os.environ.get') and isinstance(first, ast.Constant):
            return 'path' if first.value == 'TELOS_AUDIT_REPORT' else None
        source = kind(first) if first is not None else None
        if name == 'pathlib.Path' and source == 'path':
            return 'path'
        if name == 'open' and source == 'path':
            return 'stream'
        if isinstance(node.func, ast.Attribute):
            receiver = kind(node.func.value)
            if receiver == 'path' and node.func.attr == 'open':
                return 'stream'
            if receiver == 'path' and node.func.attr in ('read_text', 'read_bytes'):
                return 'text'
            if receiver == 'stream' and node.func.attr == 'read':
                return 'text'
        if (name == 'json.loads' and source == 'text') or (name == 'json.load' and source == 'stream'):
            return 'report'
        return None

    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            values[node.targets[0].id] = kind(node.value)
    # Arbitrary calls can mutate a loaded dict; they are outside our proof.
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                if values.get(node.func.value.id) == 'report' and node.func.attr != 'get':
                    assigned[node.func.value.id] += 2
            for arg in node.args:
                if isinstance(arg, ast.Name) and values.get(arg.id) == 'report':
                    assigned[arg.id] += 2
    return {name for name, value in values.items() if value == 'report'}, assigned


def _disabled(node, parents):
    current = node
    while current in parents:
        parent = parents[current]
        if isinstance(parent, ast.If) and isinstance(parent.test, ast.Constant):
            if current in (parent.orelse if parent.test.value else parent.body):
                return True
        current = parent
    return False


def _direct_conflicts(tree):
    errors, warnings = [], []
    bindings, assigned = _report_bindings(tree)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    for node in ast.walk(tree):
        key, receiver = None, None
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
            key, receiver = node.slice, node.value.id
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr == 'get' and isinstance(node.func.value, ast.Name) and node.args):
            key, receiver = node.args[0], node.func.value.id
        if receiver not in bindings or _disabled(node, parents):
            continue
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            warnings.append(('audit_report_dynamic_key', 'audit report field is dynamic; static preflight cannot prove it'))
            continue
        if key.value in REPORT_TOP_LEVEL:
            continue
        parent = parents.get(node)
        required = isinstance(node, ast.Subscript)
        if isinstance(node, ast.Call) and len(node.args) == 1 and not node.keywords:
            required = (isinstance(parent, ast.Assert) and parent.test is node) or (
                isinstance(parent, ast.Compare) and parent.left is node and len(parent.ops) == 1
                and isinstance(parent.ops[0], ast.IsNot)
                and isinstance(parent.comparators[0], ast.Constant) and parent.comparators[0].value is None
                and isinstance(parents.get(parent), ast.Assert))
        # Branches, handlers and function scopes can make an apparently missing
        # field safe. Do not pretend to prove arbitrary Python control flow.
        ancestor = node
        conditional = False
        while ancestor in parents:
            ancestor = parents[ancestor]
            if isinstance(ancestor, (ast.If, ast.Try, ast.FunctionDef, ast.AsyncFunctionDef,
                                     ast.Lambda, ast.IfExp, ast.BoolOp, ast.For, ast.While)):
                conditional = True
        if required and assigned.get(receiver) == 1 and not conditional:
            errors.append(('audit_report_top_level',
                           "direct audit report top-level field %r is not supported; expected one of %s"
                           % (key.value, ', '.join(REPORT_TOP_LEVEL))))
        else:
            warnings.append(('audit_report_optional_field',
                             'unsupported audit report field may be optional; review its control flow'))
    return errors, warnings


def _test_paths(tree):
    paths = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            stack = [arg]
            while stack:
                item = stack.pop()
                if isinstance(item, (ast.List, ast.Tuple, ast.Set)):
                    stack.extend(item.elts)
                elif (isinstance(item, ast.Constant) and isinstance(item.value, str)
                      and item.value.endswith('.py') and ('/' in item.value or item.value.startswith('tests'))
                      and not re.match(r'(?i)^[a-z]+://', item.value)):
                    paths.add(item.value)
    return sorted(paths)


def _literal_paths(tree):
    """Conservative path literals passed to filesystem-looking calls."""
    paths = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        name = None
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr
        if name not in ('Path', 'open', 'exists', 'is_file', 'is_dir', 'read_text', 'read_bytes'):
            continue
        for arg in node.args:
            if (isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                    and len(arg.value) < 500 and '\n' not in arg.value
                    and not re.match(r'(?i)^[a-z]+://', arg.value)
                    and ('/' in arg.value or '\\' in arg.value)
                    and not arg.value.startswith(('..', '/', '~'))):
                paths.add(arg.value)
    return sorted(paths)


def analyze(contract=None, checks=None, root=None, create=False):
    """Return diagnostics without writing or executing user programs."""
    if checks is None:
        checks = (contract or {}).get('checks', [])
    if not isinstance(checks, list):
        raise ValueError('checks must be a list')
    audit = (contract or {}).get('task_type') == 'audit'
    diagnostics, programs = [], 0
    for index, check in enumerate(checks):
        if not isinstance(check, dict):
            diagnostics.append(_diag(str(index), 'error', 'invalid_check', 'check must be an object'))
            continue
        check_id = check.get('id') if isinstance(check.get('id'), str) else str(index)
        argv = check.get('argv')
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
            diagnostics.append(_diag(check_id, 'error', 'invalid_argv', 'argv must be a nonempty string list'))
            continue
        code = _program(check)
        if code is None:
            continue
        programs += 1
        try:
            tree = ast.parse(code, filename='<check %s>' % check_id)
        except SyntaxError as error:
            diagnostics.append(_diag(check_id, 'error', 'syntax_error',
                                     'Python syntax error at line %s: %s' % (error.lineno, error.msg)))
            continue
        try:
            compile(code, '<check %s>' % check_id, 'exec')
        except (SyntaxError, ValueError) as error:
            diagnostics.append(_diag(check_id, 'error', 'compile_error',
                                     'Python compile error: %s' % getattr(error, 'msg', error)))
            continue
        if audit:
            errors, warnings = _direct_conflicts(tree)
            for code_name, message in errors:
                diagnostics.append(_diag(check_id, 'error', code_name, message))
            for code_name, message in warnings:
                diagnostics.append(_diag(check_id, 'warning', code_name, message))
        for name in _literal_paths(tree):
            candidate = Path(root, name) if root else Path(name)
            if not candidate.exists():
                diagnostics.append(_diag(
                    check_id, 'warning', 'path_not_found',
                    'path %r does not exist during preflight; it may be generated by the implementation' % name))
        for name in _test_paths(tree):
            candidate = Path(root, name) if root else Path(name)
            if not candidate.exists():
                diagnostics.append(_diag(
                    check_id, 'warning', 'test_path_not_found',
                    'explicit test path %r does not exist during preflight; it may be generated later' % name))
    errors = sum(item['severity'] == 'error' for item in diagnostics)
    warnings = sum(item['severity'] == 'warning' for item in diagnostics)
    return {'ok': errors == 0, 'checks': len(checks), 'programs': programs,
            'errors': errors, 'warnings': warnings, 'diagnostics': diagnostics,
            'executed': False, 'wrote_state': False}


def preflight(b=None, sid=None, contract=None, checks=None, root=None):
    """Read-only tool entry point; never writes state."""
    create = contract is not None
    if sid is not None:
        if contract is not None or checks is not None:
            raise ValueError('Pass either session_id or an explicit contract/checks')
        with b.locked(sid) as run_root:
            state, frozen = b.load_session(run_root, read_only=True)
            contract = frozen
            root = root or state.get('worktree') or state.get('repo')
    result = analyze(contract=contract, checks=checks, root=root, create=create)
    result['executed'] = False
    result['wrote_state'] = False
    return result


def require_valid(contract, root=None):
    """Fail a new creation for deterministic configuration errors only."""
    result = analyze(contract=contract, root=root, create=True)
    errors = [item for item in result['diagnostics'] if item['severity'] == 'error']
    if errors:
        first = errors[0]
        raise ValueError('Check preflight rejected %s: %s'
                         % (first.get('check_id'), first['message']))
    return result
