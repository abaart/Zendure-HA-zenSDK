from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo
import yaml
import pytest
from jinja2 import Environment

PACKAGE = yaml.safe_load((Path(__file__).parents[1] / 'Dutch (NL) Integration/packages/zendure_health_nl.yaml').read_text())

@pytest.fixture
def health():
    now = datetime(2026, 10, 22, 16, tzinfo=ZoneInfo('Europe/Amsterdam'))
    values = {'sensor.memory_free': '2000', 'sensor.zendure_proxy_uptime': '600',
              'input_text.dynamisch_nordpool_sensor': 'sensor.nordpool',
              'input_boolean.dynamisch_handelsstrategie_berekening_bezig': 'off'}
    attrs = {'sensor.dynamisch_handelsstrategie': {'bijgewerkt': now.isoformat()},
             'sensor.nordpool': {'tomorrow_valid': True, 'raw_tomorrow': [{'start': (now + timedelta(days=1)).isoformat()}]}}
    changed = {'time': now}
    class States:
        def __call__(self, entity): return values.get(entity, 'unknown')
        input_boolean = SimpleNamespace(dynamisch_handelsstrategie_berekening_bezig=SimpleNamespace(last_changed=now))
    states = States()
    def dt(value, default=None):
        if isinstance(value, datetime): return value
        try: return datetime.fromisoformat(value)
        except (TypeError, ValueError): return default
    def ts(value, default=None):
        parsed = dt(value)
        return parsed.timestamp() if parsed else default
    env = Environment()
    env.globals.update(states=states, state_attr=lambda e,a: attrs.get(e,{}).get(a),
                       is_state=lambda e,v: states(e)==v, now=lambda: changed['time'], timedelta=timedelta,
                       as_datetime=dt, as_timestamp=ts, as_local=lambda d:d.astimezone(now.tzinfo))
    sensor = PACKAGE['template'][0]['binary_sensor'][0]
    def render(): return env.from_string(sensor['state']).render().strip() == 'True'
    return values, attrs, changed, states, render


def test_healthy(health): assert not health[-1]()

@pytest.mark.parametrize('failure', ['stale', 'missing', 'busy', 'failed', 'prices', 'wrong_date', 'low_ram', 'missing_ram', 'restarts'])
def test_detects_failure(health, failure):
    values, attrs, clock, states, render = health
    if failure == 'stale': attrs['sensor.dynamisch_handelsstrategie']['bijgewerkt'] = (clock['time']-timedelta(minutes=91)).isoformat()
    if failure == 'missing': attrs['sensor.dynamisch_handelsstrategie'] = {}
    if failure == 'busy':
        values['input_boolean.dynamisch_handelsstrategie_berekening_bezig'] = 'on'
        states.input_boolean.dynamisch_handelsstrategie_berekening_bezig.last_changed -= timedelta(minutes=11)
    if failure == 'failed': attrs['sensor.dynamisch_handelsstrategie_berekening_duur'] = {'status': 'fout'}
    if failure == 'prices': attrs['sensor.nordpool']['tomorrow_valid'] = False
    if failure == 'wrong_date': attrs['sensor.nordpool']['raw_tomorrow'][0]['start'] = clock['time'].isoformat()
    if failure == 'low_ram': values['sensor.memory_free'] = '299'
    if failure == 'missing_ram': values['sensor.memory_free'] = 'unavailable'
    if failure == 'restarts': values['sensor.zendure_proxy_uptime'] = '20'
    assert render()


def test_morning_without_tomorrow_prices(health):
    _, attrs, clock, _, render = health
    clock['time'] = clock['time'].replace(hour=14)
    attrs['sensor.dynamisch_handelsstrategie']['bijgewerkt'] = clock['time'].isoformat()
    attrs['sensor.nordpool']['tomorrow_valid'] = False
    assert not render()


def test_debounce_and_deduplication():
    sensor = PACKAGE['template'][0]['binary_sensor'][0]
    assert sensor['delay_on'] == {'minutes': 5}
    assert sensor['delay_off'] == {'minutes': 2}
    choices = PACKAGE['automation'][0]['actions'][0]['choose']
    assert "'off'" in choices[0]['conditions']
    assert "'on'" in choices[1]['conditions']
    assert choices[0]['sequence'][-1]['action'] == 'input_boolean.turn_on'
    assert choices[1]['sequence'][-1]['action'] == 'input_boolean.turn_off'
