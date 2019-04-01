#!/usr/bin/env python3

import collections
from functools import reduce
import hashlib
import logging
import struct
import sys
import textwrap

class Image(object):
    def check_unknown_image(self):
        known_hashes = [
            #"121fafdef3328cde2c3bdee1b3977863175a902474870c35a31e4b45369a6998"
            "121fafdef3328cde2c3bdee1b3977863175a902474870c35a31e4b45369a699"
        ]

        digest = hashlib.sha256(self.image).hexdigest()

        if digest not in known_hashes:
            print(textwrap.dedent("""\
                asdasdad
                osdgposopsdjg
                sdjflksdfjksdjf
                """ % digest))


    def __init__(self, path):
        self.ops = []

        with open(path, 'rb') as file:
            self.image = bytearray(file.read())

        self.check_unknown_image()

        if len(self.image) >= 0x105 and self.image[0x104] == 0xa5:
            logging.info("Firmware marker found using late data offset")
            self.data_offset = 0x5000
        else:
            logging.warning('No firmware marker found, assuming data starts at 0. This is untested.')
            self.data_offset = 0

        if self.data_offset + 2 >= len(self.image):
            raise Exception('Data offset (0x%x) points past end of image' % self.data_offset)

        header = self.image[self.data_offset] | (self.image[self.data_offset+1] << 8)
        if (header >> 11 != 0x15):
            raise Exception("Invalid magic found at offset 0x%x" % self.data_offset)

        self.entries = header & 0x3ff
        self.speedmode = (header >> 10) & 1
        logging.info('Found valid data header with %d entries and speed mode %d', self.entries, self.speedmode)

    def clear(self):
        self.ops = []

    Operation = collections.namedtuple('Operation', ['page', 'addr', 'val', 'size'])
    def set(self, page, addr, val, size):
        logging.debug('setting 0x%02x / 0x%02x to 0x%x (%d)', page, addr, val, size)
        self.ops.append(Image.Operation(page, addr, val, size))

    def set8(self, page, addr, val):
        self.set(page, addr, val, 8)

    def set16(self, page, addr, val):
        self.set(page, addr, val, 16)

    def set32(self, page, addr, val):
        self.set(page, addr, val, 32)

    def set64(self, page, addr, val):
        self.set(page, addr, val, 64)

    def save(self, path):
        img = bytearray(self.image)
        new_data = bytearray()

        # This causes us to always set the page for the first write. While it
        # might not be necessary, it saves us from having to parse the existing
        # data
        cur_page = None

        for op in self.ops:
            if cur_page != op.page:
                new_data.extend(struct.pack('<BBH', 1, 0xff, op.page))
                cur_page = op.page

            if op.size == 8 or op.size == 16:
                new_data.extend(struct.pack('<BBH', 1, op.addr, op.val))
            elif op.size == 32:
                new_data.extend(struct.pack('<BBI', 2, op.addr, op.val))
            elif op.size == 64:
                new_data.extend(struct.pack('<BBQ', 4, op.addr, op.val))

        logging.debug('appending %d bytes of new data', len(new_data))

        new_header = (0x15 << 11) | (self.speedmode << 10) | int(self.entries + len(new_data)/2)
        img[self.data_offset:self.data_offset+2] = struct.pack('H', new_header)

        offset = self.data_offset + 2 + self.entries*2
        img[offset:offset+len(new_data)] = new_data

        with open(path, 'wb') as f:
            f.write(img)

class Setter(object):
    def __init__(self, page, addr, size):
        self.page = page
        self.addr = addr
        self.size = size

    def set(self, img, val):
        img.set(self.page, self.addr, val, self.size)

class Page(type):
    def __getattribute__(self, name):
        if name[0] == '_':
            return type.__getattribute__(self, name[1:])

        reg = type.__getattribute__(self, name)
        return Setter(type.__getattribute__(self, 'page'), reg[0], reg[1])

class JumboControl(metaclass=Page):
    page = 0x40

    PortMask = (0x01, 32)

class Control(metaclass=Page):
    page = 0x00

    SwitchMode = (0x0b, 8)

class IEEEVlan(metaclass=Page):
    page = 0x34

    GlobalControl = (0x00,  8)
    VlanControl1  = (0x01,  8)
    VlanControl2  = (0x02,  8)
    VlanControl3  = (0x03, 16)
    VlanControl4  = (0x05,  8)
    VlanControl5  = (0x06,  8)

    DefaultTag = (0x10, 16)

class TableAccess(metaclass=Page):
    page = 0x05

    VlanTableControl = (0x80, 8)
    VlanTableIndex = (0x81, 16)
    VlanTableEntry = (0x83, 32)

def enable_vlan(img):
    # disable jumbo frames (XXX: why do we need this?)
    JumboControl.PortMask.set(img, 0x00)

    Control.SwitchMode.set(img, 0x06)

    IEEEVlan.GlobalControl.set(img, 0xe3)
    IEEEVlan.VlanControl1.set(img, 0x0e)
    IEEEVlan.VlanControl4.set(img, 0x40)
    IEEEVlan.VlanControl5.set(img, 0x18)

def set_default_vlan(img, port, vid, qos=0):
    img.set16(IEEEVlan._page, IEEEVlan._DefaultTag[0] + port*2,
              (qos<<13) | vid)

def configure_vlan(img, vid, members, untagged):
    TableAccess.VlanTableIndex.set(img, vid)
    TableAccess.VlanTableEntry.set(img, (untagged << 9) | members)
    TableAccess.VlanTableControl.set(img, 0x80)

def block_untagged(img, ports):
    IEEEVlan.VlanControl3.set(img, ports)

def dump_port2vlan(ports):
    print("Port to VLAN mapping:")
    print("+---+-------+------------------------------------------------------------------+")
    print("| P | UNTAG | TAG                                                              |")
    print("+---+-------+------------------------------------------------------------------+")
    for port in sorted(ports.keys()):
        if 'default_vlan' in ports[port]:
            untag = "% 4d" % ports[port]['default_vlan']
        else:
            untag = "     "

        print("| %d | %s | %s" % (port, untag, ' '.join(map(lambda x: "% 4d" % x, sorted(ports[port]['vlans'])))))

def print_vlan_table(members, default_vlan):
    print("+-----+-------+%s" % ('-------+' * len(members)))
    print("| Port \ VLAN | %s |" % ' | '.join(map(lambda x: " %4d" % x, sorted(members.keys()))))
    print("+-------+-----+%s" % ('-------+' * len(members)))

    for port in range(1, 9):
        vmap = ''
        for vlan in sorted(members.keys()):
            char = ' '
            if port in members[vlan]:
                char = 't'
            if port in default_vlan and default_vlan[port] == vlan:
                char = '*'

            vmap += '   %c   |' % char

        print("|      %d      |%s" % (port, vmap))

    print("+-------------+%s" % ('-------+' * len(members)))
    print("(t) tagged, (*) untagged")

def parse_config(path):
    vlan_members = {}
    default_vlan = {}

    with open(path, 'r') as cfg:
        for lineno, line in enumerate(cfg, 1):
            line = line.strip()

            if not line or line.startswith('#'):
                continue

            try:
                (port, vlan_set) = line.split(':')
            except ValueError:
                logging.error("colon missing in line %d", lineno)
                return None

            port = int(port)
            port_tagged_vlans = set()
            if not 1 <= port <= 8:
                logging.error("port number %d out of range in line %d", port, lineno)
                return None

            for vlan in vlan_set.split():
                tagged = vlan.endswith('t')
                if tagged:
                    vlan = vlan[0:len(vlan)-1]

                vlan = int(vlan)

                # VLAN 4095 is reserved as per the datasheeet
                if not 0 <= vlan <= 4094:
                    logging.error("invalid vlan id %d in line %d", vlan, lineno)
                    return None

                if not tagged:
                    if port in default_vlan:
                        logging.error("port %d has more than one untagged vlan", port)
                        return None
                    else:
                        default_vlan[port] = vlan
                else:
                    port_tagged_vlans.add(vlan)

                if vlan not in vlan_members:
                    vlan_members[vlan] = set()

                vlan_members[vlan].add(port)

            if port in default_vlan and default_vlan[port] in port_tagged_vlans:
                logging.error("untagged vlan %d also present as tagged vlan on port %d", default_vlan[port], port)
                return None

    print("Generated VLAN table:")
    print_vlan_table(vlan_members, default_vlan)
    print("")

    return (vlan_members, default_vlan)

def members_to_bitmask(s):
    return reduce(lambda x, y: x | (1<<(y-1)), s, 0)

def apply_config(img, cfg):
    (members, default_vlan) = cfg
    untagged = {}
    untag_blocked = 0

    for port in range(1, 9):
        if port in default_vlan:
            vid = default_vlan[port]
            set_default_vlan(img, port-1, vid)

            untagged[vid] = untagged.get(vid, 0) | (1 << (port-1))
        else:
            # We can leave the default VLAN config as is, as it applies only to
            # incoming untagged frames and we'll just block all untagged frames
            # on this port

            untag_blocked |= 1 << (port-1)

    block_untagged(img, untag_blocked)

    for vid in members:
        configure_vlan(img, vid, members_to_bitmask(members[vid]), untagged.get(vid, 0))

def main():
    if len(sys.argv) != 4:
        print("usage: %s input.img vlan.cfg output.img" % sys.argv[0])
        sys.exit(1)

    logging.basicConfig(level=logging.WARN)

    vlan = parse_config(sys.argv[2])
    if vlan is None:
        sys.exit(1)

    print("Applying config")
    img = Image(sys.argv[1])
    enable_vlan(img)
    apply_config(img, vlan)
    img.save(sys.argv[3])
    print("Done")

if __name__ == "__main__":
    main()
