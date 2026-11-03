"""Local MQTT exercise only: this does not control a real robot or reserve routes."""
import argparse
import json
import sys


ROBOT_ID = 'demo-robot1'


def encode(data):
    return json.dumps(data, ensure_ascii=False, separators=(',', ':'))


def decode(payload):
    try:
        obj = json.loads(payload.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


class DemoServer:
    def __init__(self, robot_id):
        self.robot_id = robot_id
        self.state_topic = 'agv/{}/state'.format(robot_id)
        self.order_topic = 'agv/{}/order'.format(robot_id)
        self.sent_order = False

    def on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            print('[server] connection failed: {}'.format(reason_code), flush=True)
            return
        print('[server] connected; subscribing to {}'.format(self.state_topic), flush=True)
        client.subscribe(self.state_topic, qos=0)

    def on_subscribe(self, client, userdata, mid, reason_codes, properties):
        if any(code.is_failure for code in reason_codes):
            print('[server] subscription rejected', flush=True)
        else:
            print('[server] ready; start the fake robot now', flush=True)

    def on_message(self, client, userdata, message):
        data = decode(message.payload)
        if message.retain or not data or data.get('robot_id') != self.robot_id or data.get('simulated') is not True:
            return
        print('[server] state received: {}'.format(encode(data)), flush=True)
        if data.get('status') != 'idle' or self.sent_order:
            return
        order = {'order_id': 'demo-001', 'seq': 1, 'map_id': 'demo-map@v1',
                 'nodes': ['N3', 'N4'], 'released_until': 'N4',
                 'action_at_end': {'type': 'unload', 'shelf': 'demo-S2'}}
        info = client.publish(self.order_topic, encode(order), qos=1, retain=False)
        # The return code says queued locally; it is not proof of delivery.
        if info is not None and info.rc != 0:
            print('[server] order not queued: {}'.format(info.rc), flush=True)
            return
        self.sent_order = True
        print('[server] simulated order queued: {}'.format(encode(order)), flush=True)


class DemoRobot:
    def __init__(self, robot_id):
        self.robot_id = robot_id
        self.state_topic = 'agv/{}/state'.format(robot_id)
        self.order_topic = 'agv/{}/order'.format(robot_id)

    def on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            print('[robot] connection failed: {}'.format(reason_code), flush=True)
            return
        print('[robot] connected; subscribing to {}'.format(self.order_topic), flush=True)
        client.subscribe(self.order_topic, qos=1)

    def on_subscribe(self, client, userdata, mid, reason_codes, properties):
        if not reason_codes or any(code.is_failure or code.value != 1 for code in reason_codes):
            print('[robot] order subscription rejected or QoS downgraded', flush=True)
            return
        state = {'robot_id': self.robot_id, 'status': 'idle', 'simulated': True}
        info = client.publish(self.state_topic, encode(state), qos=0, retain=False)
        if info is not None and info.rc != 0:
            print('[robot] state not queued: {}'.format(info.rc), flush=True)
            return
        print('[robot] simulated state queued: {}'.format(encode(state)), flush=True)

    def on_message(self, client, userdata, message):
        order = decode(message.payload)
        if not order or not isinstance(order.get('order_id'), str):
            print('[robot] invalid order ignored', flush=True)
            return
        print('[robot] simulated order received: {}'.format(encode(order)), flush=True)
        state = {'robot_id': self.robot_id, 'status': 'order_received',
                 'order_id': order['order_id'], 'simulated': True}
        info = client.publish(self.state_topic, encode(state), qos=0, retain=False)
        if info is not None and info.rc != 0:
            print('[robot] receipt state not queued: {}'.format(info.rc), flush=True)
            return
        print('[robot] simulated receipt queued; no physical movement', flush=True)


def main():
    parser = argparse.ArgumentParser(description='Local-only simulated MQTT exercise')
    parser.add_argument('role', choices=('server', 'robot'))
    parser.add_argument('--port', type=int, default=1883)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('port must be between 1 and 65535')
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        print('Install paho-mqtt 2.1.0 first; see server/demo/README.md.', file=sys.stderr)
        return 1
    demo = DemoServer(ROBOT_ID) if args.role == 'server' else DemoRobot(ROBOT_ID)
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv311)
    client.on_connect = demo.on_connect
    client.on_subscribe = demo.on_subscribe
    client.on_message = demo.on_message
    try:
        client.connect('127.0.0.1', args.port, keepalive=30)
        client.loop_forever()
    except (OSError, ValueError) as error:
        print('[{}] broker connection error: {}'.format(args.role, error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('\n[{}] stopping'.format(args.role), flush=True)
        client.disconnect()
    return 0


if __name__ == '__main__':
    sys.exit(main())
