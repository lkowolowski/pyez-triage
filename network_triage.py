#!/usr/bin/env -S uv run --script
# /// script
# requires-python = "<3.13"
# dependencies = [
# "ansible",
# "asn1crypto",
# "bcrypt",
# "cffi",
# "colorama",
# "cryptography",
# "Jinja2",
# "junos-eznc",
# "lxml",
# "MarkupSafe",
# "ncclient",
# "netaddr",
# "paramiko",
# "pyasn1",
# "pycparser",
# "PyNaCl",
# "pyserial",
# "PyYAML",
# "retrying",
# "scp",
# "selectors2",
# "six",
# ]
# ///

import argparse
import getpass
import json
import os
import re
import sys
from datetime import datetime, timezone
from math import floor, ceil
from pathlib import Path
from validate import validate_bool, validate_choice, validate_int, validate_password, validate_str
from ansible.parsing.dataloader import DataLoader
from ansible.inventory.manager import InventoryManager
from ansible.vars.manager import VariableManager
from colorama import Fore, Style
from jnpr.junos import Device
from jnpr.junos.exception import ConnectError, ProbeError, ConnectAuthError
from jnpr.junos.op.ospf import OspfNeighborTable
from jnpr.junos.op.routes import RouteSummaryTable
from jnpr.junos.op.fpc import FpcInfoTable, FpcHwTable
from jnpr.junos.op.phyport import PhyPortErrorTable
from jnpr.junos.utils.scp import SCP
from myTables.OpTables import (PortFecTable, PhyPortDiagTable, EthMacStatTable, EthPcsStatTable,
    EthPortExtTable, EthPortTable, bgpSummaryTable, bgpTable, OspfInterfaceTable,
    HMCTable)


OLD_MEMORY_VALUES = [0x0090, 0x009a, 0x009b]


def _reached_threshold(actual, threshold):
    oper, val = threshold.split()
    if(eval(actual + oper + val)):
        return True
    return False


def _create_header(name):
    lpad = ceil((89-len(name))/2)
    rpad = floor((89-len(name))/2)
    return f"{'#'*lpad} {name.upper()} {'#'*rpad}"


def _print_if_msg(msg):
    if msg:
        print(msg)


def ints(dev, ifaces=None):

    def print_interface_header():
        if ae:
            print(f"INTERFACE: {eth.name} which is part of ae bundle {ae}")
        else:
            print(f"INTERFACE: {eth.name}")
        if eth.description:
            print(f"    Description: {eth.description}")
        if lldp_print_string:
            print(lldp_print_string)

    def _check_optic(optic, header, print_interface):
        optic_rx_msg = optic_tx_msg = ""
        if(optic.rx_power_low_alarm or optic.rx_power_high_alarm):
            optic_rx_msg = f"{Fore.RED}    **Receiver power is too high or low. Interface possibly off**{Style.RESET_ALL}"
        elif(optic.rx_power_low_warn or optic.rx_power_high_warn):
            optic_rx_msg = f"{Fore.RED}    **Receiver power is marginal. Possible errors**{Style.RESET_ALL}"
        if(optic.bias_current_high_alarm or optic.bias_current_low_alarm or
             optic.bias_current_high_warn or optic.bias_current_low_warn or
             optic.tx_power_high_alarm or optic.tx_power_low_alarm or
             optic.tx_power_high_warn or optic.tx_power_low_warn):
            optic_tx_msg = f"{Fore.RED}    **Transmit Problems. Please check SFP.**{Style.RESET_ALL}"
        if optic_rx_msg or optic_tx_msg:
            if print_interface:
                print_interface_header()
                print_interface = False
                print(f"Admin State: {eth['admin']}    Oper State: {eth['oper']}")
            print(header)
            print(f"        RX Optic Power: {optic.rx_optic_power}    TX Optic Power: {optic.tx_optic_power}")
            print(f"        Module Temp: {phy_optic.module_temperature}    Module Voltage: {phy_optic.module_voltage}")
            _print_if_msg(optic_rx_msg)
            _print_if_msg(optic_tx_msg)
        return print_interface

    def _save_curr_run(hostname, json_dict):
        fname = f"counters/{hostname}_prev_run.json"
        try:
            prevfile = Path(fname)
            if prevfile.exists():
                if prevfile.is_file():
                    os.remove(fname)
                elif prevfile.is_dir():
                    os.rmdir(fname)
            with open(fname, "w") as f:
                json.dump(json_dict, f)
            os.chmod(fname, 0o664)
        except Exception as err:
            print("Unable to save counters")
            print(err.__class__.__name__, err)

    def _get_prev_run(hostname):
        try:
            with open(f"counters/{hostname}_prev_run.json", "r") as f:
                return json.load(f)
        except Exception as err:
            print(f"No existing counters for device {hostname}")
            return None

    try:
        with open("thresholds.json", "r") as f:
            json_thresholds = json.load(f)
    except Exception as err:
        print("JSON load error")
        print("Skipping interface troubleshooting...")
        return

    hostname = dev.facts['hostname']

    json_prev_run = _get_prev_run(hostname)
    json_curr_run = {}

    timestamp = datetime.now(timezone.utc)
    json_curr_run['timestamp'] = str(timestamp)

    optics = PhyPortDiagTable(dev).get()
    phy_errs = PhyPortErrorTable(dev).get()
    fec_errs = PortFecTable(dev).get()
    pcs_stats = EthPcsStatTable(dev).get()
    mac_stats = EthMacStatTable(dev).get()
    eths = EthPortTable(dev).get()
    eth_exts = EthPortExtTable(dev).get()

    print(f"{Fore.YELLOW}{_create_header('begin troubleshoot interfaces')}{Style.RESET_ALL}\n")

    for eth in eths:
        # if user provides an interface group, then we only analyze ifaces in that
        # group
        if ifaces and not eth.name in ifaces:
            continue

        if eth['admin'] == 'down':
            print(f"{Fore.GREEN}{eth.name} is admin down, skipping remaining checks{Style.RESET_ALL}")
            continue

        # Retreive AE info if exists to later print out for user along with Interface
        # name
        logicals = eth_exts[eth.name].logical
        ae = None
        for logical in logicals:
            if logical.address_family_name == "aenet":
                ae = logical.ae_bundle_name

        # Gather LLDP info via RPC calls
        # Support Non-ELS RPC call
        if dev.facts['switch_style'] == 'VLAN':
            lldp = dev.rpc.get_lldp_interface_neighbors_information(interface_name=eth.name)
        # Support ELS RPC call
        else:
            lldp = dev.rpc.get_lldp_interface_neighbors(interface_device=eth.name)

        lldp_print_string = ""
        # Future Warning said to use __len__ method instead of the boolean value
        if len(lldp) > 0:

            lldp_neigh_sys = lldp.xpath('//lldp-remote-system-name')
            if lldp_neigh_sys:
                lldp_neigh_sys = lldp_neigh_sys[0].text
                lldp_print_string = f"    LLDP Neighbor Name: {lldp_neigh_sys}"

            lldp_if_type = lldp.xpath('//lldp-remote-port-id-subtype')
            if lldp_if_type:
                lldp_if_type = lldp_if_type[0].text

            lldp_neigh_if = lldp.xpath('//lldp-remote-port-id')
            if lldp_neigh_if:
                lldp_neigh_if = lldp_neigh_if[0].text
                if lldp_if_type in ('Interface name', 'Locally assigned'):
                    lldp_print_string = lldp_print_string + f"    Remote Iface: {lldp_neigh_if}"

            lldp_neigh_if_desc = lldp.xpath('//lldp-remote-port-description')
            if lldp_neigh_if_desc:
                lldp_neigh_if_desc = lldp_neigh_if_desc[0].text
                if lldp_neigh_if != lldp_neigh_if_desc:
                    lldp_print_string = lldp_print_string + f"    Remote Iface Descr: {lldp_neigh_if_desc}"

        # Initialze empty dict for json structure to be written later. Must
        # initialize each element/sub-element
        json_curr_run[eth.name] = {}

        # Controls when we print the interface header
        print_interface = True

        # Optics related code if interface is an optic
        if eth.name in optics:
            optic = optics[eth.name]
            phy_optic = optic
            if optic.lanes:
                for lane in optic.lanes:
                    # For channelized interfaces
                    if ":" in eth.name:
                        if eth.name[-1] != str(lane.lane_index):
                            continue
                    # Handles QSFPs as well
                    header = f"    Optic Diag Lane# {lane.name}:"
                    print_interface = _check_optic(lane, header, print_interface)
            elif optic.rx_optic_power:
                header = "    Optic Diag:"
                print_interface = _check_optic(optic, header, print_interface)

        # Using the main list of interfaces we use the interface name as the key for
        # each of the tables below This way we can resuse the same thresholds lookup
        # code mechanism in place
        tables = [phy_errs, fec_errs, pcs_stats, mac_stats]
        for table in tables:
            if eth.name in table:
                row = table[eth.name]
                if row.__class__.__name__ == "PortFecView":
                    key = 'fec_errs'
                elif row.__class__.__name__ == "PhyPortErrorView":
                    key = 'phy_errs'
                elif row.__class__.__name__ == "EthPcsStatView":
                    key = 'pcs_stats'
                elif row.__class__.__name__ == "EthMacStatView":
                    key = 'mac_stats'

                # Always make sure key exists and contains a truthy value
                for subkey in json_thresholds[key].keys():
                    if subkey in row.keys() and row[subkey]:
                        if _reached_threshold(str(row[subkey]), str(json_thresholds[key][subkey])):
                            json_curr_run[eth.name][subkey] = row[subkey]
                            if print_interface:
                                print_interface_header()
                                print_interface = False
                            print(f"    {Fore.RED}'{subkey}' threshold is {str(json_thresholds[key][subkey])}"
                                        f" with value of {str(row[subkey])}{Style.RESET_ALL}")

                            # Load values from previous run if available and print
                            # difference to user if any
                            try:
                                diff = row[subkey] - json_prev_run[eth.name][subkey]
                                prevtimestamp = datetime.strptime(json_prev_run['timestamp'], '%Y-%m-%d %H:%M:%S.%f')
                                timediff = timestamp - prevtimestamp
                                seconds = timediff.total_seconds()
                                if diff != 0:
                                    print(f"         {Fore.MAGENTA}previous value was {str(json_prev_run[eth.name][subkey])}"
                                                f" which is a difference of {str(diff)} from the last run {round(seconds,2):0.2f}s ago"
                                                f" or about {round(diff/seconds,2):0.2f}/second"
                                                f"{Style.RESET_ALL}")
                            except Exception:
                                pass

        # Delete Interface from json struct if no thresholds were violated (Remove
        # empty dict)
        if not json_curr_run[eth.name]:
            del json_curr_run[eth.name]
    _save_curr_run(hostname, json_curr_run)
    print(f"{Fore.YELLOW}{_create_header('end of troubleshoot interfaces')}{Style.RESET_ALL}\n")


def bgp(dev):
    print(f"{Fore.YELLOW}{_create_header('begin troubleshoot bgp')}{Style.RESET_ALL}\n")
    neighbors = bgpTable(dev).get()
    neighsumm = bgpSummaryTable(dev).get()
    for neighbor in neighbors:
        peer_address = neighbor.peer_address.split("+")[0]
        peer_state = neighbor.peer_state
        if peer_state == "Established":
            print(f"Local ID: {neighbor.local_id:15} Local AS: {neighbor.local_as:7} "
                        f"Local Address: {neighbor.local_address}\nPeer    ID: {neighbor.peer_id:15} "
                        f"Peer    AS: {neighbor.peer_as:7} Peer Address: {neighbor.peer_address:17}\n"
                        f"Num Routes Received: {neighbor.route_received} Local Interface: {neighbor.local_interface}\n"
                        f"Elapsed Time(secs): {neighsumm[peer_address].elapsed_time_secs}\n")
        elif peer_state == "Active":
            print(f"{Fore.RED}Neighbor {neighbor.peer_address} in active state, check configuration{Style.RESET_ALL}")
        elif peer_state == "Connect":
            print(f"{Fore.RED}Neighbor {neighbor.peer_address} in connect state, check protocol configuration"
                        f"{Style.RESET_ALL}")
        elif peer_state == "Idle":
            print(f"{Fore.RED}Neighbor {neighbor.peer_address} in idle state, check reachability{Style.RESET_ALL}")
        else:
            print(f"{Fore.RED}Unxpected state of {peer_state}. Neighbor {neighbor.peer_address} may be in transition,"
                f"rerun command in a few seconds{Style.RESET_ALL}")

    print(f"{Fore.YELLOW}{_create_header('end of troubleshoot bgp')}{Style.RESET_ALL}\n")


def ospf(dev, instance=None):
    print(f"{Fore.YELLOW}{_create_header('begin troubleshoot ospf')}{Style.RESET_ALL}\n")
    if instance:
        neighbors = OspfNeighborTable(dev).get(instance=instance)
        interfaces = OspfInterfaceTable(dev).get(instance=instance)
    else:
        neighbors = OspfNeighborTable(dev).get()
        interfaces = OspfInterfaceTable(dev).get()
    for interface in interfaces:
        if interface.passive:
            passive = 'yes'
        else:
            passive = 'no'
        print(f"Interface: {interface.interface_name:21} Neighbor Count: {interface.neighbor_count}\n"
            f"    Passive: {passive}"
                 )
        print("    Neighbors:")
        for neighbor in neighbors:
            if interface.interface_name == neighbor.interface_name:
                if neighbor.ospf_neighbor_state != "Full":
                    print(f"        {Fore.RED}{neighbor.neighbor_address:15} Uptime: {str(neighbor.neighbor_up_time):15}"
                        f"Neighbor state: {neighbor.ospf_neighbor_state}{Style.RESET_ALL}")
                else:
                    print(f"        {neighbor.neighbor_address:15} Uptime: {neighbor.neighbor_up_time}")
    routes = RouteSummaryTable(dev).get()
    total_routes = 0
    for route in routes:
        if route.proto['OSPF']:
            print(f"Table: {route.name} routes:{route.proto['OSPF'].count} active:{route.proto['OSPF'].active}")
            total_routes = total_routes + route.proto['OSPF'].count
    print(f"Total OSPF Routes: {total_routes}")
    print(f"{Fore.YELLOW}{_create_header('end of troubleshoot ospf')}{Style.RESET_ALL}\n")


def logs(dev):
    print(f"{Fore.YELLOW}{_create_header('begin parse syslog')}{Style.RESET_ALL}\n")

    ntp_issue = False
    license_issue = False
    fname = f"{dev.hostname}-messages"

    print("Transferring /var/log/messages from device")
    with SCP(dev, progress=True) as scp1:
        scp1.get("/var/log/messages", local_path=fname)
    with open(fname) as messages:
        lines = messages.readlines()
        ntp_color = license_color = "Fore.RESET"
        for line in lines:
            if "NTP" in line and "Unreachable" in line:
                ntp_issue = True
                ntp_color = "Fore.RED"
            elif "License" in line:
                license_issue = True
                license_color = "Fore.RED"
    try:
        os.remove(fname)
    except Exception as err:
        print(f"Unable to delete old log file for {fname}")
        print(err.__class__.__name__, err)

    print(f"{eval(ntp_color)}ntp_issue: {ntp_issue}{Style.RESET_ALL}, {eval(license_color)}license_issue: "
                f"{license_issue}{Style.RESET_ALL}\n")
    print(f"{Fore.YELLOW}{_create_header('end of parse syslog')}{Style.RESET_ALL}\n")


def info(dev):
    print(f"{Fore.YELLOW}{_create_header('begin get info (device facts)')}{Style.RESET_ALL}\n")
    print(f"Hostname: {dev.facts['hostname']:21} Version:        {dev.facts['version']}\n"
                f"Model:        {dev.facts['model']:21} Chassis SN: {dev.facts['serialnumber']}")
    try:
        print(f"RE0 Uptime: {dev.facts['RE0']['up_time']}")
        if dev.facts['2RE']:
            print(f"    RE1 Uptime: {dev.facts['RE1']['up_time']}")
    except:
        print(f"RE1 Uptime: {dev.facts['RE1']['up_time']}")

    # Process FPC states
    fpcs = FpcInfoTable(dev).get()
    states = {}
    offline_fpcs = []
    for fpc in fpcs:
        if fpc['state'] in states:
            states[fpc['state']] = states[fpc['state']] + 1
        else:
            states[fpc['state']] = 1
        if fpc['state'] != "Online" and fpc['state'] != "Empty":
            offline_fpcs.append(fpc.name)
    print(f"FPC status: {dict(sorted(states.items(), reverse=True))}")
    if offline_fpcs:
        print(f"{Fore.RED}FPCs not online: {offline_fpcs}{Style.RESET_ALL}")

    # Check for old fpc memory
    found_old_memory_chassis = False
    fpc_hw_info = FpcHwTable(dev).get()
    for fpc in fpcs:
        found_old_memory_fpc = False
        if fpc['state'] == "Empty":
            continue
        slot = fpc.name
        fpc_hw = fpc_hw_info[f"FPC {slot}"]
        target = f"fpc{slot}"
        hmc_data = HMCTable(dev).get(target=target)
        for k,v in hmc_data:
            if int(v['fw_set'],16) in OLD_MEMORY_VALUES:
                if not found_old_memory_fpc:
                    found_old_memory_fpc = True
                    found_old_memory_chassis = True
                    print(f"{Fore.RED}fpc {slot}, sn: {fpc_hw['sn']} has old memory{Style.RESET_ALL}")
                print(f"{Fore.RED}        id: {v['id']:3} name: {v['name']} fw_set: {v['fw_set']} "
                            f"prod_rev: {v['rev']} num: {v['num']}{Style.RESET_ALL}")
    if not found_old_memory_chassis:
        print("No old memory found for this chassis")

    print(f"{Fore.YELLOW}{_create_header('end of get info (device facts)')}{Style.RESET_ALL}\n")


def pem(dev):
    print(f"{Fore.YELLOW}{_create_header('begin check pem health')}{Style.RESET_ALL}\n")
    pem_info = dev.rpc.get_environment_pem_information()
    pem_names = pem_info.xpath('//name')
    pem_statuses = pem_info.xpath('//state')
    for idx, pem_name in enumerate(pem_names):
        pem_status = pem_statuses[idx].text
        pem_name = pem_name.text
        if pem_status != 'Online':
            print(f"{Fore.RED}Device {dev.hostname} has a problem with {pem_name}. Status is {pem_status}.{Style.RESET_ALL}")
    print(f"{Fore.YELLOW}{_create_header('end of check pem health')}{Style.RESET_ALL}\n")


def alarms(dev):
    print(f"{Fore.YELLOW}{_create_header('begin alarm check')}{Style.RESET_ALL}\n")
    system_alarms = dev.rpc.get_system_alarm_information()
    chassis_alarms = dev.rpc.get_alarm_information()
    print("SYSTEM ALARMS:")
    for alarm in system_alarms.xpath('//alarm-description'):
        print(alarm.text)
    print("\nCHASSIS ALARMS:")
    for alarm in chassis_alarms.xpath('//alarm-description'):
        print(alarm.text)
    print(f"{Fore.YELLOW}{_create_header('end of alarm check')}{Style.RESET_ALL}\n")


def junos_cmd(dev, cmd):
    print(f"{Fore.YELLOW}{_create_header('begin execute junos command')}{Style.RESET_ALL}\n")
    print(dev.cli(cmd, warning=False))
    print(f"{Fore.YELLOW}{_create_header('end of execute junos command')}{Style.RESET_ALL}\n")


def main():
    oper_choices = ["all", "ints", "bgp", "ospf", "logs", "info", "pem", "alarms", "junos_cmd"]
    parser = argparse.ArgumentParser(description='Execute troubleshooting operation(s)')
    parser.add_argument('-o', '--oper', dest='operations', metavar='<oper>',
                        choices=oper_choices, nargs='+',
                        help='select operation(s) to run from list')
    parser.add_argument('-u', '--user', dest='user', metavar='<username>',
                        help='provide username for ssh login to devices')
    parser.add_argument('-p', '--pass', dest='passwd', metavar='<password>',
                        help='provide ssh password or passphrase')
    parser.add_argument('-n', '--nopass', action='store_true',
                        help='disable password prompting')
    parser.add_argument('-c', '--config', dest='ssh_config', metavar='<ssh_config>', default='',
                        help='provide ssh config path')
    parser.add_argument('-i', '--inventory', dest='inventory_path', metavar='<inventory_path>',
                        help='provide ansible inventory path')
    parser.add_argument('-l', '--limit', dest='limit', metavar='<limit>',
                        help='specify host or group to run operations on')
    parser.add_argument('-f', '--iface', dest='iface', metavar='<limit>',
                        help='specify host or group to run operations on')
    parser.add_argument('-q', '--quiet', action='store_true',
                        help='disable optional interactive prompts')
    parser.add_argument('-j', '--junos_cmd', dest='cmd', metavar='<junos cmd>',
                        help='junos cli cmd to run')
    parser.add_argument('-r', '--instance', dest='instance', metavar='<routing-instance>',
                        help='specify routing instance for ospf')
    args = parser.parse_args()

    print(f"{Fore.YELLOW}Welcome to the Python troubleshooting script for Junos boxes using PyEZ{Style.RESET_ALL}")
    if (not args.user and not args.inventory_path and not args.operations and not args.quiet and
            validate_bool("Would you like to print the command line help? (y/n) "
                          "(type n to continue in interactive mode) ")):
        parser.print_help()
        sys.exit(0)

    if not args.user:
        user = validate_str("Enter your username: ")
    else:
        user = args.user

    if args.passwd:
        passwd = args.passwd
    elif not args.nopass:
        tries = 0
        while tries < 5:
            passwd = getpass.getpass("Enter your password: ").strip()
            if passwd:
                passwd_confirm = getpass.getpass("Confirm your password: ").strip()
                if passwd == passwd_confirm:
                    break
                print("Passwords do not match, please try again...")
            else:
                print("Password cannot be blank, please try again...")
            tries += 1
        if tries == 5:
            print("Reached maximum attempts to validate password, quitting...")
            sys.exit(1)
    else:
        passwd = None

    if not args.inventory_path:
        inventory_dir = Path("inventory")
        inventory_choices = [x for x in inventory_dir.iterdir() if x.is_dir()]
        inventory_choices.sort()
        print("\nAvailable Datacenters:")
        for idx, choice in enumerate(inventory_choices):
            print(f"{idx+1}: {choice.name}")
        user_choice = validate_int("\nSelect Datacenter (Type Number only and press Enter):", input_min=1,
            input_max=len(inventory_choices))
        choice = inventory_choices[user_choice - 1]
        datacenter = choice.as_posix()
        print(f"Datacenter {choice.name} selected")
    else:
        datacenter = args.inventory_path
    # Ensure inventory path exists. Safeguard mainly when user provides path via cmd
    # line
    if not Path(datacenter).exists():
        print(f"Inventory Path '{datacenter}' does not exist. quitting...")
        sys.exit(1)

    if (not args.limit and not args.quiet and
           validate_bool("Do you want to limit the execution to a specific set of hosts or groups? (y/n) ")):
        limit = validate_str("Wildcard matching is supported like * and ? or [1-6] or [a:d] "
           "i.e. qfx5?00-[a:d] or qfx5100*\nEnter your limit: ")
    elif args.limit:
        limit = args.limit
    else:
        limit = None

    if (not args.iface and not args.quiet and
        validate_bool("Do you want to specify an interface group? (y/n) ")):
        iface_group = validate_str("Enter name of interface group you'd like to troubleshoot: ")
    elif args.iface:
        iface_group = args.iface
    else:
        iface_group = None

    if not args.operations:
        operations = []
        while True:
            if oper_choices:
                print("Operations available to run:")
            else:
                break
            for idx, choice in enumerate(oper_choices):
                print(f"{idx+1}: {choice}")
            choice = validate_int("Select operation:", input_min=1, input_max=oper_choices.__len__())
            operation = oper_choices[choice - 1]
            if operation == 'all':
                oper_choices.remove('all')
                operations.extend(oper_choices)
                break
            operations.append(operation)
            oper_choices.remove(operation)
            print(f"Operation(s) {operation} selected")
            if not validate_bool("Would you like to select another operation? "):
                break
        print(f"List of operations selected are {operations}")
    elif args.operations == ['all']:
        operations = list(oper_choices)
        operations.remove('all')
    else:
        operations = args.operations

    if 'junos_cmd' in operations:
        if not args.cmd:
            cmd = validate_str("Enter Junos CLI command to be executed: ")
        else:
            cmd = args.cmd

    if (not args.instance and not args.quiet and 'ospf' in operations and
                    validate_bool("Do you want to specify a routing instance? (y/n) ")):
        instance = validate_str("Enter name of routing instance: ")
    elif args.instance:
        instance = args.instance
    else:
        instance = None

    loader = DataLoader()
    inventory = InventoryManager(loader=loader, sources=datacenter)
    variables = VariableManager(loader=loader, inventory=inventory)
    success = 0
    skipped = 0
    failure = 0
    skipped_hosts = []
    failed_hosts = []
    if limit:
        if "*" in limit:
            limit = limit.replace("*", ".*")
        if "?" in limit:
            limit = limit.replace("?", ".")
        if ":" in limit:
            limit = limit.replace(":", "-")
    for host in inventory.get_hosts():
        hostname = host.get_name()
        match = False
        if limit:
            if re.match(limit, hostname):
                match = True
            else:
                for group in (str(g) for g in host.get_groups()):
                    if re.match(limit, group):
                        match = True
            if not match:
                continue

        netconf_port = variables.get_vars(host=host)['netconf_port']

        # Begin Device Output to User
        print(f"{Fore.BLUE}{Style.BRIGHT}Conducting triage of device {hostname}{Style.RESET_ALL}")
        ifaces = []
        if iface_group:
            try:
                # create list of interfaces from list of dicts for given interface
                # group
                for iface in variables.get_vars(host=host)[iface_group]:
                    for k, v in iface.items():
                        ifaces.append(v)
            except KeyError as err:
                print(f"No Interfaces found in group '{iface_group}' for host '{hostname}'")
                skipped = skipped + 1
                skipped_hosts.append(hostname)
                continue
            except Exception as err:
                print(f"{Fore.RED}Abnormal termination: {err.__class__.__name__, err}{Style.RESET_ALL}")
                sys.exit(1)
                ifaces = None

        # Begin Netconf comms with Device and execute list of operations
        try:
            with Device(host=hostname, port=netconf_port, user=user,
                        passwd=passwd, ssh_config=args.ssh_config,
                        auto_probe=5) as dev:
                for operation in operations:
                    if callable(globals()[operation]) and not operation.startswith('_'):
                        if operation == 'ints' and ifaces:
                            globals()[operation](dev, ifaces=ifaces)
                        elif operation == 'ospf' and instance:
                            globals()[operation](dev, instance=instance)
                        elif operation == 'junos_cmd':
                            globals()[operation](dev, cmd=cmd)
                        else:
                            globals()[operation](dev)
                    else:
                        print(f"{Fore.RED}Invalid operation: '{operation}'\nProblem with code. Make sure oper_choices "
                            f"matches the public(no leading underscore) function names{Style.RESET_ALL}")
                        sys.exit(2)
            success = success + 1
        except ConnectAuthError as err:
            print(f"{Fore.RED}Unable to login. Check username/password: {err}")
            print(f"Exiting so you don't lock yourself out :){Style.RESET_ALL}")
            sys.exit(1)
        except (ProbeError, ConnectError) as err:
            print(f"{Fore.RED}Cannot connect to device: {err}\nMake sure device is reachable and {Style.BRIGHT}"
                f"'set system services netconf ssh'{Style.NORMAL} is set{Style.RESET_ALL}")
            failure = failure + 1
            failed_hosts.append(hostname)
        except Exception as err:
            print(f"{Fore.RED}Abnormal termination: {err.__class__.__name__, err}{Style.RESET_ALL}")
            sys.exit(1)

    # print out summary messages at the end
    if success > 0:
        print(f"{Fore.GREEN}Successfully connected to: {success} device(s){Style.RESET_ALL}")
    if skipped > 0:
        print(f"{Fore.YELLOW}Skipped {skipped} device(s)\nSkipped Hosts: "
                    f"{skipped_hosts}{Style.RESET_ALL}")
    if failure > 0:
        print(f"{Fore.RED}Failed to connect to {failure} device(s)\nFailed Hosts: {failed_hosts}{Style.RESET_ALL}")
    if not success and not skipped and not failure:
        if limit:
            print(f"{Fore.RED}No Hosts/Groups matched limit '{limit}' in Inventory Path '{datacenter}'"
                        f"{Style.RESET_ALL}")
        else:
            print(f"{Fore.RED}No Hosts/Groups found in Inventory Path '{datacenter}'{Style.RESET_ALL}")


if __name__ == "__main__":
    main()
