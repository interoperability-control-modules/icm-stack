"""The agents: one component class, parametrised. Installed into the platform's environment (PyPI by default, a
checkout with --agent-source checkout or --source NAME=PATH_OR_SPEC), then registered with vctl install --enable."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from ..processes import wait_for
from ..prompts import Question
from .base import Component, Context, NotReady, Status
from .platform_modular import Platform, env_of, vctl

Q_SOURCE = Question('agent_source', 'Install the agents from PyPI releases (pypi) or from your checkouts under $ICM_MODULES and $VOLTTRON_MODULAR_DRIVERS (checkout)',
                    default='pypi', kind='choice', choices=('pypi', 'checkout'),
                    help='One agent can be taken from elsewhere with --source-<agent> PATH_OR_REQUIREMENT (a directory, a wheel or a pip requirement); that is not asked.')


def checkout_root(variable: str) -> Path | None:
    for name in ((variable, 'DER_CONTROL_MODULES') if variable == 'ICM_MODULES' else (variable,)):
        if os.environ.get(name):
            return Path(os.environ[name]).expanduser()
    if variable == 'VOLTTRON_MODULAR_PROXIES' and os.environ.get('VOLTTRON_MODULAR_DRIVERS'):
        sibling = Path(os.environ['VOLTTRON_MODULAR_DRIVERS']).expanduser().parent / 'proxies'    # the modular tree: drivers/ next to proxies/
        if sibling.is_dir():
            return sibling
    return None


#: Libraries the agents need beyond their own package, and where their checkouts live when installing from source.
EXTRA_CHECKOUTS = {'volttron-lib-base-driver': ('VOLTTRON_MODULAR_DRIVERS', 'base-driver'), 'volttron-lib-fake-driver': ('VOLTTRON_MODULAR_DRIVERS', 'interfaces/fake'),
                   'protocol-proxy': ('VOLTTRON_MODULAR_PROXIES', 'protocol-proxy'), 'protocol-proxy-mqtt': ('VOLTTRON_MODULAR_PROXIES', 'mqtt'),
                   'icm-lib-transforms': ('ICM_MODULES', 'icm-utils/icm-lib-transforms'),
                   'icm-lib-resources[volttron,openfmb]': ('ICM_MODULES', 'icm-utils/icm-lib-resources'),
                   'icm-lib-models-sunspec': ('ICM_MODULES', 'icm-utils/icm-lib-models-sunspec'),
                   'icm-lib-models-ieee1815-2': ('ICM_MODULES', 'icm-utils/icm-lib-models-ieee1815-2'),
                   'icm-lib-models-ieee2030-5': ('ICM_MODULES', 'icm-utils/icm-lib-models-ieee2030-5'),
                   'icm-lib-models-openfmb': ('ICM_MODULES', 'icm-utils/icm-lib-models-openfmb')}
#: The two shared libraries every interoperability agent imports (the service, the Resource Bridge, the bus adapter).
ICM_LIBS = ('icm-lib-transforms', 'icm-lib-resources[volttron,openfmb]')
#: The standards' model packages, which the service's format universe and discovery need.
MODEL_LIBS = ('icm-lib-models-sunspec', 'icm-lib-models-ieee1815-2', 'icm-lib-models-ieee2030-5', 'icm-lib-models-openfmb')


@dataclass
class AgentSpec:
    name: str
    identity: str
    tag: str
    package: str                      # PyPI distribution (with extras)
    checkout: tuple[str, str]         # (variable, subdirectory)
    description: str
    extra_packages: tuple[str, ...] = ()
    wants: tuple[str, ...] = ()
    optional: bool = False


SPECS = [
    AgentSpec('driver', 'platform.driver', 'driver', 'volttron-platform-driver', ('VOLTTRON_MODULAR_DRIVERS', 'platform-driver-agent'),
              'platform driver agent', extra_packages=('volttron-lib-base-driver', 'volttron-lib-fake-driver')),
    AgentSpec('interop', 'platform.presentation', 'interop', 'interoperability-service[discovery,openfmb]', ('ICM_MODULES', 'interoperability_service'),
              'interoperability service agent', extra_packages=ICM_LIBS + MODEL_LIBS),
    AgentSpec('resource-bridge', 'platform.resource_bridge', 'resource_bridge', 'volttron-resource-bridge', ('ICM_MODULES', 'resource-bridge'),
              'Resource Bridge agent', extra_packages=ICM_LIBS, wants=('interop', 'driver')),
    AgentSpec('bus-adapter', 'platform.bus_adapter', 'bus_adapter', 'volttron-message-bus-adapter', ('ICM_MODULES', 'message-bus-adapter'),
              'message bus adapter agent', extra_packages=ICM_LIBS + ('protocol-proxy', 'protocol-proxy-mqtt'), wants=('broker', 'interop')),
    AgentSpec('rtc', 'icm.rtc', 'rtc', 'volttron-realtime-control', ('ICM_MODULES', 'rtc_agent'), 'real-time control agent (optional)', wants=('interop',), optional=True),
    AgentSpec('scheduler', 'platform.scheduler', 'scheduler', 'volttron-scheduler', ('ICM_MODULES', 'scheduler'), 'scheduler agent (optional)', optional=True),
]


class Agent(Component):
    group = 'framework'
    needs = ('platform',)
    step = 2

    def __init__(self, spec: AgentSpec):
        self.spec = spec
        self.name, self.description, self.wants = spec.name, spec.description, spec.wants

    def source_question(self) -> Question:
        # A command-line override (--source-<agent>), never asked: the agent_source question covers the usual cases.
        return Question(f'source_{self.spec.name.replace("-", "_")}', f'Where {self.spec.name} is installed from', default='',
                        help=f'a directory, a wheel or a pip requirement; default: {self.spec.package} from PyPI, or the checkout under ${self.spec.checkout[0]}/{self.spec.checkout[1]}')

    def questions(self):
        return [Q_SOURCE, self.source_question()]

    def resolve_source(self, ctx: Context) -> tuple[str, str]:
        """(pip spec, vctl install source): what pip installs into the venv and what vctl registers."""
        q = self.source_question()
        settled, explicit = ctx.answers.settled(q)
        explicit = explicit if settled else ctx.profile.answers.get(q.key)
        if explicit:
            path = Path(explicit).expanduser()
            if path.is_dir():
                return f'-e:{path}', str(path)
            return explicit, explicit if explicit.endswith('.whl') else re.split(r'[\[=<>!~;]', explicit)[0]
        if ctx.answers.ask(Q_SOURCE) == 'checkout':
            root = checkout_root(self.spec.checkout[0])
            if root is None or not (root / self.spec.checkout[1]).is_dir():
                raise NotReady(f'{self.spec.name}: set {self.spec.checkout[0]} to a directory holding {self.spec.checkout[1]}, or pass --{self.source_question().key.replace("_", "-")}')
            path = root / self.spec.checkout[1]
            return f'-e:{path}', str(path)
        return self.spec.package, re.split(r'[\[=<>!~;]', self.spec.package)[0]

    def install(self, ctx: Context) -> None:
        pip_spec, _ = self.resolve_source(ctx)
        env = env_of(ctx)
        if ctx.profile.answers.get('agent_source') == 'checkout':
            # From checkouts, the libraries must come from the checkouts too, and before the agent: a released copy of
            # the base driver next to a checkout of the platform driver is a version mismatch (the driver then fails at
            # start-up), and a library that has no release yet cannot be resolved by the agent's own install at all.
            for package in self.spec.extra_packages:
                variable, subdir = EXTRA_CHECKOUTS.get(package, (None, None))
                root = checkout_root(variable) if variable else None
                base, extras = re.split(r'(?=\[)', package, maxsplit=1) if '[' in package else (package, '')
                if root is not None and (root / subdir).is_dir():
                    env.pip_install([str(root / subdir) + extras], ctx.out, editable=True)
                elif not env.has(base):
                    ctx.out(f'{self.name}: no checkout found for {package} ({variable or "no variable"}); installing it from PyPI')
                    env.pip_install([package], ctx.out)
        if pip_spec.startswith('-e:'):
            env.pip_install([pip_spec[3:]], ctx.out, editable=True)
        else:
            env.pip_install([pip_spec], ctx.out)
        if ctx.profile.answers.get('agent_source') != 'checkout':
            missing = [p for p in self.spec.extra_packages if not env.has(re.split(r'\[', p)[0])]
            if missing:
                env.pip_install(missing, ctx.out)
        if env.has_exe('protocol-proxy-manifests'):
            env.run(['protocol-proxy-manifests', 'export'], check=False)
        ctx.out(f'{self.name}: installed in {env.path}')

    def registered(self, ctx: Context) -> str | None:
        """'running', 'stopped' or None when vctl does not list the identity (or the platform is down)."""
        done = vctl(ctx, 'status', check=False, timeout=60)
        if done.returncode != 0:
            return None
        for line in done.stdout.splitlines():
            if re.search(rf'\s{re.escape(self.spec.identity)}\s', line):
                return 'running' if 'running' in line else 'stopped'
        return None

    def start(self, ctx: Context) -> None:
        if not Platform().process(ctx).running():
            raise NotReady('the platform is not running')
        state = self.registered(ctx)
        if state is None:
            _, source = self.resolve_source(ctx)
            ctx.out(f'{self.name}: vctl install {source} as {self.spec.identity}')
            vctl(ctx, 'install', source, '--vip-identity', self.spec.identity, '--tag', self.spec.tag, '--start', '--enable', timeout=900)
        elif state == 'stopped':
            vctl(ctx, 'start', '--tag', self.spec.tag)
        if not wait_for(lambda: self.registered(ctx) == 'running', 120, 2):
            raise NotReady(f'{self.spec.identity} did not reach running:\n{vctl(ctx, "status", check=False).stdout}')
        ctx.out(f'{self.name}: {self.spec.identity} running')

    def stop(self, ctx: Context) -> None:
        if self.registered(ctx) == 'running':
            vctl(ctx, 'stop', '--tag', self.spec.tag, check=False)
            ctx.out(f'{self.name}: stopped')

    def status(self, ctx: Context) -> Status:
        installed = ctx.profile.is_installed(self.name)
        if not installed:
            return Status(False, False, '')
        if not Platform().process(ctx).running():
            return Status(True, False, f'{self.spec.identity} (platform down)')
        state = self.registered(ctx)
        return Status(True, state == 'running', self.spec.identity + ('' if state else ' (not registered)'))

    def logs(self, ctx: Context, lines: int = 40) -> str:
        text = Platform().logs(ctx, 4000)
        return ''.join(l for l in text.splitlines(keepends=True) if self.spec.identity in l or self.spec.tag in l)[-lines * 200:]


def agent_components() -> list[Agent]:
    return [Agent(spec) for spec in SPECS]
