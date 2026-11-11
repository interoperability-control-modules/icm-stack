import pytest

from icmstack.cli import Cli
from icmstack.components import default_registry
from icmstack.presets import PRESETS


def test_dependency_order_and_dependents():
    r = default_registry()
    order = r.ordered(['ui'])
    assert order.index('platform') < order.index('web') < order.index('ui')
    assert r.missing_needs(['ui']) == ['platform', 'web']
    assert 'web' in r.dependents('platform') and 'ui' in r.dependents('web')
    demo = r.ordered(PRESETS['demo']['components'])
    assert demo.index('broker') < demo.index('bus-adapter') and demo[0] == 'platform'
    for preset in PRESETS.values():
        for name in preset['components'] + preset.get('optional', []):
            assert name in r


def run(tmp_path, *argv, replies=(), interactive=True):
    out = []
    replies = list(replies)
    cli = Cli(out=out.append, input_fn=lambda prompt: replies.pop(0), interactive=interactive)
    code = cli.main(['--root', str(tmp_path), *argv])
    return code, '\n'.join(out)


def test_status_lists_every_component(tmp_path):
    code, text = run(tmp_path, 'status')
    assert code == 0 and 'framework:' in text and 'device:' in text
    for name in ('platform', 'ui', 'broker', 'sunspec-device', 'dnp3-outstation'):
        assert name in text
    assert text.count('not installed') >= 10


def test_config_set_and_show(tmp_path):
    code, text = run(tmp_path, 'config', '--set', 'instance_name=demo1')
    assert code == 0 and 'instance_name = demo1' in text
    code, text = run(tmp_path, 'config')
    assert 'instance_name = demo1' in text
    code, text = run(tmp_path, 'config', '--unset', 'instance_name')
    assert 'no recorded answers' in text


def test_start_asks_about_needs_and_respects_no(tmp_path, monkeypatch):
    monkeypatch.setenv('PATH', '/nonexistent')                              # no docker: the device itself refuses
    code, text = run(tmp_path, 'start', 'sep2-client', replies=['n', '', '', '', ''])      # needs declined; its four questions take the defaults
    lines = text.splitlines()
    assert not any(l.startswith(('platform:', 'driver:')) for l in lines) and 'sep2-client: docker is not available' in text


def test_sep2_identities_and_fixture(tmp_path):
    pytest.importorskip('cryptography')
    import datetime
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    from icmstack.devices import sep2
    key = ec.generate_private_key(ec.SECP256R1()); name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'dev')])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(1).not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1)).sign(key, hashes.SHA256())
    path = tmp_path / 'dev.crt'; path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    lfdi = sep2.lfdi_from_cert(path)
    assert len(lfdi) == 40 and lfdi == lfdi.upper() and int(lfdi, 16)
    sfdi = sep2.sfdi_from_lfdi(lfdi)
    assert len(str(sfdi)) in (11, 12) and sum(int(d) for d in f'{sfdi:012d}') % 10 == 0
    fixture = tmp_path / 'fix' / 'boot.yaml'; sep2.write_fixture(fixture, lfdi, sfdi)
    assert f'lfdi: "{lfdi}"' in fixture.read_text() and 'op_mod_max_lim_w: 10000' in fixture.read_text()


def test_dnp3_stimulus_widens_ranges():
    from icmstack.devices import dnp3_tool
    profile = {'AI': {'points': [{'point_index': 537, 'minimum': 0, 'maximum': 100}, {'point_index': 536, 'minimum': 59.0, 'maximum': 61.0}]}}
    out = dnp3_tool.with_stimulus(profile, {537: 4200.0, 536: 60.5})
    assert dnp3_tool.get_point(out, 'AI', 537) == {'point_index': 537, 'minimum': 0, 'maximum': 4201, 'value': 4200}
    assert dnp3_tool.get_point(out, 'AI', 536)['value'] == 60.5 and dnp3_tool.get_point(profile, 'AI', 537).get('value') is None


def test_doctor_runs(tmp_path):
    code, text = run(tmp_path, 'doctor', '--no-ports')
    assert 'Python' in text and 'poetry' in text and 'ok,' in text


def test_shell_prints_environment(tmp_path):
    code, text = run(tmp_path, 'shell')
    assert code == 0 and 'export VOLTTRON_HOME=' in text and 'volttron_home' in text


def test_menu_quits(tmp_path):
    code, text = run(tmp_path, replies=[''])
    assert code == 0 and 'What now?' not in text and 'framework:' in text


def test_broker_option_is_a_question_option(tmp_path):
    code, text = run(tmp_path, '-y', 'config')
    cli = Cli(out=lambda s: None)
    parser = cli.parser()
    ns = parser.parse_args(['start', 'broker', '--broker', '127.0.0.1:1883', '--broker-port', '1884'])
    assert ns.broker == '127.0.0.1:1883' and ns.broker_port == '1884'


def test_sunspec_device_runs_and_answers_modbus(tmp_path):
    pytest.importorskip('umodbus')
    from icmstack.processes import free_port
    port = free_port()
    code, text = run(tmp_path, '-y', 'start', 'sunspec-device', '--sunspec-port', str(port))
    try:
        assert code == 0 and f'127.0.0.1:{port}' in text, text
        from umodbus.client import tcp
        import socket
        with socket.create_connection(('127.0.0.1', port), timeout=3) as sock:
            words = tcp.send_message(tcp.read_holding_registers(slave_id=1, starting_address=40004, quantity=4), sock)
        assert bytes(sum(([w >> 8, w & 0xFF] for w in words), [])).startswith(b'SunSpec')      # 1_Mn default: SunSpecTest
        code, text = run(tmp_path, 'register', 'sunspec-device', '--print')
        assert 'Modbus TCP' in text and str(port) in text
    finally:
        run(tmp_path, '-y', 'stop', 'sunspec-device')


def test_openfmb_reading_payload_round_trips_through_the_codecs():
    pytest.importorskip('icm.resources.codecs.openfmb')
    from icm.resources.codecs.openfmb import decode, encode
    from icmstack.devices.openfmb_party import DEFAULT_MRID, READING_PROTO, reading_payload
    for family in ('solar', 'ess'):
        payload = reading_payload(family, DEFAULT_MRID, w=4200.5, var=310.0, hz=60.01, phase_v=241.2)
        decoded = decode(READING_PROTO[family], encode(READING_PROTO[family], payload))
        key = 'solarReading' if family == 'solar' else 'essReading'
        assert decoded[key]['readingMMXU']['W']['net']['cVal']['mag'] == pytest.approx(4200.5)
        assert decoded['readingMessageInfo']['messageInfo']['identifiedObject']['mRID'] == DEFAULT_MRID


def test_modbus_sim_runs_and_answers(tmp_path):
    pytest.importorskip('pymodbus')
    from icmstack.processes import free_port
    port = free_port()
    code, text = run(tmp_path, '-y', 'start', 'modbus-sim', '--modbus-sim-port', str(port))
    try:
        assert code == 0 and 'modbus simulator:' in text, text
        from pymodbus.client import ModbusTcpClient
        from pymodbus.client.mixin import ModbusClientMixin as M
        c = ModbusTcpClient('127.0.0.1', port=port); assert c.connect()
        assert M.convert_from_registers(c.read_input_registers(1001, count=2).registers, M.DATATYPE.FLOAT32) == pytest.approx(412.5)
        c.close()
    finally:
        run(tmp_path, '-y', 'stop', 'modbus-sim')


def test_unexpected_errors_are_one_line(tmp_path, monkeypatch):
    import icmstack.cli as cli
    monkeypatch.setattr(cli.Cli, 'do_status', lambda self, ctx, args=None: 1 / 0)
    code, text = run(tmp_path, 'status')
    assert code == 3 and 'ZeroDivisionError' in text and '--debug' in text and 'Traceback' not in text
    with pytest.raises(ZeroDivisionError):
        run(tmp_path, '--debug', 'status')


def test_missing_cryptography_is_a_clear_message(tmp_path, monkeypatch):
    import builtins
    real_import = builtins.__import__
    def fake_import(name, *a, **k):
        if name.startswith('cryptography'):
            raise ImportError(name)
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, '__import__', fake_import)
    from icmstack.devices import sep2
    with pytest.raises(RuntimeError, match='cryptography package is missing'):
        sep2.lfdi_from_cert(tmp_path / 'x.crt')
