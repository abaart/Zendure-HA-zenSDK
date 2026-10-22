#!/usr/bin/env python3
"""Inspect Sonoff passthrough; --apply explicitly permits one live remap per boot."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import xml.etree.ElementTree as ET

VM = 'd67a096e-bde8-432b-aa97-2af7d5b69349'

def find_stick(root=Path('/sys/bus/usb/devices')):
    matches = []
    for path in root.iterdir():
        try:
            if ((path/'idVendor').read_text().strip(), (path/'idProduct').read_text().strip(),
                (path/'serial').read_text().strip()) == ('10c4', 'ea60', '0001'):
                matches.append((int((path/'busnum').read_text()), int((path/'devnum').read_text())))
        except (OSError, ValueError):
            continue
    if len(matches) != 1:
        raise RuntimeError('Expected exactly one Sonoff 10c4:ea60 serial 0001; found %s' % len(matches))
    return matches[0]

def target_devices(xml):
    root = ET.fromstring(xml)
    result = []
    for dev in root.findall('./devices/hostdev'):
        vendor, product = dev.find('./source/vendor'), dev.find('./source/product')
        if dev.get('type') == 'usb' and vendor is not None and product is not None:
            if int(vendor.get('id'), 16) == 0x10c4 and int(product.get('id'), 16) == 0xea60:
                result.append(dev)
    if len(result) > 1:
        raise RuntimeError('Multiple Sonoff mappings; refusing to change VM')
    return result

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.apply and os.geteuid() != 0:
        parser.error('--apply requires root')
    prefix = ['/usr/local/bin/virsh'] + ([] if args.apply else ['-r']) + ['-c', 'qemu:///system']
    def virsh(*cmd):
        return subprocess.run(prefix + list(cmd), check=True, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=30).stdout
    if virsh('domstate', VM).strip() != 'running':
        raise RuntimeError('HA VM is not running; try again after VMM has started it')
    bus, device = find_stick()
    mappings = target_devices(virsh('dumpxml', VM))
    print(json.dumps({'vm':VM, 'usb_bus':bus, 'usb_device':device,
                      'mappings':len(mappings), 'mode':'apply' if args.apply else 'inspect'}))
    if not args.apply:
        return
    # Directory creation is an atomic lock and a per-boot success guard.
    boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    marker = Path('/tmp/z2m-usb-remapped-' + boot_id)
    marker.mkdir(mode=0o700)
    success = False
    try:
        with tempfile.TemporaryDirectory(prefix='z2m-usb-') as temp:
            old = Path(temp)/'old.xml'
            new = Path(temp)/'new.xml'
            new.write_text('<hostdev mode="subsystem" type="usb" managed="no"><source>'
                           '<vendor id="0x10c4"/><product id="0xea60"/>'
                           '<address bus="%s" device="%s"/></source></hostdev>' % (bus, device))
            if mappings:
                old.write_text(ET.tostring(mappings[0], encoding='unicode'))
                virsh('detach-device', VM, str(old), '--live')
            try:
                virsh('attach-device', VM, str(new), '--live')
            except subprocess.SubprocessError:
                if mappings:
                    virsh('attach-device', VM, str(old), '--live')
                raise
            mapped = target_devices(virsh('dumpxml', VM))
            address = mapped[0].find('./source/address') if len(mapped) == 1 else None
            if address is None or (int(address.get('bus')), int(address.get('device'))) != (bus, device):
                raise RuntimeError('USB mapping readback does not match the current device')
            success = True
            print('USB mapping restored. Check HA serial device and Z2M health response.')
    finally:
        if not success:
            print('Recovery failed; per-boot marker retained to prevent repeated remapping.')

if __name__ == '__main__':
    main()
