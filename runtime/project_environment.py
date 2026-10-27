"""Declared project capabilities, verified in the actual work/check sandbox.

Shared cache holds public facts only, never writable dependencies or build output.
Every invocation rechecks versions, lock/config bytes, local services and private
workspace permissions. Bootstrap markers are private and tied to source/output hashes.
"""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import sys
import time
import uuid
from urllib.parse import urlsplit

from identity import VERSION
from providers import ProviderError
from processes import terminate_and_drain
from serialization import digest

VARIABLES = {'CI', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
             'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy',
             'npm_config_registry', 'npm_config_offline', 'PIP_INDEX_URL',
             'PIP_EXTRA_INDEX_URL', 'PIP_NO_INDEX', 'PIP_FIND_LINKS'}
FIELDS = {'version', 'tools', 'lockfiles', 'config_files', 'download_caches', 'variables',
          'services', 'registries', 'browser', 'bootstrap', 'local_ipc'}


class EnvironmentBlocked(ProviderError):
    code = 'blocked_environment'
    next_action = 'repair_declared_environment'

    def __init__(self, label, reason, *, phase='work', evidence=None):
        super().__init__('Project environment blocked at ' + label + ': ' + reason)
        self.evidence = evidence or {
            'status': 'blocked_environment', 'phase': phase, 'failed_check': label,
            'reason': reason,
            'repair_actions': ['Restore the declared capability or explicitly create a task with corrected permissions/configuration. '
                               'No dependency installation or permission expansion was attempted implicitly.']}


def _object(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise ProviderError('Invalid project environment fields: ' + ', '.join(required))


def _string(value):
    if not isinstance(value, str) or not value.strip() or '\x00' in value or len(value) > 8000:
        raise ProviderError('Environment values must be bounded nonempty strings')
    if re.search(r'(?i)(bearer\s+\S+|(?:token|password|api[_-]?key|secret)\s*[=:]\s*\S+)', value):
        raise ProviderError('Environment declarations must contain references, not credentials')
    return value


def _relative(value):
    _string(value)
    if Path(value).is_absolute() or any(p in ('', '.', '..', '.git') for p in value.split('/')) or '\\' in value:
        raise ProviderError('Environment file/output paths must be literal relative paths')
    return value


def _absolute(value):
    _string(value)
    if not Path(value).is_absolute() or '..' in Path(value).parts:
        raise ProviderError('Environment executable/dependency paths must be explicit absolute paths')
    return value


def _phases(row):
    phases = row.get('phases', ['work', 'checks'])
    if not isinstance(phases, list) or not phases or any(x not in ('work', 'checks') for x in phases):
        raise ProviderError('Environment phases must select work and/or checks')


def validate(value, repo=None):
    if value is None:
        return None
    _object(value, FIELDS, ('version',))
    if type(value['version']) is not int or value['version'] != 1:
        raise ProviderError('Unsupported environment requirement version')
    value = json.loads(json.dumps(value))
    if type(value.get('local_ipc', False)) is not bool:
        raise ProviderError('local_ipc must be explicitly true or false')
    for name in ('tools', 'lockfiles', 'config_files', 'download_caches', 'services', 'registries', 'bootstrap'):
        rows = value.get(name, [])
        if not isinstance(rows, list) or len(rows) > 30:
            raise ProviderError('Environment lists must contain at most 30 entries')
    for name in ('lockfiles', 'config_files'):
        for path in value.get(name, []):
            _relative(path)
    for path in value.get('download_caches', []):
        _absolute(path)
    variables = value.get('variables', {})
    _object(variables, VARIABLES)
    for name, item in variables.items():
        _string(item)
        if 'proxy' in name.lower() or 'index_url' in name.lower() or name == 'npm_config_registry':
            for url in item.split():
                u = urlsplit(url)
                if u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password or u.query or u.fragment:
                    raise ProviderError('Registry/proxy URLs must be HTTP(S), credential-free and query-free')
    ids = set()
    for row in value.get('tools', []):
        _object(row, ('id','path','kind','version_args','expected_version'), ('id','path'))
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', row['id']) or row['id'] in ids:
            raise ProviderError('Tool IDs must be unique')
        ids.add(row['id']); _absolute(row['path'])
        if repo and Path(row['path']).resolve().is_relative_to(Path(repo).resolve()):
            raise ProviderError('Runtime version probes cannot execute repository scripts; declare bootstrap explicitly')
        if row.get('kind', 'tool') not in ('tool', 'python', 'node', 'package_manager'):
            raise ProviderError('Unsupported declared runtime kind')
        args = row.get('version_args', ['--version'])
        if not isinstance(args, list) or not args or len(args) > 20:
            raise ProviderError('Version arguments must be explicit')
        for arg in args: _string(arg)
        if 'expected_version' in row: _string(row['expected_version'])
    for row in value.get('services', []):
        _object(row, ('id','host','port','phases'), ('id','host','port'))
        _string(row['id']); _phases(row)
        if row['host'] not in ('localhost','127.0.0.1','::1') or type(row['port']) is not int or not 1 <= row['port'] <= 65535:
            raise ProviderError('Test services require declared loopback host and port')
    for row in value.get('registries', []):
        _object(row, ('id','url','phases'), ('id','url'))
        _string(row['id']); _phases(row); _string(row['url'])
        u = urlsplit(row['url'])
        if u.scheme not in ('http','https') or not u.hostname or u.username or u.password or u.query or u.fragment:
            raise ProviderError('Registry probes must not contain credentials, query or fragment')
    browser = value.get('browser')
    if browser is not None:
        _object(browser, ('node','module','executable','phases'), ('node','module','executable'))
        for name in ('node','module','executable'): _absolute(browser[name])
        _phases(browser)
    for row in value.get('bootstrap', []):
        _object(row, ('id','argv','outputs','phases','timeout_seconds'), ('id','argv','outputs'))
        _string(row['id']); _phases(row)
        if not isinstance(row['argv'], list) or not row['argv'] or len(row['argv']) > 100:
            raise ProviderError('Bootstrap requires an authorized argv, never implicit repository script discovery')
        for arg in row['argv']: _string(arg)
        if not isinstance(row['outputs'], list) or not row['outputs']:
            raise ProviderError('Bootstrap requires declared scratch-relative outputs for integrity validation')
        for path in row['outputs']: _relative(path)
        if type(row.get('timeout_seconds', 120)) is not int or not 1 <= row.get('timeout_seconds',120) <= 600:
            raise ProviderError('Bootstrap timeout must be 1-600 seconds')
    return value


def service_ports(contract, phase):
    req = contract.get('environment_requirements') or {}
    return [r['port'] for r in req.get('services', []) if phase in r.get('phases', ['work','checks'])]


def _directory(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    s = path.lstat()
    if not stat.S_ISDIR(s.st_mode) or s.st_uid != os.getuid() or s.st_mode & 0o077:
        raise EnvironmentBlocked('scratch', 'Expected an owner-only directory: ' + str(path))
    return path


def work_scratch(b, sid):
    # Actual short paths, not aliases whose canonical form breaks AF_UNIX length.
    key = digest([str(b.CACHE.resolve()), sid])[:24]
    return _directory(Path('/tmp').resolve() / ('tl-' + str(os.getuid()) + '-' + key))


@contextlib.contextmanager
def check_scratch():
    import tempfile
    with tempfile.TemporaryDirectory(prefix='tl-check-', dir=str(Path('/tmp').resolve())) as name:
        yield Path(name)


def build_environment(b, contract, source, scratch, initial, *, phase):
    """The same HOME/TMP/output convention for work, public self-check and acceptance."""
    from check_runtime import dependency_home
    scratch = _directory(scratch)
    home, temp, output = [_directory(scratch/n) for n in ('home','tmp','output')]
    protected = dependency_home(contract.get('dependency_paths', []), b.HOME, home, links=True)
    env = dict(initial)
    env.update(HOME=str(home), TMPDIR=str(temp), TMPPREFIX=str(temp/'zsh'),
               TELOS_CHECK_OUTPUT=str(output), TELOS_ENV_SCRATCH=str(scratch),
               PYTHONDONTWRITEBYTECODE='1')
    req = contract.get('environment_requirements') or {}
    env.update(req.get('variables', {}))
    paths = [str(Path(t['path']).parent) for t in req.get('tools', [])]
    if paths: env['PATH'] = os.pathsep.join(dict.fromkeys(paths + env.get('PATH','/usr/bin:/bin').split(os.pathsep)))
    if req.get('download_caches'):
        env['TELOS_DOWNLOAD_CACHES'] = json.dumps(req['download_caches'])
    return env, protected


def _run(profile, argv, cwd, env, label, phase, timeout=20):
    public_env = {k:v for k,v in env.items() if not re.search(r'(?i)(token|password|secret|api_?key)',k)}
    try:
        p = subprocess.Popen(['/usr/bin/sandbox-exec','-p',profile,*argv], cwd=cwd, env=public_env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        try:
            out, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as expired:
            terminate_and_drain(p, expired)
            raise EnvironmentBlocked(label, 'Sandbox probe timed out; inspect the declared capability', phase=phase)
    except OSError as error:
        raise EnvironmentBlocked(label, 'Cannot start declared probe: ' + type(error).__name__, phase=phase) from None
    if p.returncode:
        # Avoid copying arbitrary tool/bootstrap output (possibly secrets) to public evidence.
        raise EnvironmentBlocked(label, 'Sandbox command exited ' + str(p.returncode) +
                                 '; check declared path, version, access and service availability', phase=phase)
    return out.decode(errors='replace').strip()


def _python(profile, code, args, cwd, env, label, phase):
    return _run(profile, [sys.executable,'-I','-c',code,*args], cwd, env, label, phase)


@contextlib.contextmanager
def _lock(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('a') as stream:
        deadline = time.monotonic()+60
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise EnvironmentBlocked('shared_environment', 'Another bounded preflight is still filling this cache')
                time.sleep(.05)
        yield


def _tree_hash(root, *, confined=None):
    root = Path(root)
    boundary = Path(confined).absolute() if confined is not None else None
    budget = {'files': 0, 'bytes': 0}
    deadline = time.monotonic() + 30

    def entry(path):
        budget['files'] += 1
        if budget['files'] > 100000 or time.monotonic() > deadline:
            raise EnvironmentBlocked('integrity', 'Artifact tree exceeds bounded integrity scan')
        if path.is_symlink():
            if boundary is not None and not path.resolve().is_relative_to(boundary):
                raise EnvironmentBlocked('integrity', 'Output link points outside its declared artifact tree')
            return ['link', os.readlink(path)]
        mode = stat.S_IMODE(path.stat().st_mode)
        if path.is_dir():
            return ['directory', mode]
        if not path.is_file():
            raise EnvironmentBlocked('integrity', 'Artifact contains an unsupported special file')
        h = hashlib.sha256()
        with path.open('rb') as stream:
            while chunk := stream.read(1024 * 1024):
                budget['bytes'] += len(chunk)
                if budget['bytes'] > 4 * 1024**3 or time.monotonic() > deadline:
                    raise EnvironmentBlocked('integrity', 'Artifact tree exceeds bounded integrity scan')
                h.update(chunk)
        return ['file', mode, h.hexdigest()]

    first = entry(root)
    if root.is_symlink() or not root.is_dir():
        return digest(first)
    rows = [['.', first]]
    for base, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(n for n in dirs if n != '.git')
        for name in sorted(dirs + [n for n in files if n != '.git']):
            path = Path(base) / name
            rows.append([str(path.relative_to(root)), entry(path)])
    return digest(rows)


def _read_cache(b, path, key):
    try:
        record = b.read_json(path)
        proof = record.pop('proof')
        if proof != digest(record) or record['key'] != key or record['version'] != 1:
            return None
        if record.get('status') in ('passed', 'blocked_environment') and 'at' in record and time.time()-record['at'] > (300 if record['status']=='passed' else 30):
            return None
        return record
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _save_cache(b, path, record):
    b.write_json(path, {**record, 'proof': digest(record)})


def probe(b, contract, source, scratch, env, profile, *, phase='work'):
    req = contract.get('environment_requirements')
    if req is None: return None
    req = validate(req, source)
    source, scratch = Path(source), Path(scratch)
    tools=[]
    for tool in req.get('tools', []):
        label='tool:'+tool['id']
        out = _run(profile,[tool['path'],*tool.get('version_args',['--version'])],source,env,label,phase)
        if tool.get('expected_version') is not None and out != tool['expected_version']:
            raise EnvironmentBlocked(label,'Installed version does not equal the declared expected_version',phase=phase)
        abi=''
        if tool.get('kind') == 'python':
            abi=_run(profile,[tool['path'],'-I','-c',
                'import sysconfig,platform; print(sysconfig.get_config_var("SOABI"),platform.machine())'],source,env,label,phase)
        elif tool.get('kind') == 'node':
            abi=_run(profile,[tool['path'],'-p','JSON.stringify([process.versions.modules,process.arch,process.platform])'],source,env,label,phase)
        # This fingerprint is read inside the sandbox, including the real symlink target.
        identity=_python(profile,'import hashlib,pathlib,sys; p=pathlib.Path(sys.argv[1]); '
                         'print(str(p.resolve()),hashlib.file_digest(p.open("rb"),"sha256").hexdigest())',
                         [tool['path']],source,env,label,phase)
        tools.append({'id':tool['id'],'path':tool['path'],'version':out[:1000],'abi':abi[:1000],'identity':identity})
    browser_identity = []
    if req.get('browser') and phase in req['browser'].get('phases',['work','checks']):
        for name in ('node','module','executable'):
            path = req['browser'][name]
            browser_identity.append(_python(profile,'import hashlib,pathlib,sys; p=pathlib.Path(sys.argv[1]); '
                'print(str(p.resolve()),hashlib.file_digest(p.open("rb"),"sha256").hexdigest())',
                [path],source,env,'browser:'+name,phase))
    files=req.get('lockfiles',[])+req.get('config_files',[])
    file_hashes=json.loads(_python(profile,
        'import pathlib,hashlib,json,sys; root=pathlib.Path(sys.argv[1]).resolve(); out={}; '
        '\nfor name in json.loads(sys.argv[2]):\n p=(root/name).resolve(); assert p.is_relative_to(root); '
        'out[name]=hashlib.file_digest(p.open("rb"),"sha256").hexdigest()\nprint(json.dumps(out))',
        [str(source),json.dumps(files)],source,env,'lockfiles/configuration',phase))
    environment_probe = (
        'import os,pathlib,socket,sys,uuid,json; facts={}\n'
        'for key in ("HOME","TMPDIR","TELOS_CHECK_OUTPUT"):\n'
        ' p=pathlib.Path(os.environ[key]); f=p/("probe-"+uuid.uuid4().hex); '
        'f.write_text("probe"); assert f.read_text()=="probe"; f.unlink(); facts[key]="read_write"\n'
        'assert pathlib.Path.cwd().is_dir()\n'
        'if sys.argv[1]=="ipc":\n'
        ' p=str(pathlib.Path(os.environ["TMPDIR"])/("s-"+uuid.uuid4().hex[:8])); '
        'assert len(p.encode())<104; s=socket.socket(socket.AF_UNIX); s.bind(p); s.close(); os.unlink(p)\n'
        'print(json.dumps(facts))')
    permissions=json.loads(_python(profile,environment_probe,['ipc' if req.get('local_ipc') else 'paths'],
                                   source,env,'workspace_permissions',phase))
    cache_inputs=[]
    for path in req.get('download_caches', []):
        fact=_python(profile,'import os,pathlib,json,sys; p=pathlib.Path(sys.argv[1]); '
                     'next(iter(p.iterdir()),None); s=p.stat(); print(json.dumps([str(p.resolve()),s.st_mtime_ns,s.st_ino]))',
                     [path],source,env,'download_cache',phase)
        cache_inputs.append(json.loads(fact))
    for service in req.get('services', []):
        if phase not in service.get('phases',['work','checks']): continue
        _python(profile,'import socket,sys; s=socket.create_connection((sys.argv[1],int(sys.argv[2])),3); s.close()',
                [service['host'],str(service['port'])],source,env,'service:'+service['id'],phase)
    # Remove only this workspace's literals from the capability signature. Read-only
    # versus writable source, network, dependencies and all other restrictions remain.
    normalized=profile
    for name, path in sorted({'SOURCE':str(source), 'SCRATCH':str(scratch),
                              **({'CODEX_HOME':env['CODEX_HOME']} if env.get('CODEX_HOME') else {})}.items(),key=lambda x:-len(x[1])):
        normalized=normalized.replace(path,'{'+name+'}')
    normalized=re.sub(r'localhost:[0-9]+', 'localhost:DECLARED_OR_RELAY', normalized)
    key=digest({'version':1,'runtime_version':VERSION,'requirements':req,'tools':tools,'files':file_hashes,'download_sources':cache_inputs,
                'platform':[sys.platform,platform.machine(),platform.release()],
                'phase':phase,'sandbox':normalized,'browser_identity':browser_identity})
    cache=b.CACHE/'project-environments'/ (key+'.json')
    with _lock(cache.with_suffix('.lock')):
        prior=_read_cache(b,cache,key)
        hit=bool(prior and prior.get('status')=='passed')
        if prior and prior.get('status')=='blocked_environment':
            raise EnvironmentBlocked('shared_environment','Previous matching sandbox preflight failed; diagnostic retained',
                                     phase=phase,evidence=prior['evidence'])
        if not hit:
            browser_version = None
            try:
                for registry in req.get('registries',[]):
                    if phase not in registry.get('phases',['work','checks']): continue
                    _python(profile,
                        'import urllib.request,sys\n'
                        'class NoRedirect(urllib.request.HTTPRedirectHandler):\n'
                        ' def redirect_request(self,*args): return None\n'
                        'r=urllib.request.build_opener(NoRedirect).open(urllib.request.Request(sys.argv[1],method="HEAD"),timeout=5); '
                        'assert 200<=r.status<300; r.close()', [registry['url']],source,env,'registry:'+registry['id'],phase)
                browser=req.get('browser')
                if browser and phase in browser.get('phases',['work','checks']):
                    browser_version = _run(profile,[browser['node'],'-e',
                        'const p=require(process.argv[1]); (async()=>{const b=await p.chromium.launch({'
                        'executablePath:process.argv[2],headless:true}); const page=await b.newPage(); '
                        'await page.setContent("<p>probe</p>"); console.log(b.version()); await b.close();})()'
                        '.catch(()=>process.exit(1));',browser['module'],browser['executable']],
                        source,env,'browser_launch',phase)
                _save_cache(b,cache,{'version':1,'key':key,'status':'passed','at':time.time(),'tools':tools,
                                   'browser_version':browser_version})
            except EnvironmentBlocked as error:
                _save_cache(b,cache,{'version':1,'key':key,'status':'blocked_environment','at':time.time(),'evidence':error.evidence})
                raise
    browser_fact = (prior.get('browser_version') if hit else browser_version)
    preparations=[]
    for command in req.get('bootstrap',[]):
        if phase not in command.get('phases',['work','checks']): continue
        public={'id':command['id']}
        try:
            source_hash=_tree_hash(source)
            prep_key=digest([key,str(scratch),source_hash,command])
            marker=b.CACHE/'project-environments'/'prepared'/(prep_key+'.json')
            with _lock(marker.with_suffix('.lock')):
                previous=_read_cache(b,marker,prep_key)
                def outputs():
                    result={}
                    for name in command['outputs']:
                        path=scratch/name
                        if not path.resolve().is_relative_to(scratch.resolve()):
                            raise EnvironmentBlocked('bootstrap:'+command['id'],'Output redirected outside private scratch',phase=phase)
                        result[name]=_tree_hash(path, confined=path)
                    return result
                try: intact=previous and previous.get('status')=='passed' and previous.get('outputs')==outputs()
                except (OSError,EnvironmentBlocked): intact=False
                if intact:
                    public['status']='reused'
                else:
                    replacements={'{repo}':str(source),'{scratch}':str(scratch),'{python}':sys.executable}
                    argv=[]
                    for arg in command['argv']:
                        for before,after in replacements.items(): arg=arg.replace(before,after)
                        argv.append(arg)
                    _save_cache(b,marker,{'version':1,'key':prep_key,'status':'preparing','at':time.time()})
                    _run(profile,argv,scratch,env,'bootstrap:'+command['id'],phase,command.get('timeout_seconds',120))
                    _save_cache(b,marker,{'version':1,'key':prep_key,'status':'passed','outputs':outputs()})
                    public['status']='executed'
        except OSError:
            raise EnvironmentBlocked('bootstrap:'+command['id'],'Declared output is missing/unreadable',phase=phase) from None
        preparations.append(public)
    return {'status':'passed','phase':phase,'tools':tools,'files':file_hashes,'workspace_permissions':permissions,
            'cache_key':key,'cache_path':str(cache),'cache_hit':hit,'bootstrap':preparations,
            'download_caches':req.get('download_caches',[]),'browser_version':browser_fact,'cwd':str(source)}


def schema(obj):
    string = {'type':'string'}
    strings = {'type':'array','items':string,'maxItems':30}
    phases = {'type':'array','items':{'enum':['work','checks']},'minItems':1}
    def rows(item): return {'type':'array','items':item,'maxItems':30}
    tool = obj({'id':string,'path':string,'kind':{'enum':['tool','python','node','package_manager']},
                'version_args':strings,'expected_version':string}, ['id','path'])
    service = obj({'id':string,'host':{'enum':['localhost','127.0.0.1','::1']},
                   'port':{'type':'integer','minimum':1,'maximum':65535},'phases':phases}, ['id','host','port'])
    registry = obj({'id':string,'url':string,'phases':phases}, ['id','url'])
    browser = obj({'node':string,'module':string,'executable':string,'phases':phases}, ['node','module','executable'])
    bootstrap = obj({'id':string,'argv':strings,'outputs':strings,'phases':phases,
                     'timeout_seconds':{'type':'integer','minimum':1,'maximum':600}}, ['id','argv','outputs'])
    return obj({'version':{'type':'integer','enum':[1]},'tools':rows(tool),'lockfiles':strings,
                'config_files':strings,'download_caches':strings,'variables':obj({k:string for k in sorted(VARIABLES)}, []),
                'services':rows(service),'registries':rows(registry),'browser':browser,
                'bootstrap':rows(bootstrap),'local_ipc':{'type':'boolean'}}, ['version'])


def check_environment(b, state, contract, scratch):
    """Public environment-only probe of a clean snapshot using actual check policies.

    No private acceptance program/question is copied into executor context.
    """
    if contract.get('environment_requirements') is None:
        return None
    import tempfile
    import shutil
    source = Path(state['worktree'])
    scratch = Path(scratch)
    evidence=[]
    # Keep the snapshot outside the executor-writable scratch; never let its
    # stale working/build artifacts stand in for a clean acceptance source.
    parent=b.CACHE/'environment-snapshots'
    parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    target=parent/uuid.uuid4().hex
    try:
        commit=b.git(source,'rev-parse','HEAD')
        b.materialize_commit(source,commit,target)
        env,protected=build_environment(b,contract,target,scratch,b.minimal_env(),phase='checks')
        for network in sorted({bool(c.get('network',False)) for c in contract.get('checks',[])} or {False}):
            profile=b.sandbox_profile(network,writable=[scratch],
                readable=[target,*contract.get('dependency_paths',[])],protected=[target,*protected],
                service_ports=service_ports(contract,'checks'),
                ipc_paths=[env['TMPDIR']] if contract['environment_requirements'].get('local_ipc') else [])
            evidence.append(probe(b,contract,target,scratch,env,profile,phase='checks'))
        return {'source_commit':commit,'clean_snapshot':True,'checks':evidence}
    finally:
        if target.exists(): shutil.rmtree(target)
