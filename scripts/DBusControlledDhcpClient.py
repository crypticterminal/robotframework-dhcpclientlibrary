#!/usr/bin/python
# -*- coding: utf-8 -*-

from __future__ import print_function

import sys
import os
import signal

import gobject
import dbus
import dbus.service
import dbus.mainloop.glib

import argparse

import subprocess

import random

import MacAddr

import threading
import time

import atexit
import lockfile

from pydhcplib.dhcp_packet import *
from pydhcplib.dhcp_network import *

import rfdhcpclientlib.DhcpLeaseStatus

#import pyiface	# Commented-out... for now we are using the system's userspace tools (ifconfig, route etc...)

progname = os.path.basename(sys.argv[0])

main_lock = None	# FileLock object used to force only one DHCP client instance on a given network interface
client = None	# Global instance of DHCP client

VERSION = '1.0.0'

# DHCP types names array (index is the DHCP type)
DHCP_TYPES = ['UNKNOWN',
	'DISCOVER', # 1
	'OFFER', # 2
	'REQUEST', # 3
	'DECLINE', # 4
	'ACK', # 5
	'NACK', # 6
	'RELEASE', # 7
	'INFORM', # 8
]

DBUS_NAME = 'com.legrandelectric.RobotFrameworkIPC.DhcpClientLibrary'	# The name of bus we are creating in D-Bus
DBUS_OBJECT_ROOT = '/com/legrandelectric/RobotFrameworkIPC/DhcpClientLibrary'	# The root under which we will create a D-Bus object with the name of the network interface for D-Bus communication, eg: /com/legrandelectric/RobotFrameworkIPC/DhcpClientLibrary/eth0 for an instance running on eth0
DBUS_SERVICE_INTERFACE = 'com.legrandelectric.RobotFrameworkIPC.DhcpClientLibrary'	# The name of the D-Bus service under which we will perform input/output on D-Bus

CLIENT_ID_HWTYPE_ETHER = 0x01	# HWTYPE byte as used in the client_identifier DHCP option

def dhcpNameToType(name, exception_on_unknown = True):
	"""
	Find a DHCP type (integer), given its name (case insentive)
	If exception_on_unknown is set to False, this function will return 0 (UNKNOWN) if not found
	Otherwise, it will raise UnknownDhcpType
	"""
	name = name.upper()
	for index, item in enumerate(DHCP_TYPES):
		if item == name:
			return index
	if exception_on_unknown:
		raise Exception('UnknownDhcpType')
	else:
		return 0
	
def dhcpTypeToName(type, exception_on_unknown = True):
	"""
	Find a DHCP name (string in uppercase), given its type (integer)
	If exception_on_unknown is set to False, this function will return 'UNKNOWN' if not found
	Otherwise, it will raise UnknownDhcpType
	"""
	
	try:
		return DHCP_TYPES[type].upper()
	except:
		if exception_on_unknown:
			raise
		else:
			return 'UNKNOWN'


def cleanupAtExit():
    """
    Called when this program is terminated, to release the lock
    """
    
    global main_lock
    
    if main_lock and main_lock.i_am_locking():
		#print(progname + ': Releasing lock file', file=sys.stderr)
		main_lock.release()
		main_lock = None

def signalHandler(signum, frame):
	"""
	Called when receiving a UNIX signal
	Will only terminate if receiving a SIGINT or SIGTERM, otherwise, just ignore the signal
	"""
	global client
	
	if signum == signal.SIGINT or signum == signal.SIGTERM:
		cleanupAtExit()
		if not client is None:
			#print(progname + ': Got signal ' + str(signum) + '. We have a client object instance to stop... doing it now', file=sys.stderr)
			client.exit()
			client = None
	else:
		#print(progname + ': Ignoring signal ' + str(signum), file=sys.stderr)
		pass

class DBusControlledDhcpClient(DhcpClient, dbus.service.Object):
    def __init__(self, conn, dbus_loop, object_name=DBUS_OBJECT_ROOT, ifname = None, listen_address = '0.0.0.0', client_port = 68, server_port = 67, mac_addr = None, apply_ip = False, dump_packets = False, silent_mode = True, **kwargs):
        """
        Instanciate a new DBusControlledDhcpClient client bound to ifname (if specified) or a specific interface address listen_address (if specified)
        Client listening UDP port and server destination UDP port can also be overridden from their default values
        """
        
        # Note: **kwargs is here to make this contructor more generic (it will however force args to be named, but this is anyway good practice) and is a step towards efficient mutliple-inheritance with Python new-style-classes
        DhcpClient.__init__(self, ifname = ifname, listen_address = listen_address, client_listen_port = client_port, server_listen_port = server_port)
        if not ifname is None:
            object_name += '/' + str(ifname)    # Add /eth0 to object PATH if ifname is 'eth0'
        dbus.service.Object.__init__(self, conn, object_name)
        
        if ifname:
            self.BindToDevice()
        if listen_address != '0.0.0.0' and listen_address != '::':    # 0.0.0.0 and :: are addresses any in IPv4 and IPv6 respectively
            self.BindToAddress()
        
        self._ifname = ifname
        self._listen_address = listen_address
        self._client_port = client_port
        self._server_port = server_port
        self._silent_mode = silent_mode
        
        self._dhcp_status = rfdhcpclientlib.DhcpLeaseStatus.DhcpLeaseStatus()
        
        self._request_sent = False
        
        self._parameter_list = None    # DHCP Parameter request list (options requested from the DHCP server)
        
        self._random = random.Random()
        self._random.seed()

        self._renew_thread = None
        self._release_thread = None
        
        self._dbus_loop = dbus_loop

        self._dbus_loop_thread = threading.Thread(target = self._loopHandleDbus)    # Start handling D-Bus messages in a background thread.
        self._dbus_loop_thread.setDaemon(True)    # dbus loop should be forced to terminate when main program exits
        self._dbus_loop_thread.start()
        
        self._on_exit_callback = None
        
        self._iface_modified = False
        
        self._apply_ip = apply_ip
        if self._apply_ip and not self._ifname:
            raise Exception('NoIfaceProvidedWithApplyIP')
        
        self._dump_packets = dump_packets
        
        if mac_addr is None:
            if self._ifname:
                self._mac_addr = MacAddr.getHwAddrForIf(ifname = self._ifname)
            elif self._listen_address != '0.0.0.0' and self._listen_address != '::':
                self._mac_addr = MacAddr.getHwAddrForIp(ip = self._listen_address)
            else:
                raise Exception('NoInterfaceProvided')
        
        self._current_xid = None
        self._xid_mutex = threading.Lock()      # This mutex protects writes to the _current_xid attribute
        self.genNewXid()    # Generate a random transaction ID for future packet exchanges
    
    def setOnExit(self, function):
        """
        Set the function that will be called when this object's exit() method is called (as a result of a D-Bus message or if .exit() is called directly
        """ 
        if not hasattr(function, '__call__'):    # Argument is not callable
            raise('NotAFunction')
        self._on_exit_callback = function
    
    # D-Bus-related methods
    def _loopHandleDbus(self):
        """
        This method should be run within a thread... This thread's aim is to run the Glib's main loop while the main thread does other actions in the meantime
        This methods will loop infinitely to receive and send D-Bus messages and will only stop looping when the value of self._loopDbus is set to False (or when the Glib's main loop is stopped using .quit()) 
        """
        if not self._silent_mode: print('Starting dbus mainloop')
        self._dbus_loop.run()
        if not self._silent_mode: print('Stopping dbus mainloop')
    
    @dbus.service.signal(dbus_interface = DBUS_SERVICE_INTERFACE)
    def DhcpDiscoverSent(self):
        """
        D-Bus decorated method to send the "DhcpDiscoverSent" signal
        """
        pass
    
    @dbus.service.signal(dbus_interface = DBUS_SERVICE_INTERFACE)
    def DhcpOfferRecv(self, ip, server):
        """
        D-Bus decorated method to send the "DhcpOfferRecv" signal
        """
        pass
    
    @dbus.service.signal(dbus_interface = DBUS_SERVICE_INTERFACE)
    def DhcpRequestSent(self):
        """
        D-Bus decorated method to send the "DhcpRequestSent" signal
        """
        pass
    
    @dbus.service.signal(dbus_interface = DBUS_SERVICE_INTERFACE)
    def DhcpRenewSent(self):
        """
        D-Bus decorated method to send the "DhcpRenewSent" signal
        """
        pass
        
    @dbus.service.signal(dbus_interface = DBUS_SERVICE_INTERFACE)
    def DhcpReleaseSent(self, ip):
        """
        D-Bus decorated method to send the "DhcpReleaseSent" signal
        """
        pass
    
    @dbus.service.signal(dbus_interface = DBUS_SERVICE_INTERFACE)
    def DhcpAckRecv(self, ip, netmask, defaultgw, dns, server, leasetime):
        """
        D-Bus decorated method to send the "DhcpAckRecv" signal
        """
        pass

    @dbus.service.signal(dbus_interface = DBUS_SERVICE_INTERFACE)
    def IpConfigApplied(self, interface, ip, netmask, defaultgw, leasetime, dns_space_sep, serverid):
        """
        D-Bus decorated method to send the "IpConfigApplied" signal
        """
        pass
    
    @dbus.service.signal(dbus_interface = DBUS_SERVICE_INTERFACE)
    def IpDnsReceived(self, dns_space_sep_list):
        """
        D-Bus decorated method to send the "IpDnsReceived" signal
        """
        pass
    
    @dbus.service.signal(dbus_interface = DBUS_SERVICE_INTERFACE)
    def LeaseLost(self):
        """
        D-Bus decorated method to send the "LeaseLost" signal
        """
        pass

    def exit(self):
        """
        Cleanup object and stop all threads
        """
        self.sendDhcpRelease()    # Release our current lease if any (this will also clear all DHCP-lease-related threads)
        self._dbus_loop.quit()    # Stop the D-Bus main loop
        if not self._on_exit_callback is None:
            self._on_exit_callback() 

    @dbus.service.method(dbus_interface = DBUS_SERVICE_INTERFACE, in_signature='', out_signature='i')
    def GetPid(self):
        """
        D-Bus method to output the PID of this process
        """
        return (int(os.getpid()))

    @dbus.service.method(dbus_interface = DBUS_SERVICE_INTERFACE, in_signature='', out_signature='')
    def Release(self):
        """
        D-Bus method to release our current DHCP lease
        """
        if not self._silent_mode: print("Received Release() command from D-Bus")
        self.sendDhcpRelease()

    @dbus.service.method(dbus_interface = DBUS_SERVICE_INTERFACE, in_signature='', out_signature='')
    def Discover(self):
        """
        D-Bus decorated method executed when receiving the D-Bus "Discover" message call
        This method will force to send a DHCP discovery (but won't release the previous lease, nor remove its config from the internal records or from the pysical interface)
        Use with care! 
        """
        self.sendDhcpDiscover(release = False)

    @dbus.service.method(dbus_interface = DBUS_SERVICE_INTERFACE, in_signature='', out_signature='')
    def Renew(self):
        """
        D-Bus decorated method executed when receiving the D-Bus "Renew" message call
        This method will force a renew before the renew timeout
        """
        self.sendDhcpRenew()

    @dbus.service.method(dbus_interface = DBUS_SERVICE_INTERFACE, in_signature='', out_signature='')
    def Restart(self):
        """
        D-Bus decorated method executed when receiving the D-Bus "Restart" message call
        This method will force restarting the whole DHCP discovery process from the beginning
        """
        self.sendDhcpDiscover(release = True)    # Restart the DHCP discovery (and release our current lease if any (this will also clear all DHCP-lease-related threads))

    @dbus.service.method(dbus_interface = DBUS_SERVICE_INTERFACE, in_signature='', out_signature='')
    def FreezeRenew(self):
        """
        D-Bus decorated method executed when receiving the D-Bus "FreezeRenew" message call
        This method will stop any renew from being sent (even after the lease will expire)
        It will also stop any release from being sent out... basically, we will mute the DHCP client messaging to the server
        """
        if not self._renew_thread is None: self._renew_thread.cancel()    # Cancel the renew timeout
        if not self._release_thread is None: self._release_thread.cancel()    # Cancel the release timeout
    
    @dbus.service.method(dbus_interface = DBUS_SERVICE_INTERFACE, in_signature='', out_signature='s')
    def GetVersion(self):
        """
        D-Bus decorated method executed when receiving the D-Bus "GetVersion" message call
        This method will return the version of this program.
        It can also be used to make sure that this process is running (as a heartbeat or ping)
        """
        global VERSION
        return VERSION
    
    @dbus.service.method(dbus_interface = DBUS_SERVICE_INTERFACE, in_signature='', out_signature='s')
    def GetInterface(self):
        """
        D-Bus decorated method executed when receiving the D-Bus "GetInterface" message call
        This method will return the network interface on which this program is acting as a DHCP client.
        """
        return self._ifname
    
    @dbus.service.method(dbus_interface = DBUS_SERVICE_INTERFACE, in_signature='s', out_signature='')
    def Debug(self, msg):
        """
        D-Bus decorated method executed when receiving the D-Bus "Debug" message call
        This method will just echo on stdout the string given as argument
        """
        if not self._silent_mode: print('Received echo message from D-Bus: "' + str(msg) + '"')
    
    # IP self configuration-related methods
    def applyIpAddressFromDhcpLease(self):
        """
        Apply the IP address and netmask that we currently have in out self._dhcp_status (got from last lease)
        Warning : we won't check if the lease is still valid now, this is up to the caller
        """ 
        self._iface_modified = True
        cmdline = ['ifconfig', str(self._ifname), '0.0.0.0']
        if not self._silent_mode: print(cmdline)
        subprocess.call(cmdline)
        if self._dhcp_status.ipv4_address:
            cmdline = ['ifconfig', str(self._ifname), str(self._dhcp_status.ipv4_address), 'netmask', str(self._dhcp_status.ipv4_netmask)]
            if not self._silent_mode: print(cmdline)
            subprocess.call(cmdline)
    
    def applyDefaultGwFromDhcpLease(self):
        """
        Apply the default gateway that we currently have in out self._dhcp_status (got from last lease)
        Warning : we won't check if the lease is still valid now, this is up to the caller
        """ 
        self._iface_modified = True
        if self._dhcp_status.ipv4_defaultgw:
            cmdline = ['route', 'add', 'default', 'gw', str(self._dhcp_status.ipv4_defaultgw)]
            if not self._silent_mode: print(cmdline)
            subprocess.call(cmdline)

    # DHCP-related methods
    def genNewXid(self):
        """
        Generate a new random DHCP transaction ID
        It will be stored inside the _current_xid property of this object and used in all subsequent DHCP packets sent by this object
        It can be retrieved using getXid()
        """
        with self._xid_mutex:
            self._current_xid = self._random.randint(0,0xffffffff)
    
    def _getXitAsDhcpOption(self):
        """
        Get the current xid property of this object, encoded as a DhcpOption format that can be used with DhcpPacket.SetOption()
        The format returned is an array of 4 bytes
        """
        if self._current_xid is None:
            return None
        xid = []
        decxid = self._current_xid
        for i in xrange(4):
            xid.insert(0, decxid & 0xff)
            decxid = decxid >> 8
        return xid
    
    def setXid(self, xid):
        """
        Set the transaction ID that will be used for all subsequent DHCP packets sent by us
        We are expecting a 32-bit integer as argument xid
        """
        with self._xid_mutex:
            self._current_xid = xid
    
    def getXid(self):
        """
        Get the transaction ID that is currently used for all DHCP packets sent by us
        """
        return self._current_xid
    
    def _unconfigure_iface(self):
        """
        Unconfigure our interface (fall back to its default system config)
        Warning, we will not modify the current lease information stored in this object however
        """
        if self._iface_modified:    # Clean up our ip configuration (revert to standard config for this interface)
            if self._ifname:
                cmdline = ['ifdown', str(self._ifname)]
                if not self._silent_mode: print(cmdline)
                subprocess.call(cmdline, stdout=open(os.devnull, 'wb'), stderr=subprocess.STDOUT)
                time.sleep(0.2)    # Grrrr... on some implementations, ifdown returns too early (before actually doing its job)
                cmdline = ['ifconfig', str(self._ifname), '0.0.0.0', 'down']    # Make sure we get rid of the IP address
                if not self._silent_mode: print(cmdline)
                subprocess.call(cmdline)
                cmdline = ['ifup', str(self._ifname)]
                if not self._silent_mode: print(cmdline)
                subprocess.call(cmdline, stdout=open(os.devnull, 'wb'), stderr=subprocess.STDOUT)
                self._iface_modified = False

    def sendDhcpDiscover(self, parameter_list = None, release = True):
        """
        Send a DHCP DISCOVER packet to the network
        """
        # Cancel all renew and release threads
        if release:
            self.sendDhcpRelease()    # Release our current lease if any (this will also clear all DHCP-lease-related threads)
        
        dhcp_discover = DhcpPacket()
        dhcp_discover.SetOption('op', [1])
        dhcp_discover.SetOption('htype', [1])
        dhcp_discover.SetOption('hlen', [6])
        dhcp_discover.SetOption('hops', [0])
        dhcp_discover.SetOption('xid', self._getXitAsDhcpOption())
        dhcp_discover.SetOption('giaddr',ipv4('0.0.0.0').list())
        dhcp_discover.SetOption('chaddr',hwmac(self._mac_addr).list() + [0] * 10)
        dhcp_discover.SetOption('ciaddr',ipv4('0.0.0.0').list())
        dhcp_discover.SetOption('siaddr',ipv4('0.0.0.0').list())
        dhcp_discover.SetOption('dhcp_message_type', [dhcpNameToType('DISCOVER')])
        dhcp_discover.SetOption('client_identifier', [CLIENT_ID_HWTYPE_ETHER] + hwmac(self._mac_addr).list())
        if parameter_list is None:
            parameter_list =[1,    # Subnet mask
                3,    # Router
                6,    # DNS
                15,    # Domain
                42,    # NTP servers
                ]
        self._parameter_list = parameter_list
        dhcp_discover.SetOption('parameter_request_list', self._parameter_list)
        #client.dhcp_socket.settimeout(timeout)
        dhcp_discover.SetOption('flags',[128, 0])
        dhcp_discover_type = dhcp_discover.GetOption('dhcp_message_type')[0]
        if not self._silent_mode: print("==>Sending DISCOVER")
        self._request_sent = False
        bytes_sent = self.SendDhcpPacketTo(dhcp_discover, '255.255.255.255', self._server_port)
        if bytes_sent == 0:
            raise Exception('FailedSendDhcpPacketTo')
        self.DhcpDiscoverSent()    # Emit DBUS signal
    
    def handleDhcpOffer(self, res):
        """
        Handle a DHCP OFFER packet coming from the network
        """
        dhcp_offer = res
        dhcp_message_type = dhcp_offer.GetOption('dhcp_message_type')[0]
        message = "==>Received " + dhcpTypeToName(dhcp_message_type, False)
        if self._dump_packets:
            message += ' with content:'
        if not self._silent_mode: print(message)
        if self._dump_packets:
            print(dhcp_offer.str())
        
        proposed_ip = ipv4(dhcp_offer.GetOption('yiaddr'))
        server_id = ipv4(dhcp_offer.GetOption('server_identifier'))
        self.DhcpOfferRecv('IP ' + str(proposed_ip), 'SERVER ' + str(server_id))    # Emit DBUS signal with proposed IP address
        self.sendDhcpRequest(requested_ip = proposed_ip, server_id = server_id)
    
    def HandleDhcpOffer(self, res):
        """
        Inherited DhcpClient has virtual methods written with an initial capital, so wrap around it to use our method naming convention
        """
        self.handleDhcpOffer(res)
    
    def sendDhcpRequest(self, requested_ip = '0.0.0.0', server_id = '0.0.0.0', dstipaddr = '255.255.255.255'):
        """
        Send a DHCP REQUEST packet to the network
        """
        dhcp_request = DhcpPacket()
        dhcp_request.SetOption('op', [1])
        dhcp_request.SetOption('htype', [1])
        dhcp_request.SetOption('hlen', [6])
        dhcp_request.SetOption('hops', [0])
        dhcp_request.SetOption('xid', self._getXitAsDhcpOption())
        dhcp_request.SetOption('giaddr', ipv4('0.0.0.0').list())
        dhcp_request.SetOption('chaddr', hwmac(self._mac_addr).list() + [0] * 10)
        dhcp_request.SetOption('ciaddr', ipv4('0.0.0.0').list())
        dhcp_request.SetOption('siaddr', ipv4('0.0.0.0').list())
        if isinstance(requested_ip, basestring):    # In python 3, this would be isinstance(x, str)
            requested_ip = ipv4(requested_ip)
        if isinstance(server_id, basestring):
            server_id = ipv4(server_id)
        dhcp_request.SetOption('dhcp_message_type', [dhcpNameToType('REQUEST')])
        dhcp_request.SetOption('client_identifier', [CLIENT_ID_HWTYPE_ETHER] + hwmac(self._mac_addr).list())
        dhcp_request.SetOption('request_ip_address', requested_ip.list())
        dhcp_request.SetOption('server_identifier', server_id.list())
        if not self._parameter_list is None:
            dhcp_request.SetOption('parameter_request_list', self._parameter_list)    # Resend the same parameter list as for DISCOVER
        #self.dhcp_socket.settimeout(timeout)
        dhcp_request.SetOption('flags', [128, 0])
        dhcp_request_type = dhcp_request.GetOption('dhcp_message_type')[0]
        if not self._silent_mode: print("==>Sending REQUEST")
        bytes_sent = self.SendDhcpPacketTo(dhcp_request, dstipaddr, self._server_port)
        if bytes_sent == 0:
            raise Exception('FailedSendDhcpPacketTo')
        self._request_sent = True
        self.DhcpRequestSent()    # Emit DBUS signal
        
    def sendDhcpRenew(self, ciaddr = None, dstipaddr = '255.255.255.255'):
        """
        Send a DHCP REQUEST to renew the current lease
        This is almost the same as the REQUEST following a DISCOVER, but we provide our client IP address here
        """
        if not self._renew_thread is None:    # If there was a lease currently obtained
            self._renew_thread.cancel()
            self._renew_thread = None
        
        self.genNewXid()    # Generate a new transaction
        dhcp_request = DhcpPacket()
        dhcp_request.SetOption('op', [1])
        dhcp_request.SetOption('htype', [1])
        dhcp_request.SetOption('hlen', [6])
        dhcp_request.SetOption('hops', [0])
        dhcp_request.SetOption('xid', self._getXitAsDhcpOption())
        dhcp_request.SetOption('giaddr', ipv4('0.0.0.0').list())
        dhcp_request.SetOption('chaddr', hwmac(self._mac_addr).list() + [0] * 10)
        if ciaddr is None:
            with self._dhcp_status._dhcp_status_mutex:    # Hold the mutex so that ipv4_lease_valid and ipv4_address remain coherent for the whole operation
                if self._dhcp_status.ipv4_lease_valid:
                    ciaddr = ipv4(self._dhcp_status.ipv4_address)
                else:
                    raise Exception('RenewOnInvalidLease')
        dhcp_request.SetOption('ciaddr', ciaddr.list())
        dhcp_request.SetOption('siaddr', ipv4('0.0.0.0').list())
        dhcp_request.SetOption('dhcp_message_type', [dhcpNameToType('REQUEST')])
        dhcp_request.SetOption('client_identifier', [CLIENT_ID_HWTYPE_ETHER] + hwmac(self._mac_addr).list())
        if not self._parameter_list is None:
            dhcp_request.SetOption('parameter_request_list', self._parameter_list)    # Resend the same parameter list as for DISCOVER
        dhcp_request.SetOption('flags', [128, 0])
        dhcp_request_type = dhcp_request.GetOption('dhcp_message_type')[0]
        if not self._silent_mode: print("==>Sending REQUEST (renewing lease)")
        self.DhcpRenewSent()    # Emit DBUS signal
        self._request_sent = True
        bytes_sent = self.SendDhcpPacketTo(dhcp_request, dstipaddr, self._server_port)
        if bytes_sent == 0:
            raise Exception('FailedSendDhcpPacketTo')
        # After the first renew is sent, increase the frequency of the next renew packets (send 5 more renew during the second half of the lease)
        self._renew_thread = threading.Timer(self._dhcp_status.ipv4_lease_duration / 5 / 2, self.sendDhcpRenew, [])
        
        self._renew_thread.setDaemon(True)
        self._renew_thread.start()

    
    def sendDhcpRelease(self, ciaddr = None, unconfigure_iface = True):
        """
        Send a DHCP RELEASE to release the current lease
        """
        if not self._renew_thread is None: self._renew_thread.cancel()    # Cancel the renew timeout
        if not self._release_thread is None: self._release_thread.cancel()    # Cancel the release timeout
        self._release_thread = None    # Delete pointer to our own thread handle now that we have been called
        if not self._renew_thread is None:    # If there was a lease currently obtained
            self._renew_thread = None    # Delete pointer to the renew (we have lost our lease)
            
            with self._dhcp_status._dhcp_status_mutex:    # Copy locally the values used in the next part so that they are coherent (even if obsolete)
                ipv4_lease_valid = self._dhcp_status.ipv4_lease_valid
                ipv4_address = self._dhcp_status.ipv4_address
                ipv4_dhcpserverid = self._dhcp_status.ipv4_dhcpserverid
            
            if ipv4_lease_valid and ipv4_address:    # Do we have a lease and a valid IPv4 address?
                self.genNewXid()
                dhcp_release = DhcpPacket()
                dhcp_release.SetOption('op', [1])
                dhcp_release.SetOption('htype', [1])
                dhcp_release.SetOption('hlen', [6])
                dhcp_release.SetOption('hops', [0])
                dhcp_release.SetOption('xid', self._getXitAsDhcpOption())
                dhcp_release.SetOption('giaddr', ipv4('0.0.0.0').list())
                dhcp_release.SetOption('chaddr', hwmac(self._mac_addr).list() + [0] * 10)
                dhcp_release.SetOption('ciaddr', ipv4(ipv4_address).list())
                dhcp_release.SetOption('siaddr', ipv4('0.0.0.0').list())
                dhcp_release.SetOption('dhcp_message_type', [dhcpNameToType('RELEASE')])
                dhcp_release.SetOption('client_identifier', [CLIENT_ID_HWTYPE_ETHER] + hwmac(self._mac_addr).list())
                if ipv4_dhcpserverid:
                    dhcp_release.SetOption('server_identifier', ipv4(ipv4_dhcpserverid).list())
                #self.dhcp_socket.settimeout(timeout)
                dhcp_release.SetOption('flags', [128, 0])
                dhcp_release_type = dhcp_release.GetOption('dhcp_message_type')[0]
                if not self._silent_mode: print("==>Sending RELEASE")
                release_sent_message = 'IP ' + str(ipv4_address)    # Build a string for the D-Bus signal now before erasing _last_ipaddress
                self._request_sent = False
                self._dhcp_status.reset()
                self.LeaseLost()    # Notify that the lease becomes invalid via a D-Bus signal
                
                bytes_sent = self.SendDhcpPacketTo(dhcp_release, '255.255.255.255', self._server_port) 
                if bytes_sent == 0:
                    raise Exception('FailedSendDhcpPacketTo')
                self.DhcpReleaseSent(release_sent_message)    # Emit D-Bus signal
                
            if unconfigure_iface:
                self._unconfigure_iface()    # Clean up our IP configuration (revert to standard config for this interface)
    
    def handleDhcpAck(self, packet):
        """
        Handle a DHCP ACK packet coming from the network
        """
        message = "==>Received ACK"
        if self._dump_packets:
            message += ' with content:'
        if not self._silent_mode: print(message)
        if self._dump_packets:
            print(packet.str())
        
        if self._request_sent:
            self._request_sent = False
        else:
            if not self._silent_mode: print("Received an ACK without having sent a REQUEST")
            #raise Exception('UnexpectedAck')
        
        ipv4_address = str(ipv4(packet.GetOption('yiaddr')))
        ipv4_netmask = str(ipv4(packet.GetOption('subnet_mask')))
        ipv4_defaultgw = str(ipv4(packet.GetOption('router')))
        ipv4_dnslist = []
        dnsip_array = packet.GetOption('domain_name_server')    # DNS is of type ipv4+ so we could get more than one router IPv4 address... handle all DNS entries in a list
        for i in range(0, len(dnsip_array), 4):
            if len(dnsip_array[i:i+4]) == 4:
                ipv4_dnslist += [str(ipv4(dnsip_array[i:i+4]))]
        ipv4_dhcpserverid = str(ipv4(packet.GetOption('server_identifier')))
        ipv4_lease_duration = ipv4(packet.GetOption('ip_address_lease_time')).int()

        
        with self._dhcp_status._dhcp_status_mutex:
            self._dhcp_status.ipv4_address = ipv4_address
            self._dhcp_status.ipv4_netmask = ipv4_netmask
            self._dhcp_status.ipv4_defaultgw = ipv4_defaultgw    # router is of type ipv4+ so we could get more than one router IPv4 address... but we only pick up the first one here
            self._dhcp_status.ipv4_dnslist = ipv4_dnslist
            self._dhcp_status.ipv4_dhcpserverid = ipv4_dhcpserverid
            self._dhcp_status.ipv4_lease_duration = ipv4_lease_duration
            self._dhcp_status.ipv4_lease_valid = True
            
        dns_space_sep = ' '.join(ipv4_dnslist)
        
        self.DhcpAckRecv('IP ' + str(ipv4_address),
            'NETMASK ' + str(ipv4_netmask),
            'DEFAULTGW ' + str(ipv4_defaultgw),
            'DNS ' + dns_space_sep,
            'SERVER ' + str(ipv4_dhcpserverid),
            'LEASEDURATION ' + str(ipv4_lease_duration))
        
        if not self._silent_mode: print('Starting renew thread')
        if not self._renew_thread is None: self._renew_thread.cancel()    # Cancel the renew timeout
        if not self._release_thread is None: self._release_thread.cancel()    # Cancel the release timeout
        
        self._renew_thread = threading.Timer(ipv4_lease_duration / 2, self.sendDhcpRenew, [])
        self._renew_thread.setDaemon(True)
        self._renew_thread.start()
        self._release_thread = threading.Timer(ipv4_lease_duration, self.sendDhcpRelease, [])    # Restart the release timeout
        self._release_thread.setDaemon(True)
        self._release_thread.start()
        
        if self._apply_ip and self._ifname:
            if not self._silent_mode: print('Applying IP config and Sending D-Bus Signal IpConfigApplied')
            self.applyIpAddressFromDhcpLease()
            self.applyDefaultGwFromDhcpLease()
            self.IpConfigApplied(str(self._ifname), str(ipv4_address), str(ipv4_netmask), str(ipv4_defaultgw), str(ipv4_lease_duration), dns_space_sep, str(ipv4_dhcpserverid))
            self.IpDnsReceived(dns_space_sep)
    
    def HandleDhcpAck(self, packet):
        """
        Inherited DhcpClient has virtual methods written with an initial capital, so wrap around it to use our method naming convention
        """
        self.handleDhcpAck(packet)
    
    def handleDhcpNack(self, packet):
        """
        Handle a DHCP NACK packet coming from the network
        Today, this will raise an exception. No processing will be done on such packets
        """
        
        message = "==>Received NACK"
        if self._dump_packets:
            message += ' with content:'
        if not self._silent_mode: print(message)
        if self._dump_packets:
            print(packet.str())

        self._dhcp_status.reset()
        self._request_sent = False
            
        self.LeaseLost()
        
        raise Exception('DhcpNack')
    
    def HandleDhcpNack(self, packet):
        """
        Inherited DhcpClient has virtual methods written with an initial capital, so wrap around it to use our method naming convention
        """
        self.handleDhcpNack(packet)


dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)	# Use Glib's mainloop as the default loop for all subsequent code

if __name__ == '__main__':
	atexit.register(cleanupAtExit)
	
	parser = argparse.ArgumentParser(description="This program launches a DHCP client daemon. \
It will report every DHCP client state change via D-Bus signal. \
It will also accept D-Bus method calls to change its behaviour (see Discover(), Renew(), Restart(), Release() etc... methods.", prog=progname)
	parser.add_argument('-i', '--ifname', type=str, help='network interface on which to send/receive DHCP packets', required=True)
	parser.add_argument('-A', '--applyconfig', action='store_true', help='apply the IP config (ip address, netmask and default gateway) to the interface when lease is obtained')
	parser.add_argument('-D', '--dumppackets', action='store_true', help='dump received packets content', default=False)
	parser.add_argument('-S', '--startondbus', action='store_true', help='only start the DHCP client when receiving a D-Bus Discover() method (also suppresses all stdout output)', default=False)
	parser.add_argument('-d', '--debug', action='store_true', help='display debug info', default=False)
	args = parser.parse_args()
	
	system_bus = dbus.SystemBus(private=True)
	gobject.threads_init()	# Allow the mainloop to run as an independent thread
	dbus.mainloop.glib.threads_init()
	
	name = dbus.service.BusName(DBUS_NAME, system_bus)      # Publish the name to the D-Bus so that clients can see us
	
	lockfilename = '/var/lock/' + progname + '.' + args.ifname
	
	signal.signal(signal.SIGINT, signalHandler)	# Install a cleanup handler on SIGINT and SIGTERM
	signal.signal(signal.SIGTERM, signalHandler)
	
	main_lock = lockfile.FileLock(lockfilename)
	try:
		main_lock.acquire(timeout = 0)
		
		client = DBusControlledDhcpClient(ifname = args.ifname, conn = system_bus, dbus_loop = gobject.MainLoop(), apply_ip = args.applyconfig, dump_packets = args.dumppackets, silent_mode = (not args.debug))	# Instanciate a dhcpClient (incoming packets will start getting processing starting from now...)
		#client.setOnExit(exit)
		
		if not args.startondbus:
			client.sendDhcpDiscover()	# Send a DHCP DISCOVER on the network
		
		try:
			while True:	client.GetNextDhcpPacket()	# Handle incoming DHCP packets
		except select.error as ex:	# Catch select error 4 (interrupted system call)
			if ex[0] == 4:
				#print(progname + ': Terminating', file=sys.stderr)
				pass
			else:
				raise  
		finally:
			if not client is None:
				client.exit()
			client = None
	except lockfile.AlreadyLocked:
		print(progname + ': Error: Could not get lock on file ' + lockfilename + '.lock', file=sys.stderr)
