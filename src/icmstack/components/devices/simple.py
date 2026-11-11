"""The remaining pure-Python devices: the Modbus simulator, the OpenFMB participant and the driver's fake device."""
from __future__ import annotations

import sys
from pathlib import Path

from ...processes import ManagedProcess, port_open, wait_for
from ...prompts import Question
from ..base import Component, Context, NotReady, Status

DATA = Path(__file__).resolve().parents[2] / 'data' / 'registries'


class ProcessDevice(Component):
    """A device run as a child of this tool's interpreter (``python -m icmstack.devices.<module>``)."""
    group = 'device'
    step = 3
    module = ''
    requires = ()                         # importable names the module needs

    def process(self, ctx: Context) -> ManagedProcess:
        return ManagedProcess(self.name, ctx.profile)

    def check_requirements(self) -> None:
        for name, hint in self.requires:
            try:
                __import__(name)
            except ImportError:
                raise NotReady(f'{self.name}: the {name} package is missing in icmstack\'s environment: {hint}')

    def args(self, ctx: Context) -> list[str]:
        raise NotImplementedError

    def listens(self, ctx: Context) -> tuple[str, int] | None:
        return None

    def start(self, ctx: Context) -> None:
        proc = self.process(ctx)
        if proc.running():
            ctx.out(f'{self.name}: already running')
            return
        self.check_requirements()
        proc.start([sys.executable, '-m', f'icmstack.devices.{self.module}', *self.args(ctx)])
        where = self.listens(ctx)
        ready = (lambda: port_open(*where)) if where else (lambda: 'party:' in proc.tail(5) or 'simulator:' in proc.tail(5))
        if not wait_for(lambda: ready() or not proc.running(), 20):
            raise NotReady(f'{self.name} did not come up:\n{proc.tail(10)}')
        if not proc.running():
            raise NotReady(f'{self.name} exited:\n{proc.tail(10)}')
        banner = next((l for l in proc.tail(10).splitlines() if 'party:' in l or 'simulator:' in l), proc.tail(1).strip())
        ctx.out(f'{self.name}: {banner.strip()}')

    def stop(self, ctx: Context) -> None:
        if self.process(ctx).stop():
            ctx.out(f'{self.name}: stopped')

    def status(self, ctx: Context) -> Status:
        installed = ctx.profile.is_installed(self.name)
        return Status(installed, self.process(ctx).running() if installed else False, self.detail(ctx) if installed else '')

    def detail(self, ctx: Context) -> str:
        return ''

    def logs(self, ctx: Context, lines: int = 40) -> str:
        return self.process(ctx).tail(lines)


class ModbusSim(ProcessDevice):
    name = 'modbus-sim'
    description = 'plain Modbus simulator (pymodbus)'
    module = 'modbus_sim'
    requires = (('pymodbus', 'pip install pymodbus'),)
    Q_HOST = Question('modbus_sim_host', 'Modbus simulator host', default='127.0.0.1')
    Q_PORT = Question('modbus_sim_port', 'Modbus simulator port', default=5021, kind='int')
    Q_TOPIC = Question('modbus_sim_topic', 'Topic of the Modbus simulator in the platform (devices/<topic>, used by register)', default='icm/modbus/rtu')

    def questions(self):
        return [self.Q_HOST, self.Q_PORT, self.Q_TOPIC]

    def install(self, ctx: Context) -> None:
        self.check_requirements()
        ctx.answers.ask(self.Q_HOST); ctx.answers.ask(self.Q_PORT)

    def args(self, ctx):
        a = ctx.profile.answers
        return ['--host', a['modbus_sim_host'], '--port', str(a['modbus_sim_port'])]

    def listens(self, ctx):
        return ctx.profile.answers['modbus_sim_host'], int(ctx.profile.answers['modbus_sim_port'])

    def detail(self, ctx):
        return f'{ctx.profile.answers.get("modbus_sim_host")}:{ctx.profile.answers.get("modbus_sim_port")} unit 1'

    def connection_details(self, ctx: Context) -> str:
        a = ctx.profile.answers
        return (f'modbus-sim: Modbus TCP, host {a.get("modbus_sim_host", "127.0.0.1")}, port {a.get("modbus_sim_port", 5021)}, unit id 1; '
                f'registry {DATA / "modbus_sim.modbus.csv"}; suggested topic {a.get("modbus_sim_topic", self.Q_TOPIC.default)}')

    def register(self, ctx: Context) -> None:
        """Registry and a Modbus client device entry; no resource, since a plain RTU has no hub format."""
        from ...platform_config import device_entry, store_csv, store_json
        from ..platform_modular import Platform
        if not Platform().process(ctx).running():
            raise NotReady('the platform is not running')
        for q in self.questions():
            ctx.answers.ask(q)
        a = ctx.profile.answers
        topic = a['modbus_sim_topic'].strip('/')
        store_csv(ctx, 'platform.driver', 'registry_configs/modbus_sim.csv', DATA / 'modbus_sim.modbus.csv')
        store_json(ctx, 'platform.driver', f'devices/{topic}', device_entry('modbus', 'modbus_sim.csv', {
            'transport_protocol': 'tcp', 'device_address': a['modbus_sim_host'], 'port': int(a['modbus_sim_port']), 'unit_id': 1, 'timeout': 5}))
        ctx.out(f'modbus-sim: registered as devices/{topic} (registry modbus_sim.csv, no resource: a plain RTU has no hub format)')


class OpenFmbParty(ProcessDevice):
    name = 'openfmb-party'
    description = 'OpenFMB participant on the broker (publishes readings, logs what it receives)'
    module = 'openfmb_party'
    needs = ('broker',)
    requires = (('paho.mqtt.client', 'pip install paho-mqtt'), ('icm.resources.codecs.openfmb', 'pip install "icm-lib-resources[openfmb]"'))
    Q_FAMILY = Question('openfmb_family', 'OpenFMB profile family', default='solar', kind='choice', choices=('solar', 'ess'))
    Q_MRID = Question('openfmb_mrid', 'Device mRID in the profiles', default='7d1a2b3c-0000-4000-8000-000000000001')
    Q_INTERVAL = Question('openfmb_interval', 'Seconds between readings (0: publish nothing)', default=5, kind='float')
    Q_CONTROL = Question('openfmb_control_limit', 'Active power limit (%) to publish as a SolarControlProfile at start, empty: none', default='')

    def questions(self):
        return [self.Q_FAMILY, self.Q_MRID, self.Q_INTERVAL, self.Q_CONTROL]

    def install(self, ctx: Context) -> None:
        self.check_requirements()
        for q in self.questions():
            ctx.answers.ask(q)

    def broker(self, ctx: Context) -> tuple[str, int]:
        a = ctx.profile.answers
        spec = a.get('broker', 'docker')
        if spec == 'docker':
            return '127.0.0.1', int(a.get('broker_port', 1883))
        host, _, port = str(spec).rpartition(':')
        return host or '127.0.0.1', int(port or 1883)

    def topic(self, ctx: Context) -> str:
        a = ctx.profile.answers
        family = a.get('openfmb_family', 'solar')
        return f'openfmb/{family}module/{"Solar" if family == "solar" else "ESS"}ReadingProfile/{a.get("openfmb_mrid", self.Q_MRID.default)}'

    def args(self, ctx):
        a = ctx.profile.answers
        host, port = self.broker(ctx)
        args = ['--host', host, '--port', str(port), '--family', a['openfmb_family'], '--mrid', a['openfmb_mrid'], '--interval', str(a['openfmb_interval'])]
        if a.get('openfmb_control_limit') not in (None, ''):
            args += ['--control-limit', str(a['openfmb_control_limit'])]
        return args

    def detail(self, ctx):
        host, port = self.broker(ctx)
        return f'{self.topic(ctx)} via {host}:{port}'

    def connection_details(self, ctx: Context) -> str:
        host, port = self.broker(ctx)
        return (f'openfmb-party: broker {host}:{port}; publishes {self.topic(ctx)} (protobuf, edition default); '
                f'listens on openfmb/# and logs controls. Register an alias with this UAI and a remote-topic delivery on the Resources tab.')

    def register(self, ctx: Context) -> None:
        ctx.out('openfmb-party: nothing of its own to register; the demo preset wires the SunSpec device\'s readings to an OpenFMB alias '
                'relayed to the broker (icmstack.wiring), which this party then receives and logs')


class FakeDevice(Component):
    name = 'fake-device'
    group = 'device'
    description = 'the driver\'s fake interface: a device with no hardware (configuration only)'
    needs = ('driver',)
    step = 3
    Q_TOPIC = Question('fake_topic', 'Topic of the fake device in the platform (devices/<topic>)', default='icm/fake/device1')

    def questions(self):
        return [self.Q_TOPIC]

    def install(self, ctx: Context) -> None:
        ctx.answers.ask(self.Q_TOPIC)

    def start(self, ctx: Context) -> None:
        state = self.registered(ctx)
        if state is None:
            raise NotReady('the platform is not running')
        if not state:
            self.register(ctx)
        else:
            ctx.out(f'fake-device: configuration only; devices/{ctx.profile.answers["fake_topic"]} is registered')

    def stop(self, ctx: Context) -> None:
        ctx.out('fake-device: configuration only, nothing to stop (remove it from the Devices tab, or with register --unregister later)')

    def registered(self, ctx: Context) -> bool | None:
        from ..platform_modular import Platform, vctl
        if not Platform().process(ctx).running():
            return None
        done = vctl(ctx, 'config', 'list', 'platform.driver', check=False, timeout=60)
        return done.returncode == 0 and f'devices/{ctx.profile.answers.get("fake_topic", "")}' in done.stdout

    def status(self, ctx: Context) -> Status:
        installed = ctx.profile.is_installed(self.name)
        reg = self.registered(ctx) if installed else None
        return Status(installed, None, (f'devices/{ctx.profile.answers.get("fake_topic")}' + ('' if reg else ' (not in the store)' if reg is not None else ' (platform down)')) if installed else '')

    def connection_details(self, ctx: Context) -> str:
        return f'fake-device: driver_type fake, registry {DATA / "fake.csv"} (25 points: EKG sine wave, Heartbeat, temperatures); topic {ctx.profile.answers.get("fake_topic", self.Q_TOPIC.default)}'

    def register(self, ctx: Context) -> None:
        from ...platform_config import store_csv, store_json
        from ..platform_modular import Platform
        if not Platform().process(ctx).running():
            raise NotReady('the platform is not running')
        topic = ctx.answers.ask(self.Q_TOPIC).strip('/')
        store_csv(ctx, 'platform.driver', 'registry_configs/fake.csv', DATA / 'fake.csv')
        store_json(ctx, 'platform.driver', f'devices/{topic}', {'driver_type': 'fake', 'driver_config': {}, 'registry_config': 'config://registry_configs/fake.csv', 'interval': 5})
        ctx.out(f'fake-device: registered as devices/{topic} (registry fake.csv)')
