"""MCP schemas for audit boundaries, review and constrained probes."""
STR = {'type': 'string'}


def obj(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False}


def array(items=STR):
    return {'type': 'array', 'items': items}


AUDIT = obj({
    'environment': obj({'kind': {'enum': ['local', 'test', 'staging']}, 'name': STR}, ('kind', 'name')),
    'scope_paths': array(), 'requirements': array(), 'out_of_scope': array(), 'stop_conditions': array(),
    'mutation_policy': STR,
    'checklist': array(obj({'id': STR, 'kind': {'enum': ['code', 'api', 'interaction', 'visual']},
                          'prompt': STR, 'expected': STR, 'required': {'type': 'boolean'}, 'viewports': array()}, ('id', 'kind', 'prompt', 'expected'))),
    'targets': array(obj({'origin': STR, 'path_prefix': STR, 'methods': array()}, ('origin', 'path_prefix', 'methods'))),
    'limits': obj({'max_probes': {'type': 'integer'}, 'probe_timeout_seconds': {'type': 'integer'},
                   'max_artifact_bytes': {'type': 'integer'}}),
    'browser': obj({'module_path': STR, 'executable_path': STR, 'actions': array(),
                    'viewports': array(obj({'name': STR, 'width': {'type': 'integer'}, 'height': {'type': 'integer'}},
                                          ('name', 'width', 'height')))},
                   ('module_path', 'executable_path', 'viewports', 'actions')),
}, ('environment', 'scope_paths', 'requirements', 'out_of_scope', 'stop_conditions', 'mutation_policy', 'checklist', 'targets'))

REPAIR = obj({'session_id': STR, 'finding_ids': array()}, ('session_id', 'finding_ids'))
REVIEW = obj({'findings': array(obj({'id': STR, 'disposition': {'enum': ['confirmed', 'unconfirmed', 'dismissed', 'suggestion']},
                                    'rationale': STR}, ('id', 'disposition', 'rationale'))),
              'limitations': array(), 'visual_evidence_reviewed': array(), 'coverage_rationale': STR},
             ('findings', 'limitations', 'visual_evidence_reviewed', 'coverage_rationale'))
PROBE = obj({'request_id': STR, 'check_id': STR, 'kind': {'enum': ['http', 'browser']}, 'url': STR,
             'method': STR, 'headers': {'type': 'object', 'additionalProperties': STR},
             'body': {'type': ['string', 'null']}, 'viewport': STR,
             'steps': array(obj({'action': STR, 'selector': STR, 'value': STR}, ('action',)))},
            ('request_id', 'check_id', 'kind', 'url'))


def extend(tools, project_task):
    by_name = {t['name']: t for t in tools}
    for schema in (by_name['telos_open']['inputSchema'], project_task):
        schema['properties'].update(task_type={'enum': ['implementation', 'audit']}, audit=AUDIT, repair_of=REPAIR)
    by_name['telos_open']['inputSchema']['properties']['allowed_paths'] = array()
    by_name['telos_grade']['inputSchema']['properties']['audit_review'] = REVIEW
    by_name['telos_open']['description'] += (
        ' For system audits set task_type=audit and freeze public audit boundaries/checklist separately from private '
        'quality questions/checks. Source is read-only, reports/evidence go to a separate artifact directory. '
        'Use scoped HTTP/browser probes; no unrestricted command/check network. Page audits require an existing '
        'Playwright module and browser executable, explicit targets/actions/viewports. Checks assess investigation '
        'quality, not absence of target bugs; read TELOS_AUDIT_REPORT in quality checks. Required checklist items '
        'must be investigated; optional untested items need reasons. For fixes of supervisor-confirmed findings '
        'use repair_of and allowed_paths; no automatic repair/merge/deploy.')
    by_name['telos_grade']['description'] += (
        ' Audit grading additionally requires audit_review: decide each finding, acknowledge all limitations and '
        'actually inspect each captured screenshot. Audit accepted=true means evidence-backed coverage, never a healthy system.')
    by_name['telos_project_open']['description'] += (
        ' Audit children use task_type=audit, audit boundaries and allowed_paths=[]; read-only auditors may overlap. '
        'Split by independently reviewable scope; include interface/visual checks and shared end-to-end flows as needed. '
        'Add authorized repair tasks after findings are confirmed, using repair_of and dependencies. '
        'Final checks can read TELOS_AUDIT_REPORTS; final review includes child audit reports.')
    tools.append({'name': 'telos_audit_probe', 'description':
                  'Run a bounded HTTP or declarative browser probe within an active audit contract. Original supervisor only; '
                  'executors use their authenticated local probe service. Runs before evaluation; request_id deduplicates '
                  'operations, including uncertain outcomes. Captures evidence; business failures are findings, not acceptance. '
                  'No arbitrary JS, redirects, WebSockets, installations or target/permission expansion.',
                  'inputSchema': obj({'session_id': STR, 'request': PROBE}, ('session_id', 'request'))})
