'''
Rather simple wrapper around the Twisted callback version until we have time
to migrate the BitcoinClientProtocol using gevent.

Created on Dec 15, 2013

@author: cdecker
'''

#from gevent.server import StreamServer
from gevent import socket, spawn_later
import struct
#from gevent.socket import create_connection
#from gevent import socket as gsocket

from bitcoin.messages import GetDataPacket, BlockPacket, TxPacket, InvPacket,\
    VersionPacket, Address, AddrPacket
from _pyio import BytesIO
from bitcoin.BitcoinProtocol import get_external_ip, serialize_packet
from gevent.greenlet import Greenlet
from gevent.pool import Group
import random

class ConnectionLostException(Exception):
    pass

network_params = {
    "magic": "D9B4BEF9".decode("hex")[::-1],
    "port": 8333,
}

parsers = {
           "version": VersionPacket,
           "inv": InvPacket,
           "tx": TxPacket,
           "block": BlockPacket,
           "getdata": GetDataPacket,
           "addr": AddrPacket,
}

class NetworkClient():
    """
    Class that collects all the necessary meta information about this client. 
    """
    def __init__(self):
        self.external_ip = get_external_ip()
        self.port = 8333
        self.connections = {}
        self.connection_group = Group()
    
    def get_blockchain_height(self):
        return 0
    
    def join(self):
        self.connection_group.join()
    
    def connect(self, host):
        c = Connection(host, self, False)
        g = Greenlet.spawn(c.connect_and_run)
        self.connection_group.add(g)
        self.connections[host] = c
        return c
    
    def remove_connection(self, connection):
        self.connections.pop(connection.address, None)

class PooledNetworkClient(NetworkClient):
    def __init__(self, pool_size=500):
        NetworkClient.__init__(self)
        self.pool_size = pool_size
        self.open_connections = set()
        self.unreachable_peers = set()
        self.known_peers = set()
        spawn_later(5, self.pool_maintenance)
        # TODO implement
        
    def connect(self, host):
        """
        Patch into connection creation in order to catch addr messages.
        """
        self.open_connections |= set([host])
        c = NetworkClient.connect(self, host)
        c.handlers['addr'].append(self.on_addr_message)
        c.handlers['disconnect'].append(self.on_disconnect)
        return c
    
    def on_disconnect(self, connection, reason):
        self.open_connections -= set([connection.address])
        # TODO distinguish whether this is a failure or regular closure
        if not isinstance(reason, ConnectionLostException):
            self.unreachable_peers |= set([connection.address])
    
    def on_addr_message(self, connection, message):
        self.known_peers |= set([(a.ip, a.port) for a in message.addresses])
        
    def pool_maintenance(self):
        spawn_later(5, self.pool_maintenance)
        print "Current connection pool: %d connections, %d known peers, %d marked as unreachable" % (len(self.open_connections), len(self.known_peers), len(self.unreachable_peers))
        if len(self.open_connections) >= self.pool_size:
            return
        available_peers = self.known_peers - self.open_connections - self.unreachable_peers
        if len(available_peers) < 1:
            print "No more peers available for connection"
        for c in random.sample(available_peers,min(len(available_peers), 20, self.pool_size - len(self.open_connections))):
            self.connect(c)

class Connection(object):
    
    def __init__(self, address, client, incoming=False, socket=None):
        self.address = address
        self.socket = socket
        self.incoming = incoming
        self.client = client
        self.bytes_out = 0
        self.bytes_in = 0
        self.version = None
        self.handlers = {
                         "ping": [self.handle_ping],
                         "inv": [self.print_inv],
                         "addr": [],
                         # Virtual events for connection and disconnection
                         "connect": [],
                         "disconnect": [],
                         "version": [self.on_version_message]
                         }
    

    def connect(self, timeout=5):
        self.socket = socket.create_connection(self.address,timeout=timeout)
        for h in self.handlers.get("connect", []):
            h(self)

    def on_version_message(self, connection, version):
        self.version = version
        self._send("verack")
        self.connected = True

    def connect_and_run(self):
        try:
            self.connect()
            self.run()
        except Exception as e:
            self.terminate(e)
    
    def run(self):
        if not self.socket:
            raise Exception("Not connected")
        try:
            if not self.incoming:
                # We have to send a version message first
                self.send_version()
        
            while True:
                command = self.read_command()
                if command == None:
                    continue
                for h in self.handlers.get(command.type, []):
                    h(self, command)
        except Exception as e:
            self.terminate(e)
        
    def terminate(self, reason):
        self.connected = False
        if self.socket and not self.socket.closed:
            self.socket.close()
        for h in self.handlers.get("disconnect", []):
            h(self, reason)
        self.client.remove_connection(self)        
        
    def read_command(self):
        header = self.socket.recv(24)
        if header < 24:
            raise ConnectionLostException()
        
        # Drop the checksum for now
        magic, command, length, _ = struct.unpack("<4s12sII", header)
        if network_params['magic'] != magic:
            raise ConnectionLostException()
        
        payload = self.socket.recv(length)
        if len(payload) < length:
            raise ConnectionLostException()
        
        command = command.strip("\x00")
        if command not in parsers.keys():
            return None
        packet = parsers[command.strip()]()
        packet.parse(BytesIO(payload))
        return packet
        
    def handle_ping(self):
        pass
    
    def send_version(self):
        v = VersionPacket()
        v.addr_from = Address(self.client.external_ip, True, self.client.port, 1)
        v.addr_recv = Address(self.address[0], True, self.address[1], 1)
        v.best_height = 0
        self._send("version", v)
    
    def print_inv(self, connection, packet):
        #print [(h[0], h[1].encode("hex")) for h in packet.hashes]
        pass
    
    def _send(self, packetType, payload=""):
        """
        Utility method to calculate the checksum, the payload length and combine
        everything into a nice package.
        """
        message = serialize_packet(packetType, payload, network_params)
        self.bytes_out += len(message)
        self.socket.send(message)