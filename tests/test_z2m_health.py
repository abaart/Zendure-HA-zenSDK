import importlib.util
from pathlib import Path
import json
import pytest
import yaml
from jinja2 import Environment
R = Path(__file__).parents[1]
P = yaml.safe_load((R/'Dutch (NL) Integration/packages/z2m_health_nl.yaml').read_text())

@pytest.mark.parametrize('bridge,answer,expected',[('on','on',False),('off','on',True),('on','unavailable',True),('unknown','unknown',True)])
def test_health(bridge,answer,expected):
    env=Environment();env.globals['is_state']=lambda e,s: (bridge if 'bridge' in e else answer)==s
    assert (env.from_string(P['template'][0]['binary_sensor'][0]['state']).render()=='True') == expected

@pytest.mark.parametrize('payload,expected',[({'status':'ok','data':{'healthy':True}},'ON'),({'status':'ok','data':{'healthy':False}},'OFF'),({'status':'error'},'OFF'),({},'OFF')])
def test_response(payload,expected):
    sensor=P['mqtt']['binary_sensor'][0]
    assert Environment().from_string(sensor['value_template']).render(value_json=payload)==expected
    assert sensor['expire_after']==300

spec=importlib.util.spec_from_file_location('recovery', R/'scripts/synology/z2m_usb_recovery.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

def test_usb_match_requires_unique_serial(tmp_path):
    with pytest.raises(RuntimeError):m.find_stick(tmp_path)
    for name in ['a','b']:
        p=tmp_path/name;p.mkdir()
        for key,value in {'idVendor':'10c4','idProduct':'ea60','serial':'0001','busnum':'1','devnum':'2'}.items(): (p/key).write_text(value)
        if name=='a':assert m.find_stick(tmp_path)==(1,2)
    with pytest.raises(RuntimeError):m.find_stick(tmp_path)

def test_mapping_refuses_ambiguity():
    dev='<hostdev type="usb"><source><vendor id="0x10c4"/><product id="0xea60"/></source></hostdev>'
    assert len(m.target_devices('<domain><devices>'+dev+'</devices></domain>'))==1
    with pytest.raises(RuntimeError):m.target_devices('<domain><devices>'+dev*2+'</devices></domain>')
