"""The framework components against the fake environment (tests/fakevenv.py): no network, no real platform."""
import json
import os
import socket

import pytest

import fakevenv
from icmstack.cli import Cli
from icmstack.components import default_registry
from icmstack.components.base import Context
from icmstack.components.platform_modular import Web
from icmstack.profile import Profile
from icmstack.prompts import Answers


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def run(root, *argv, replies=()):
    out, replies = [], list(replies)
    code = Cli(out=out.append, input_fn=lambda p: replies.pop(0), interactive=True).main(['--root', str(root), *argv])
    return code, '\n'.join(out)


@pytest.fixture
def stack(tmp_path):
    venv = fakevenv.make(tmp_path)
    fakevenv.add_poetry(venv)
    pw = tmp_path / 'pw'; pw.write_text('s3cret\n')
    home = tmp_path / 'home'
    web_port, ui_port = free_port(), free_port()
    common = ['--venv', str(venv), '--volttron-home', str(home), '--bind', f'https://0.0.0.0:{web_port}', '--web-password-file', str(pw),
              '--ui-port', str(ui_port), '--instance-name', 'icmtest']
    yield {'root': tmp_path, 'venv': venv, 'home': home, 'web_port': web_port, 'ui_port': ui_port, 'common': common}
    run(tmp_path, '-y', 'down')


def test_up_minimal_installs_configures_and_starts(stack):
    code, text = run(stack['root'], '-y', 'up', 'minimal', *stack['common'])
    home, venv = stack['home'], stack['venv']
    config = (home / 'config').read_text()
    assert 'instance-name = icmtest' in config and f'bind-web-address = https://0.0.0.0:{stack["web_port"]}' in config
    assert 'web-ssl-cert' in config and 'rpc-allow-list' in config and 'platform.presentation: resolve' in config
    certs = stack['root'] / 'default' / 'certs'
    assert (certs / 'web.crt').exists() and oct((certs / 'web.key').stat().st_mode & 0o777) == '0o600'
    users = json.loads((home / 'web-users.json').read_text())
    assert 'admin' in users and set(users['admin']['groups']) == {'admin', 'vui'}
    assert (home / 'pyproject.toml').exists() and '[tool.poetry]' in (home / 'pyproject.toml').read_text()
    vctl_log = (home / 'vctl.log').read_text()
    assert 'install volttron-platform-driver --vip-identity platform.driver --tag driver --start --enable' in vctl_log
    assert 'install interoperability-service --vip-identity platform.presentation --tag interop --start --enable' in vctl_log
    assert 'authz add agent platform.web --role-names default_rpc_capabilities' in vctl_log
    pip_log = (venv / 'pip.log').read_text()
    assert 'interoperability-service[discovery,openfmb]' in pip_log and 'volttron-platform-driver' in pip_log
    ui_args = (venv / 'ui_fake.py.args').read_text()
    assert f'--platform-url https://localhost:{stack["web_port"]}' in ui_args and '--platform-name icmtest' in ui_args
    assert 'fake-device: registered as devices/icm/fake/device1' in text and 'skipped' not in text
    store = home / 'fake_store' / 'platform.driver'
    assert (store / 'registry_configs__fake.csv').exists() and json.loads((store / 'devices__icm__fake__device1').read_text())['driver_type'] == 'fake'
    assert (home / 'volttron.log').read_text().count('fake platform up') == 1    # web configured before the first start: no restart
    code, status = run(stack['root'], 'status')
    for name in ('platform', 'web', 'driver', 'interop', 'ui'):
        line = next(l for l in status.splitlines() if l.strip().startswith(name + ' '))
        assert 'running' in line, line
    code, text = run(stack['root'], '-y', 'down')
    code, status = run(stack['root'], 'status')
    assert 'running' not in status


def test_stop_agent_and_restart_platform_keeps_registration(stack):
    run(stack['root'], '-y', 'start', 'driver', *stack['common'])
    code, text = run(stack['root'], '-y', 'stop', 'driver')
    assert 'driver: stopped' in text and 'stop --tag driver' in (stack['home'] / 'vctl.log').read_text()
    code, text = run(stack['root'], '-y', 'start', 'driver')
    log = (stack['home'] / 'vctl.log').read_text()
    assert log.count('install volttron-platform-driver') == 1 and 'start --tag driver' in log     # registered once, then started
    code, text = run(stack['root'], 'stop', 'platform', replies=['n'])                             # dependents running: asks, no
    assert 'depend on platform' in text or 'platform: not running' not in text


def test_agent_sources(stack, monkeypatch):
    from icmstack.components.agents import Agent, SPECS
    interop = next(Agent(s) for s in SPECS if s.name == 'interop')
    profile = Profile('default', root=stack['root'])
    ctx = Context(profile, Answers(profile, yes=True, interactive=True), out=lambda s: None)
    assert interop.resolve_source(ctx) == ('interoperability-service[discovery,openfmb]', 'interoperability-service')
    checkout = stack['root'] / 'modules' / 'interoperability_service'; checkout.mkdir(parents=True)
    monkeypatch.setenv('ICM_MODULES', str(stack['root'] / 'modules'))
    ctx = Context(profile, Answers(profile, cli={'agent_source': 'checkout'}, yes=True, interactive=True), out=lambda s: None)
    assert interop.resolve_source(ctx) == (f'-e:{checkout}', str(checkout))
    ctx = Context(profile, Answers(profile, cli={'source_interop': 'interoperability-service==0.3'}, yes=True, interactive=True), out=lambda s: None)
    assert interop.resolve_source(ctx) == ('interoperability-service==0.3', 'interoperability-service')


def test_web_url_from_bind(tmp_path):
    profile = Profile('default', root=tmp_path)
    ctx = Context(profile, Answers(profile, yes=True, interactive=True), out=lambda s: None)
    profile.answers['bind'] = 'https://0.0.0.0:8443'
    assert Web().url(ctx) == 'https://localhost:8443'
    profile.answers['bind'] = 'http://10.0.0.5:8080'
    assert Web().url(ctx) == 'http://10.0.0.5:8080'


def test_dependents_are_skipped_when_a_need_fails(stack, monkeypatch):
    """Without poetry the platform refuses to start and everything that needs it is skipped rather than attempted."""
    (stack['venv'] / 'bin' / 'poetry').unlink()
    monkeypatch.setenv('PATH', '/usr/bin:/bin')          # no poetry anywhere on the path
    code, text = run(stack['root'], '-y', 'up', 'minimal', *stack['common'])
    assert 'poetry does not run' in text
    for name in ('web', 'driver', 'interop', 'ui', 'fake-device'):
        assert f'{name}: skipped' in text


def test_poetry_project_has_no_duplicate_keys(stack):
    from icmstack.venv import PlatformEnv
    text = PlatformEnv(stack['venv']).poetry_project()
    keys = [line.split(' = ')[0] for line in text.splitlines() if line.startswith('"')]
    assert keys and len(keys) == len(set(keys))


def test_register_sunspec_device_writes_registry_device_and_resource(stack):
    pytest.importorskip('umodbus')
    from icmstack.processes import free_port
    port = free_port()
    run(stack['root'], '-y', 'start', 'platform', *stack['common'])
    code, text = run(stack['root'], '-y', 'register', 'sunspec-device', '--sunspec-port', str(port), '--sunspec-topic', 'icm/sunspec/pv1')
    assert code == 0 and 'registered as devices/icm/sunspec/pv1' in text, text
    store = stack['home'] / 'fake_store'
    assert (store / 'platform.driver' / 'registry_configs__pv1.csv').read_text().startswith('volttron_point_name')
    device = json.loads((store / 'platform.driver' / 'devices__icm__sunspec__pv1').read_text())
    assert device['remote_config'] == {'driver_type': 'modbus', 'transport_protocol': 'tcp', 'device_address': '127.0.0.1', 'port': port, 'unit_id': 1, 'timeout': 5}
    service = json.loads((store / 'platform.presentation' / 'config').read_text())
    assert 'fake_sunspec_pv' in service['formats'] and len(service['transforms']) == 2
    mapping = service['mappings'][0]
    assert mapping['uai'] == ['icm', 'sunspec', 'pv1'] and mapping['resource']['publication_topic'] == 'devices/icm/sunspec/pv1/all'
    code, text = run(stack['root'], '-y', 'register', 'sunspec-device')            # again: replaced, not duplicated
    service = json.loads((store / 'platform.presentation' / 'config').read_text())
    assert len(service['mappings']) == 1 and len(service['transforms']) == 2


def test_every_showcase_wires_and_describes(stack, monkeypatch):
    from icmstack.wiring import SHOWCASES, describe, wire
    run(stack['root'], '-y', 'start', 'platform', *stack['common'])
    from icmstack.devices import sep2 as sep2mod
    certs = stack['root'] / 'default' / 'sep2' / 'tls'; (certs / 'notify').mkdir(parents=True)
    for name in sep2mod.certificate_paths(certs).values():
        name.write_text('x')
    monkeypatch.setattr(sep2mod, 'generate_certs', lambda d: sep2mod.certificate_paths(d))
    monkeypatch.setattr(sep2mod, 'images_present', lambda: {sep2mod.SERVER_IMAGE: True, sep2mod.CLIENT_IMAGE: True})
    monkeypatch.setattr('icmstack.components.devices.containers.docker_available', lambda: True)
    monkeypatch.setattr(sep2mod, 'lfdi_from_cert', lambda p: 'AB' * 20)
    for device in ('sunspec-device', 'dnp3-master', 'sep2-server', 'sep2-client'):
        code, text = run(stack['root'], '-y', 'register', device)
        assert code == 0, text
    run(stack['root'], 'config', '--set', 'broker=docker', '--set', 'broker_port=18840')
    profile = Profile('default', root=stack['root'])
    ctx = Context(profile, Answers(profile, yes=True, interactive=True), out=lambda s: None)
    wire(ctx, list(SHOWCASES))
    store = stack['home'] / 'fake_store'
    bridges = {b['name']: b for b in json.loads((store / 'platform.resource_bridge' / 'config').read_text())['bridges']}
    assert set(bridges) == {'sunspec>openfmb.reading', 'openfmb.control>sunspec', 'sunspec>dnp3.served', 'dnp3.served>sunspec', 'sunspec>sep2', 'sep2>sunspec', 'sep2.served>openfmb.reading'}
    assert bridges['openfmb.control>sunspec']['source'] == {'uai': ['openfmb', 'solarmodule', 'SolarControlProfile', '7d1a2b3c-0000-4000-8000-000000000011'], 'bus': True}
    assert 'points' not in bridges['openfmb.control>sunspec'] and 'points' not in bridges['sunspec>dnp3.served']
    assert bridges['dnp3.served>sunspec']['points'] == ['AO_87', 'BO_17'] and bridges['sep2>sunspec']['points'] == ['DERControl_opModMaxLimW']
    assert bridges['sunspec>dnp3.served']['target'] == ['icm', 'dnp3', 'served'] and bridges['sep2>sunspec']['source'] == ['icm', 'sep2', 'der']
    service = json.loads((store / 'platform.presentation' / 'config').read_text())
    aliases = {'/'.join(m['uai']): m for m in service['mappings'] if m['resource_type'] == 'alias'}
    assert len(aliases) == 3 and aliases['openfmb/essmodule/ESSReadingProfile/5e6f7a80-0000-4000-8000-000000000013']['resource']['references'] == ['icm', 'sep2', 'served']
    site = json.loads((store / 'platform.bus_adapter' / 'config').read_text())['adapters'][0]
    assert len(site['local_subscriptions']) == 2 and site['remote_subscriptions'] == ['openfmb/solarmodule/SolarControlProfile/7d1a2b3c-0000-4000-8000-000000000011']
    wire(ctx, list(SHOWCASES))                                                         # idempotent
    assert len(json.loads((store / 'platform.resource_bridge' / 'config').read_text())['bridges']) == 7
    for name in SHOWCASES:
        text = describe(ctx, name)
        assert 'Observe:' in text and ('"+ Alias"' in text or '"+ Device bridge"' in text)
    code, text = run(stack['root'], 'wire', '--list')
    assert code == 0 and text.count('\n') + 1 == len(SHOWCASES)
    code, text = run(stack['root'], 'wire', '--print', 'openfmb-to-sunspec')
    assert 'remote topic on a bus' in text and 'inbound' in text


def test_demo_wiring_writes_alias_bridge_and_bus_adapter(stack):
    pytest.importorskip('umodbus')
    from icmstack.processes import free_port
    from icmstack.wiring import wire_demo
    run(stack['root'], '-y', 'start', 'platform', *stack['common'])
    run(stack['root'], '-y', 'register', 'sunspec-device', '--sunspec-port', str(free_port()), '--sunspec-topic', 'icm/sunspec/pv1')
    run(stack['root'], 'config', '--set', 'broker=docker', '--set', 'broker_port=18840')
    profile = Profile('default', root=stack['root'])
    out = []
    wire_demo(Context(profile, Answers(profile, yes=True, interactive=True), out=out.append))
    store = stack['home'] / 'fake_store'
    service = json.loads((store / 'platform.presentation' / 'config').read_text())
    aliases = [m for m in service['mappings'] if m['resource_type'] == 'alias']
    assert len(aliases) == 2 and aliases[0]['resource']['references'] == ['icm', 'sunspec', 'pv1'] and aliases[0]['resource']['encoding'] == 'protobuf'
    bridges = {b['name']: b for b in json.loads((store / 'platform.resource_bridge' / 'config').read_text())['bridges']}
    assert set(bridges) == {'sunspec>openfmb.reading', 'openfmb.control>sunspec'}
    assert bridges['sunspec>openfmb.reading'] == {'name': 'sunspec>openfmb.reading', 'source': ['icm', 'sunspec', 'pv1'],
                                                  'target': {'uai': ['openfmb', 'solarmodule', 'SolarReadingProfile', '7d1a2b3c-0000-4000-8000-000000000011'], 'bus': True}}
    bus = json.loads((store / 'platform.bus_adapter' / 'config').read_text())
    assert bus['bus_type'] == 'mqtt' and bus['adapters'][0]['port'] == 18840 and bus['adapters'][0]['local_subscriptions'] == ['openfmb/solarmodule/SolarReadingProfile/7d1a2b3c-0000-4000-8000-000000000011']
    wire_demo(Context(profile, Answers(profile, yes=True, interactive=True), out=out.append))       # idempotent
    assert len(json.loads((store / 'platform.resource_bridge' / 'config').read_text())['bridges']) == 2


def test_register_container_devices_in_both_roles(stack, monkeypatch):
    """Registration needs no docker: registries, device entries in client and served roles, formats and transforms."""
    run(stack['root'], '-y', 'start', 'platform', *stack['common'])
    certs = stack['root'] / 'default' / 'sep2' / 'tls'; (certs / 'notify').mkdir(parents=True)
    from icmstack.devices import sep2 as sep2mod
    monkeypatch.setattr(sep2mod, 'generate_certs', lambda d: sep2mod.certificate_paths(d))
    monkeypatch.setattr(sep2mod, 'images_present', lambda: {sep2mod.SERVER_IMAGE: True, sep2mod.CLIENT_IMAGE: True})
    monkeypatch.setattr('icmstack.components.devices.containers.docker_available', lambda: True)
    monkeypatch.setattr(sep2mod, 'lfdi_from_cert', lambda p: 'AB' * 20)
    for name in sep2mod.certificate_paths(certs).values():
        name.write_text('x')
    code, text = run(stack['root'], '-y', 'register', 'dnp3-outstation', '--dnp3-port', '20010')
    assert code == 0 and 'format fake_dnp3_der' in text, text
    code, text = run(stack['root'], '-y', 'register', 'dnp3-master', '--dnp3-served-port', '20011')
    assert code == 0 and 'format fake_dnp3_der_served' in text, text
    code, text = run(stack['root'], '-y', 'register', 'sep2-server', '--sep2-port', '9450')
    assert code == 0 and 'format fake_sunspec_pv_sep2' in text, text
    code, text = run(stack['root'], '-y', 'register', 'sep2-client', '--sep2-served-port', '9451')
    assert code == 0 and 'format fake_sunspec_pv_sep2_served' in text, text
    store = stack['home'] / 'fake_store'
    client = json.loads((store / 'platform.driver' / 'devices__icm__dnp3__der').read_text())['remote_config']
    assert client['outstation_ip'] == '127.0.0.1' and client['port'] == 20010 and client['master_id'] == 2
    served = json.loads((store / 'platform.driver' / 'devices__icm__dnp3__served').read_text())['remote_config']
    assert served['driver_role'] == 'outstation' and served['port'] == 20011
    sep2_served = json.loads((store / 'platform.driver' / 'devices__icm__sep2__served').read_text())['remote_config']
    assert sep2_served['driver_role'] == 'server' and sep2_served['client_lfdi'] == 'AB' * 20 and sep2_served['client_pin'] == 111115
    service = json.loads((store / 'platform.presentation' / 'config').read_text())
    assert set(service['formats']) == {'fake_dnp3_der', 'fake_dnp3_der_served', 'fake_sunspec_pv_sep2', 'fake_sunspec_pv_sep2_served'}
    assert {'/'.join(m['uai']) for m in service['mappings']} == {'icm/dnp3/der', 'icm/dnp3/served', 'icm/sep2/der', 'icm/sep2/served'}
    assert all(any(f in (t['input_format'], t['output_format']) for f in service['formats']) for t in service['transforms'])


def test_reset_config_wipes_the_stores_after_confirmation(stack):
    run(stack['root'], '-y', 'start', 'platform', *stack['common'])
    run(stack['root'], '-y', 'register', 'fake-device')
    store = stack['home'] / 'fake_store'
    assert (store / 'platform.driver').is_dir()
    code, text = run(stack['root'], 'reset-config', replies=['n'])
    assert 'nothing deleted' in text and (store / 'platform.driver').is_dir()
    code, text = run(stack['root'], 'reset-config', replies=['y'])
    assert 'platform.driver: store wiped' in text and not (store / 'platform.driver').exists()


def test_reset_config_with_platform_stopped_deletes_store_files(stack):
    store = stack['home'] / 'configuration_store'; store.mkdir(parents=True)
    for name in ('platform.driver', 'platform.presentation'):
        (store / f'{name}.store').write_text('{}')
    code, text = run(stack['root'], 'reset-config', '--volttron-home', str(stack['home']), replies=['n'])
    assert 'nothing deleted' in text and len(list(store.glob('*.store'))) == 2
    code, text = run(stack['root'], '-y', 'config', '--set', f"volttron_home={stack['home']}")
    code, text = run(stack['root'], 'reset-config', '--identities', 'platform.driver', replies=['y'])
    assert 'platform.driver: store file deleted' in text and [f.name for f in store.glob('*.store')] == ['platform.presentation.store']


def test_extra_packages_come_from_checkouts_when_agents_do(stack, monkeypatch):
    from icmstack.components.agents import Agent, SPECS
    tree = stack['root'] / 'modular'
    for sub in ('drivers/base-driver', 'drivers/interfaces/fake', 'drivers/platform-driver-agent', 'proxies/protocol-proxy', 'proxies/mqtt'):
        (tree / sub).mkdir(parents=True)
    monkeypatch.setenv('VOLTTRON_MODULAR_DRIVERS', str(tree / 'drivers'))
    monkeypatch.delenv('VOLTTRON_MODULAR_PROXIES', raising=False)
    monkeypatch.setattr('icmstack.venv.PlatformEnv.has', lambda self, d: True)   # released copies present: the checkouts must still win
    profile = Profile('default', root=stack['root']); profile.answers.update({'venv': str(stack['venv']), 'agent_source': 'checkout'}); profile.save()
    ctx = Context(profile, Answers(profile, yes=True, interactive=True), out=lambda s: None)
    next(Agent(s) for s in SPECS if s.name == 'driver').install(ctx)
    log = (stack['venv'] / 'pip.log').read_text()
    assert f'-e {tree}/drivers/platform-driver-agent' in log and f'-e {tree}/drivers/base-driver' in log and f'-e {tree}/drivers/interfaces/fake' in log


def test_shared_libraries_install_from_the_icm_utils_checkout_before_the_agent(stack, monkeypatch):
    """The agents depend on icm-lib-transforms and icm-lib-resources; from checkouts those come from icm-utils, with their
    extras, and ahead of the agent so pip never has to resolve them from PyPI."""
    from icmstack.components.agents import Agent, SPECS
    modules = stack['root'] / 'modules'
    for sub in ('resource-bridge', 'icm-utils/icm-lib-transforms', 'icm-utils/icm-lib-resources'):
        (modules / sub).mkdir(parents=True)
    monkeypatch.setenv('ICM_MODULES', str(modules))
    monkeypatch.setattr('icmstack.venv.PlatformEnv.has', lambda self, d: False)
    profile = Profile('default', root=stack['root']); profile.answers.update({'venv': str(stack['venv']), 'agent_source': 'checkout'}); profile.save()
    ctx = Context(profile, Answers(profile, yes=True, interactive=True), out=lambda s: None)
    next(Agent(s) for s in SPECS if s.name == 'resource-bridge').install(ctx)
    log = (stack['venv'] / 'pip.log').read_text()
    transform, resource, agent = (log.index(f'-e {modules}/icm-utils/icm-lib-transforms'),
                                  log.index(f'-e {modules}/icm-utils/icm-lib-resources[volttron,openfmb]'),
                                  log.index(f'-e {modules}/resource-bridge'))
    assert transform < agent and resource < agent


def test_stray_platform_on_the_home_is_found_stopped_and_reported(stack):
    """A platform process nobody tracks (an orphan of a deleted profile) blocks the home; the tool finds it by its
    VOLTTRON_HOME, offers to stop it, and down stops it even with an empty state."""
    import os, subprocess, sys, time
    from icmstack.processes import pid_alive
    home = stack['home']; home.mkdir(parents=True, exist_ok=True)
    stray = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)', 'volttron', '-l', str(home / 'volttron.log')],
                             env={**os.environ, 'VOLTTRON_HOME': str(home)}, start_new_session=True)
    try:
        time.sleep(0.3)
        run(stack['root'], '-y', 'config', '--set', f'volttron_home={home}', '--set', f'venv={stack["venv"]}')
        code, text = run(stack['root'], 'status')
        assert f'untracked platform process on this home: pid {stray.pid}' in text
        code, text = run(stack['root'], 'start', 'platform', *stack['common'], replies=['', 'n'])     # backend default, then refuse the stop
        assert code == 1 and 'another platform uses' in text and pid_alive(stray.pid)
        code, text = run(stack['root'], '-y', 'down')
        time.sleep(0.5)
        assert f'stopping stray pid {stray.pid}' in text and not pid_alive(stray.pid)
    finally:
        if stray.poll() is None:
            stray.kill()


def test_bind_failure_fails_fast(stack):
    """A platform whose router died on 'Address already in use' lingers; the tool stops waiting on that message."""
    (stack['venv'] / 'volttron_fake.py').write_text("import os, time\nprint('zmq.error.ZMQError: Address already in use (addr=ipc://x)', flush=True)\nwhile True: time.sleep(1)\n")
    code, text = run(stack['root'], '-y', 'start', 'platform', *stack['common'])
    assert code == 1 and 'could not bind its socket' in text
    code, status = run(stack['root'], 'status')
    assert 'platform' in status and 'running' not in status.split('web')[0]


def test_install_reinstalls_an_installed_component(stack):
    run(stack['root'], '-y', 'start', 'platform', *stack['common'])
    run(stack['root'], '-y', 'install', 'driver', *stack['common'])
    before = (stack['venv'] / 'pip.log').read_text().count('platform-driver')
    run(stack['root'], '-y', 'install', 'driver')
    assert (stack['venv'] / 'pip.log').read_text().count('platform-driver') == before + 1       # install always installs
    run(stack['root'], '-y', 'start', 'driver')
    assert (stack['venv'] / 'pip.log').read_text().count('platform-driver') == before + 1       # start does not
