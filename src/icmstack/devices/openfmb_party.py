"""An OpenFMB participant on an MQTT broker: publishes a reading profile at an interval with moving values and logs
every profile it receives on a topic prefix, decoded. What a utility system or another adapter would be.
Runnable: ``python -m icmstack.devices.openfmb_party --port 1883 --topic openfmb/solarmodule/SolarReadingProfile/<mrid>``.
Needs paho-mqtt and the OpenFMB codecs of ``icm-lib-resources[openfmb]``."""
from __future__ import annotations

import argparse
import json
import math
import sys
import time

READING_PROTO = {'solar': 'solarmodule.SolarReadingProfile', 'ess': 'essmodule.ESSReadingProfile'}
CONTROL_PROTO = {'solar': 'solarmodule.SolarControlProfile', 'ess': 'essmodule.ESSControlProfile'}
DEFAULT_MRID = '7d1a2b3c-0000-4000-8000-000000000001'


def reading_payload(family: str, mrid: str, w: float, var: float, hz: float, phase_v: float) -> dict:
    """A reading profile in the protobuf-JSON shape the codecs take: power, reactive power, frequency, a phase voltage."""
    key = 'solarReading' if family == 'solar' else 'essReading'
    return {'readingMessageInfo': {'messageInfo': {'identifiedObject': {'mRID': mrid}}},    # a wrapped string is a plain JSON string
            key: {'readingMMXU': {'W': {'net': {'cVal': {'mag': w}}}, 'VAr': {'net': {'cVal': {'mag': var}}},
                                  'Hz': {'mag': hz}, 'PhV': {'phsA': {'cVal': {'mag': phase_v}}}}}}


def control_payload(family: str, mrid: str, limit_pct: float, enabled: bool = True) -> dict:
    """A control profile with one schedule point limiting active power (limitWOperation), as the e2e harness sends."""
    a, b, c = (('solarControl', 'solarControlFSCC', 'SolarControlScheduleFSCH') if family == 'solar' else ('essControl', 'essControlFSCC', 'essControlScheduleFSCH'))
    entry = {'startTime': {'seconds': int(time.time())}, 'control': {'limitWOperation': {'maxLimParameter': {'modEna': enabled}, 'wMaxSptVal': limit_pct}}}
    return {'controlMessageInfo': {'messageInfo': {'identifiedObject': {'mRID': mrid}}}, a: {b: {c: {'ValDCSG': {'crvPts': [entry]}}}}}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--port', type=int, default=1883)
    p.add_argument('--family', choices=sorted(READING_PROTO), default='solar')
    p.add_argument('--mrid', default=DEFAULT_MRID)
    p.add_argument('--topic', default=None, help='publish topic (default openfmb/<family>module/<Reading profile>/<mrid>)')
    p.add_argument('--subscribe', default='openfmb/#', help='topic filter to log what others publish')
    p.add_argument('--interval', type=float, default=5.0, help='seconds between readings; 0 publishes nothing')
    p.add_argument('--client-id', default='icmstack-openfmb-party')
    p.add_argument('--control-limit', type=float, default=None, help='publish a control profile limiting active power to this percent on the control topic of --control-mrid')
    p.add_argument('--control-mrid', default='7d1a2b3c-0000-4000-8000-000000000011', help='mRID of the platform\'s alias the control is addressed to')
    p.add_argument('--control-every', type=float, default=0, help='repeat the control every N seconds (0: once at start)')
    args = p.parse_args(argv)
    try:
        import paho.mqtt.client as mqtt
        from icm.resources.codecs.openfmb import decode, encode
    except ImportError as e:
        print(f'missing package: {e}; pip install paho-mqtt "icm-lib-resources[openfmb]"', file=sys.stderr)
        return 2
    profile = 'Solar' if args.family == 'solar' else 'ESS'
    topic = args.topic or f'openfmb/{args.family}module/{profile}ReadingProfile/{args.mrid}'
    proto, control = READING_PROTO[args.family], CONTROL_PROTO[args.family]

    def on_message(client, userdata, message):
        if message.topic == topic:
            return
        # Protobuf decodes leniently (a reading parses as a control with unknown fields), so the profile is taken from
        # the topic, OpenFMB's convention: openfmb/<module>/<Profile>/<mrid>.
        parts = message.topic.split('/')
        candidate = f'{parts[1]}.{parts[2]}' if len(parts) >= 3 and parts[0] == 'openfmb' else proto
        try:
            decoded = decode(candidate, message.payload)
        except Exception as e:
            print(f'received {message.topic}: {len(message.payload)} bytes, not decodable as {candidate}: {e}', flush=True)
            return
        summary = ''
        reading = decoded.get('solarReading') or decoded.get('essReading')
        if reading:
            mmxu = reading.get('readingMMXU', {})
            w = mmxu.get('W', {}).get('net', {}).get('cVal', {}).get('mag'); hz = mmxu.get('Hz', {}).get('mag')
            summary = f' W={w} Hz={hz}'
        print(f'received {message.topic} as {candidate}:{summary} {json.dumps(decoded)[:300]}', flush=True)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=args.client_id)
    client.on_message = on_message
    client.connect(args.host, args.port, keepalive=30)
    client.loop_start()
    client.subscribe(args.subscribe, qos=1)
    print(f'openfmb party: broker {args.host}:{args.port}, publishing {proto} on {topic} every {args.interval} s, logging {args.subscribe}', flush=True)
    control_topic = f'openfmb/{args.family}module/{profile}ControlProfile/{args.control_mrid}'
    last_control = None

    def publish_control(limit: float):
        client.publish(control_topic, encode(control, control_payload(args.family, args.control_mrid, limit)), qos=1)
        print(f'published control limit {limit} % on {control_topic}', flush=True)
    n = 0
    try:
        while True:
            if args.control_limit is not None and (last_control is None or (args.control_every > 0 and time.time() - last_control >= args.control_every)):
                publish_control(args.control_limit); last_control = time.time()
            if args.interval > 0:
                t = n * args.interval
                payload = reading_payload(args.family, args.mrid, w=round(4000 + 500 * math.sin(t / 60), 1), var=round(300 + 50 * math.cos(t / 60), 1),
                                          hz=round(60 + 0.02 * math.sin(t / 30), 3), phase_v=round(240 + 2 * math.sin(t / 45), 1))
                client.publish(topic, encode(proto, payload), qos=1)
                print(f'published W={payload[list(payload)[1]]["readingMMXU"]["W"]["net"]["cVal"]["mag"]} on {topic}', flush=True)
                n += 1
                time.sleep(args.interval)
            else:
                time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        client.disconnect()
        client.loop_stop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
