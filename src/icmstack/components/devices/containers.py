"""The container devices: the IEEE 1815.2 reference outstation and control station, the GridAPPS-D Go 2030.5 server
and DER client. The outstation and the Go server are devices the platform's drivers talk to (client role); the
control station and the Go client talk to devices the platform serves (served role), so those two need a served device
registered first and `register` writes that served device."""
from __future__ import annotations

import json
from pathlib import Path

from ...devices import dnp3_tool, sep2
from ...processes import docker_available, port_open, wait_for
from ...prompts import Question
from ..base import Component, Context, NotReady, Status

DATA = Path(__file__).resolve().parents[2] / 'data'
DRIVER, SERVICE = 'platform.driver', 'platform.presentation'


def _declared(name: str) -> dict:
    return json.loads((DATA / 'service' / f'{name}.presentation.json').read_text())


def _register_device(ctx: Context, topic: str, registry_file: str, registry_name: str, driver_type: str, remote: dict, declaration: str, served: bool) -> str:
    """Registry, device entry and canonical resource (format and transforms from the bundled declaration)."""
    from ...platform_config import canonical_mapping, device_entry, merge_service_config, store_csv, store_json
    spec = _declared(declaration)
    fmt = spec['served_format'] if served else spec['client_format']
    store_csv(ctx, DRIVER, f'registry_configs/{registry_name}', DATA / 'registries' / registry_file)
    store_json(ctx, DRIVER, f'devices/{topic}', device_entry(driver_type, registry_name, remote))
    transforms = [t for t in spec['transforms'] if fmt in (t.get('input_format'), t.get('output_format'))]
    merge_service_config(ctx, SERVICE, {fmt: spec['formats'][fmt]}, transforms, [canonical_mapping(topic, fmt)])
    return fmt


def _platform_up(ctx: Context) -> None:
    from ..platform_modular import Platform
    if not Platform().process(ctx).running():
        raise NotReady('the platform is not running')


def _device_registered(ctx: Context, topic: str) -> bool:
    from ...platform_config import list_entries
    return f'devices/{topic}' in list_entries(ctx, DRIVER)


class ContainerDevice(Component):
    group = 'device'
    step = 4

    def container_name(self, ctx: Context) -> str:
        return f'icmstack-{ctx.profile.name}-{self.name}'

    def check_docker(self) -> None:
        if not docker_available():
            raise NotReady(f'{self.name}: docker is not available')


# ---------------------------------------------------------------- DNP3
class Dnp3Base(ContainerDevice):
    def check_tool(self) -> None:
        self.check_docker()
        ok, why = dnp3_tool.available()
        if not ok:
            raise NotReady(f'{self.name}: {why}')

    def container(self, ctx: Context, profile: dict) -> dnp3_tool.Container:
        return dnp3_tool.Container(self.container_name(ctx), profile, ctx.profile.dir / 'dnp3')

    def running(self, ctx: Context) -> bool:
        return sep2.container_running(self.container_name(ctx))

    def stop(self, ctx: Context) -> None:
        if sep2.container_stop(self.container_name(ctx)):
            ctx.out(f'{self.name}: stopped')

    def logs(self, ctx: Context, lines: int = 40) -> str:
        return dnp3_tool.ANSI.sub('', sep2.container_logs(self.container_name(ctx), lines))


class Dnp3Outstation(Dnp3Base):
    name = 'dnp3-outstation'
    description = 'IEEE 1815.2 reference outstation (docker): the DNP3 device the platform polls'
    Q_PORT = Question('dnp3_port', 'DNP3 outstation port', default=20000, kind='int')
    Q_OUT = Question('dnp3_outstation_address', 'DNP3 outstation address', default=1, kind='int')
    Q_MASTER = Question('dnp3_master_address', 'DNP3 master address', default=2, kind='int')
    Q_W = Question('dnp3_w', 'Active power the outstation reports (AI 537), empty for the profile\'s zeros', default=4200.0, kind='float')
    Q_HZ = Question('dnp3_hz', 'Frequency the outstation reports (AI 536)', default=60.0, kind='float')
    Q_TOPIC = Question('dnp3_topic', 'Topic of the DNP3 outstation in the platform (devices/<topic>, used by register)', default='icm/dnp3/der')

    def questions(self):
        return [self.Q_PORT, self.Q_OUT, self.Q_MASTER, self.Q_W, self.Q_HZ, self.Q_TOPIC]

    def install(self, ctx: Context) -> None:
        self.check_tool()
        for q in (self.Q_PORT, self.Q_OUT, self.Q_MASTER, self.Q_W, self.Q_HZ):
            ctx.answers.ask(q)

    def start(self, ctx: Context) -> None:
        if self.running(ctx):
            ctx.out(f'{self.name}: already running'); return
        self.check_tool()
        a = ctx.profile.answers
        profile = dnp3_tool.with_stimulus(dnp3_tool.load_profile(), {537: float(a['dnp3_w']), 536: float(a['dnp3_hz'])} if a.get('dnp3_w') is not None else {})
        dnp3_tool.start_outstation(self.container(ctx, profile), int(a['dnp3_port']), int(a['dnp3_outstation_address']), int(a['dnp3_master_address']))
        ctx.out(f'{self.name}: 127.0.0.1:{a["dnp3_port"]}, outstation {a["dnp3_outstation_address"]}, master {a["dnp3_master_address"]}')

    def status(self, ctx: Context) -> Status:
        installed = ctx.profile.is_installed(self.name)
        return Status(installed, self.running(ctx) if installed else False, f'127.0.0.1:{ctx.profile.answers.get("dnp3_port")}' if installed else '')

    def connection_details(self, ctx: Context) -> str:
        a = ctx.profile.answers
        return (f'dnp3-outstation: DNP3 TCP 127.0.0.1:{a.get("dnp3_port", 20000)}, outstation address {a.get("dnp3_outstation_address", 1)}, '
                f'master address {a.get("dnp3_master_address", 2)}; registry {DATA / "registries" / "dnp3_der.dnp3.csv"} (1815.2 DER profile, AO variation 1); '
                f'suggested topic {a.get("dnp3_topic", self.Q_TOPIC.default)}')

    def register(self, ctx: Context) -> None:
        _platform_up(ctx)
        for q in self.questions():
            ctx.answers.ask(q)
        a = ctx.profile.answers
        topic = a['dnp3_topic'].strip('/')
        fmt = _register_device(ctx, topic, 'dnp3_der.dnp3.csv', 'dnp3_der.csv', 'dnp3', {
            'outstation_ip': '127.0.0.1', 'port': int(a['dnp3_port']), 'response_timeout': 5, 'integrity_poll_interval': 0,
            'master_id': int(a['dnp3_master_address']), 'outstation_id': int(a['dnp3_outstation_address'])}, 'dnp3_der', served=False)
        ctx.out(f'dnp3-outstation: registered as devices/{topic} (registry dnp3_der.csv, resource {topic} in format {fmt})')


class Dnp3Master(Dnp3Base):
    name = 'dnp3-master'
    after_register = True
    description = 'IEEE 1815.2 reference control station (docker): polls the outstation the platform serves'
    needs = ('driver',)
    Q_PORT = Question('dnp3_served_port', 'Port of the outstation the platform serves', default=20001, kind='int')
    Q_OUT = Question('dnp3_served_outstation_address', 'Served outstation address', default=1, kind='int')
    Q_MASTER = Question('dnp3_served_master_address', 'Served master address', default=2, kind='int')
    Q_TOPIC = Question('dnp3_served_topic', 'Topic of the outstation the platform serves for the control station (devices/<topic>)', default='icm/dnp3/served')
    Q_LIMIT = Question('dnp3_master_limit', 'Active power limit (%) the control station keeps writing to AO 87, empty: it only reads', default='')

    def questions(self):
        return [self.Q_PORT, self.Q_OUT, self.Q_MASTER, self.Q_TOPIC, self.Q_LIMIT]

    def install(self, ctx: Context) -> None:
        self.check_tool()
        for q in self.questions():
            ctx.answers.ask(q)

    def start(self, ctx: Context) -> None:
        if self.running(ctx):
            ctx.out(f'{self.name}: already running'); return
        self.check_tool()
        a = ctx.profile.answers
        topic = a['dnp3_served_topic'].strip('/')
        if not _device_registered(ctx, topic):
            raise NotReady(f'dnp3-master: no served outstation at devices/{topic}; run icmstack register dnp3-master first')
        if not wait_for(lambda: port_open('127.0.0.1', int(a['dnp3_served_port'])), 30):
            raise NotReady(f'dnp3-master: nothing listens on 127.0.0.1:{a["dnp3_served_port"]}; is the driver\'s served outstation up?')
        profile = dnp3_tool.load_profile()
        limit = a.get('dnp3_master_limit')
        if limit not in (None, ''):
            # Reads only the points the limit needs, and writes AO 87 (the active power limit) to the given value.
            profile = dnp3_tool.with_stimulus(dnp3_tool.trimmed(profile, ai=(148, 69), ao=(87,), bi=(69,), bo=(17,)), ao_targets={87: float(limit)})
        dnp3_tool.start_control_station(self.container(ctx, profile), int(a['dnp3_served_port']),
                                        int(a['dnp3_served_outstation_address']), int(a['dnp3_served_master_address']))
        ctx.out(f'{self.name}: polling 127.0.0.1:{a["dnp3_served_port"]}' + (f', writing AO 87 = {limit}' if limit not in (None, '') else ''))

    def status(self, ctx: Context) -> Status:
        installed = ctx.profile.is_installed(self.name)
        return Status(installed, self.running(ctx) if installed else False, f'-> 127.0.0.1:{ctx.profile.answers.get("dnp3_served_port")}' if installed else '')

    def connection_details(self, ctx: Context) -> str:
        a = ctx.profile.answers
        return (f'dnp3-master: polls a served outstation on 127.0.0.1:{a.get("dnp3_served_port", 20001)} (outstation {a.get("dnp3_served_outstation_address", 1)}, '
                f'master {a.get("dnp3_served_master_address", 2)}); served registry {DATA / "registries" / "dnp3_der_served.dnp3.csv"}; topic {a.get("dnp3_served_topic", self.Q_TOPIC.default)}')

    def register(self, ctx: Context) -> None:
        """The served device: the platform's driver as a DNP3 outstation on the port the control station polls."""
        _platform_up(ctx)
        for q in self.questions():
            ctx.answers.ask(q)
        a = ctx.profile.answers
        topic = a['dnp3_served_topic'].strip('/')
        fmt = _register_device(ctx, topic, 'dnp3_der_served.dnp3.csv', 'dnp3_der_served.csv', 'dnp3', {
            'driver_role': 'outstation', 'bind_host': '127.0.0.1', 'port': int(a['dnp3_served_port']),
            'master_id': int(a['dnp3_served_master_address']), 'outstation_id': int(a['dnp3_served_outstation_address'])}, 'dnp3_der', served=True)
        ctx.out(f'dnp3-master: served outstation registered as devices/{topic} (format {fmt}); the driver binds 127.0.0.1:{a["dnp3_served_port"]}')


# ---------------------------------------------------------------- IEEE 2030.5
class Sep2Base(ContainerDevice):
    def check_docker(self) -> None:
        super().check_docker()
        try:
            import cryptography  # noqa: F401
        except ImportError:
            raise NotReady(f'{self.name}: the cryptography package is missing in icmstack\'s environment (it reads the 2030.5 certificates): '
                           'pip install cryptography, or pip install "icmstack[devices]"')

    def cert_dir(self, ctx: Context) -> Path:
        return ctx.profile.dir / 'sep2' / 'tls'

    def certs(self, ctx: Context) -> dict[str, Path]:
        self.check_docker()
        present = sep2.images_present()
        if not present[sep2.SERVER_IMAGE]:
            raise NotReady(f'{self.name}: docker image {sep2.SERVER_IMAGE} is missing (built from the GridAPPS-D server repository with the 2030.5 driver\'s Dockerfile.e2e)')
        return sep2.generate_certs(self.cert_dir(ctx))

    def running(self, ctx: Context) -> bool:
        return sep2.container_running(self.container_name(ctx))

    def stop(self, ctx: Context) -> None:
        if sep2.container_stop(self.container_name(ctx)):
            ctx.out(f'{self.name}: stopped')

    def logs(self, ctx: Context, lines: int = 40) -> str:
        return sep2.container_logs(self.container_name(ctx), lines)


class Sep2Server(Sep2Base):
    name = 'sep2-server'
    description = 'GridAPPS-D Go 2030.5 utility server (docker): the server the platform\'s DER client talks to'
    Q_PORT = Question('sep2_port', '2030.5 server port', default=9443, kind='int')
    Q_ADMIN = Question('sep2_admin_port', '2030.5 server admin API port', default=9444, kind='int')
    Q_NOTIFY = Question('sep2_notify_port', 'Port the platform\'s client listens on for notifications', default=9445, kind='int')
    Q_TOPIC = Question('sep2_topic', 'Topic of the 2030.5 server as a device in the platform (devices/<topic>, used by register)', default='icm/sep2/der')

    def questions(self):
        return [self.Q_PORT, self.Q_ADMIN, self.Q_NOTIFY, self.Q_TOPIC]

    def install(self, ctx: Context) -> None:
        for q in (self.Q_PORT, self.Q_ADMIN, self.Q_NOTIFY):
            ctx.answers.ask(q)
        certs = self.certs(ctx)
        ctx.out(f'sep2-server: certificates in {self.cert_dir(ctx)} (CA, server, device "volttron", device "goclient", notify receiver)')

    def start(self, ctx: Context) -> None:
        if self.running(ctx):
            ctx.out(f'{self.name}: already running'); return
        certs = self.certs(ctx)
        a = ctx.profile.answers
        lfdi = sep2.lfdi_from_cert(certs['device_cert'])
        fixture = ctx.profile.dir / 'sep2' / 'fixture' / 'boot.yaml'
        sep2.write_fixture(fixture, lfdi, sep2.sfdi_from_lfdi(lfdi))
        sep2.start_server(self.container_name(ctx), self.cert_dir(ctx), fixture, int(a['sep2_port']), int(a['sep2_admin_port']), ctx.profile.dir / 'sep2' / 'capture')
        ctx.out(f'{self.name}: https://127.0.0.1:{a["sep2_port"]} (admin http://127.0.0.1:{a["sep2_admin_port"]}), end device LFDI {lfdi}')

    def status(self, ctx: Context) -> Status:
        installed = ctx.profile.is_installed(self.name)
        return Status(installed, self.running(ctx) if installed else False, f'https://127.0.0.1:{ctx.profile.answers.get("sep2_port")}' if installed else '')

    def connection_details(self, ctx: Context) -> str:
        a, c = ctx.profile.answers, sep2.certificate_paths(self.cert_dir(ctx))
        return (f'sep2-server: https://127.0.0.1:{a.get("sep2_port", 9443)}; client certificate {c["device_cert"]}, key {c["device_key"]}, CA {c["ca"]}; '
                f'notification receiver 127.0.0.1:{a.get("sep2_notify_port", 9445)} with {c["notify_cert"]} / {c["notify_key"]}; '
                f'registry {DATA / "registries" / "sep2_pv.ieee2030_5.csv"}; suggested topic {a.get("sep2_topic", self.Q_TOPIC.default)}')

    def register(self, ctx: Context) -> None:
        _platform_up(ctx)
        for q in self.questions():
            ctx.answers.ask(q)
        a, c = ctx.profile.answers, sep2.certificate_paths(self.cert_dir(ctx))
        if not c['device_cert'].exists():
            self.certs(ctx)
        topic = a['sep2_topic'].strip('/')
        fmt = _register_device(ctx, topic, 'sep2_pv.ieee2030_5.csv', 'sep2_pv.csv', 'ieee2030_5', {
            'server_url': f'https://127.0.0.1:{a["sep2_port"]}', 'cert_path': str(c['device_cert']), 'key_path': str(c['device_key']), 'ca_path': str(c['ca']),
            'description': 'ICM',    # what the client registers as its EndDevice description; the driver's own default names the platform
            'subscribe': True, 'notify_host': '127.0.0.1', 'notify_bind_host': '127.0.0.1', 'notify_port': int(a['sep2_notify_port']),
            'notify_cert_path': str(c['notify_cert']), 'notify_key_path': str(c['notify_key']), 'poll_rate_floor': 1, 'poll_rate_ceiling': 5,
            'response_timeout': 10}, 'sep2_pv', served=False)
        ctx.out(f'sep2-server: registered as devices/{topic} (registry sep2_pv.csv, resource {topic} in format {fmt})')

    def create_control(self, ctx: Context, max_lim_w: int) -> str:
        return sep2.create_control(int(ctx.profile.answers['sep2_admin_port']), max_lim_w)


class Sep2Client(Sep2Base):
    name = 'sep2-client'
    after_register = True
    description = 'GridAPPS-D Go 2030.5 DER client (docker): registers with the server the platform serves'
    needs = ('driver',)
    Q_PORT = Question('sep2_served_port', 'Port of the 2030.5 server the platform serves', default=9446, kind='int')
    Q_PIN = Question('sep2_served_pin', 'Registration PIN', default=111115, kind='int')
    Q_NOTIFY = Question('sep2_client_notify_port', 'Port the Go client listens on for notifications', default=9447, kind='int')
    Q_TOPIC = Question('sep2_served_topic', 'Topic of the 2030.5 server the platform serves for the Go client (devices/<topic>)', default='icm/sep2/served')

    def questions(self):
        return [self.Q_PORT, self.Q_PIN, self.Q_NOTIFY, self.Q_TOPIC]

    def install(self, ctx: Context) -> None:
        for q in self.questions():
            ctx.answers.ask(q)
        self.certs(ctx)
        if not sep2.images_present()[sep2.CLIENT_IMAGE]:
            raise NotReady(f'{self.name}: docker image {sep2.CLIENT_IMAGE} is missing (built from the GridAPPS-D client repository)')

    def start(self, ctx: Context) -> None:
        if self.running(ctx):
            ctx.out(f'{self.name}: already running'); return
        a = ctx.profile.answers
        topic = a['sep2_served_topic'].strip('/')
        if not _device_registered(ctx, topic):
            raise NotReady(f'sep2-client: no served 2030.5 server at devices/{topic}; run icmstack register sep2-client first')
        if not wait_for(lambda: port_open('127.0.0.1', int(a['sep2_served_port'])), 60):
            raise NotReady(f'sep2-client: nothing listens on 127.0.0.1:{a["sep2_served_port"]}; is the driver\'s served 2030.5 server up?')
        sep2.start_client(self.container_name(ctx), self.cert_dir(ctx), f'https://127.0.0.1:{a["sep2_served_port"]}', int(a['sep2_served_pin']), int(a['sep2_client_notify_port']))
        ctx.out(f'{self.name}: registering with https://127.0.0.1:{a["sep2_served_port"]} (PIN {a["sep2_served_pin"]})')

    def status(self, ctx: Context) -> Status:
        installed = ctx.profile.is_installed(self.name)
        return Status(installed, self.running(ctx) if installed else False, f'-> https://127.0.0.1:{ctx.profile.answers.get("sep2_served_port")}' if installed else '')

    def connection_details(self, ctx: Context) -> str:
        a, c = ctx.profile.answers, sep2.certificate_paths(self.cert_dir(ctx))
        lfdi = sep2.lfdi_from_cert(c['goclient_cert']) if c['goclient_cert'].exists() else '(certificates not generated yet)'
        return (f'sep2-client: connects to a served server on https://127.0.0.1:{a.get("sep2_served_port", 9446)} with PIN {a.get("sep2_served_pin", 111115)}; '
                f'its LFDI {lfdi}; served registry {DATA / "registries" / "sep2_pv_served.ieee2030_5.csv"}; topic {a.get("sep2_served_topic", self.Q_TOPIC.default)}')

    def register(self, ctx: Context) -> None:
        """The served device: the platform's driver as a 2030.5 server expecting the Go client's LFDI and PIN."""
        _platform_up(ctx)
        for q in self.questions():
            ctx.answers.ask(q)
        c = self.certs(ctx)
        a = ctx.profile.answers
        topic = a['sep2_served_topic'].strip('/')
        state_dir = ctx.profile.dir / 'sep2' / 'state'; state_dir.mkdir(parents=True, exist_ok=True)
        fmt = _register_device(ctx, topic, 'sep2_pv_served.ieee2030_5.csv', 'sep2_pv_served.csv', 'ieee2030_5', {
            'driver_role': 'server', 'bind_host': '127.0.0.1', 'port': int(a['sep2_served_port']), 'cert_path': str(c['server_cert']),
            'key_path': str(c['server_key']), 'ca_path': str(c['ca']), 'client_lfdi': sep2.lfdi_from_cert(c['goclient_cert']),
            'client_pin': int(a['sep2_served_pin']), 'poll_rate': 2, 'immediate_control_duration': 120, 'state_dir': str(state_dir)}, 'sep2_pv', served=True)
        ctx.out(f'sep2-client: served 2030.5 server registered as devices/{topic} (format {fmt}); the driver binds 127.0.0.1:{a["sep2_served_port"]}')
