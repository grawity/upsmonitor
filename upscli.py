#!/usr/bin/env python
# -*- coding: utf-8; indent-tabs-mode: t; tab-width: 4 -*- vim: noet
from __future__ import print_function
from __future__ import with_statement

import math
import os
import shlex
import socket
import struct
import sys

def configpaths(name):
	return [os.path.join(sys.path[0], ".%s" % name),
	        os.path.expanduser("~/.%s" % name),
	        os.path.expanduser("~/.config/%s" % name)]

def loadservers(path):
	# .upslist.conf contains a list of UPS addreses, one 'ups@host' per line, with
	# optional description after the address. An address with only '@host' means
	# an apcupsd server instead of a NUT server.
	servers = []
	with open(path, "r") as fh:
		for line in fh:
			line = line.rstrip()
			if not line:
				continue
			if line.startswith("#"):
				continue
			line = line.split(None, 1)
			upsaddr = line[0]
			upsdesc = line[1] if len(line) >= 2 else None
			servers.append((upsaddr, upsdesc))
	return servers

def tryloadservers(paths):
	for path in paths:
		try:
			return loadservers(path)
		except (OSError, IOError):
			pass
	return []

def hms(seconds):
	t = seconds;	h = t // 3600
	t = t % 3600;	m = t // 60
	t = t % 60;	s = t
	if h > 0:
		return "%dh %02dm" % (h, m)
	else:
		return "%02dm" % (m,)

def gauge(value, width, max_value=100):
	assert width >= len("[##]")
	ceil = lambda x: int(math.ceil(x))
	floor = lambda x: int(math.floor(x))
	max_width = width - len("[]")
	if value is None:
		bar = "-" * max_width
	else:
		fill_width = max_width * value / max_value
		bar = "#" * ceil(fill_width) + " " * floor(max_width-fill_width)
	return "[%s]" % bar

class UpsError(Exception):
	pass

class UpsProtocolError(IOError):
	pass

class Ups:
	PORT = 0
	FMODE = "rw"

	def __init__(self, address):
		self.upsname, _, self.hostname = address.rpartition("@")
		self.hostname = self.hostname or "localhost"
		self.address = "%s@%s" % (self.upsname, self.hostname)
		self.sock = None
		self.stream = None

	def __repr__(self):
		return "Ups(%r)" % self.address

	def connect(self):
		res = socket.getaddrinfo(self.hostname,
								 self.PORT,
								 socket.AF_UNSPEC,
								 socket.SOCK_STREAM)
		for (af, kind, proto, cname, addr) in res:
			self.sock = socket.socket(af, kind, proto)
			self.sock.settimeout(2.0)
			self.sock.connect(addr)
			self.stream = self.sock.makefile(self.FMODE)
			break

	def tryconnect(self):
		if not self.sock:
			self.connect()

	def close(self):
		self.stream = tryclose(self.stream)
		self.sock = tryclose(self.sock)

class NutUps(Ups):
	PORT = 3493

	def __repr__(self):
		return "NutUps(%r)" % self.address

	def send(self, line):
		self.tryconnect()
		self.stream.write(line + "\n")
		self.stream.flush()

	def recv(self):
		line = self.stream.readline()
		if line:
			return line.rstrip("\r\n")
		else:
			return None

	@staticmethod
	def tokenize(line):
		# There isn't really any formal syntax for quoting or escaping;
		# NUT just directly emits a sprintf("VAR %s %s \"%s\"\n") and the
		# bundled Perl module regexes the quotes away.
		return shlex.split(line)

	def recvone(self):
		line = self.recv()
		if line is None:
			raise UpsProtocolError("End of stream")
		elif not line:
			raise UpsProtocolError("Empty line")
		try:
			words = self.tokenize(line)
		except ValueError:
			e = sys.exc_info(1)
			raise UpsProtocolError("Tokenize error - %s: %r" % (e, line))
		return words

	def recvlist(self):
		items = []
		topic = None
		while True:
			resp = self.recvone()
			if resp[0] == "ERR":
				raise UpsError(*resp[1:])
			elif resp[0] == "BEGIN":
				if topic is not None:
					raise UpsProtocolError("BEGIN in the middle of a list: %r" % (resp,))
				if len(resp) < 3 or resp[1] != "LIST":
					raise UpsProtocolError("Not enough parameters: %r" % (resp,))
				topic = resp[2]
			elif resp[0] == "END":
				if topic is None:
					raise UpsProtocolError("END without BEGIN: %r" % (resp,))
				if len(resp) < 3 or resp[1] != "LIST":
					raise UpsProtocolError("Not enough parameters: %r" % (resp,))
				break
			elif resp[0] == topic:
				items.append(tuple(resp[1:]))
			else:
				raise UpsProtocolError("Unexpected: %r" % (resp,))
		return items

	def listvars(self):
		self.send("LIST VAR %s" % self.upsname)
		resp = self.recvlist()
		vars = {}
		for ups, var, value in resp:
			vars[var] = value
		return vars

	def getvar(self, name):
		self.tryconnect()
		self.send("GET VAR %s %s" % (self.upsname, name))
		resp = self.recvone()
		if resp[0] == "ERR":
			if resp[1] == "VAR-NOT-SUPPORTED":
				raise KeyError(name)
			else:
				raise UpsError(*resp[1:])
		elif resp[0] == "VAR":
			if len(resp) < 4:
				raise UpsProtocolError("Not enough parameters: %r" % (resp,))
			elif resp[1:3] != (self.upsname, name):
				raise UpsProtocolError("Desynchronized: %r; expected: %r" % (resp, name))
			return resp[3]
		else:
			raise UpsProtocolError("Unexpected: %r" % (resp,))

class ApcupsdUps(Ups):
	PORT = 3551
	FMODE = "rwb"

	def __repr__(self):
		return "ApcupsdUps(%r)" % self.address

	def send(self, command):
		self.tryconnect()
		buf = command.encode("utf-8")
		buf = struct.pack(">h", len(buf)) + buf
		self.stream.write(buf)
		self.stream.flush()

	def recvone(self):
		buf = self.stream.read(2)
		length, = struct.unpack(">h", buf)
		buf = self.stream.read(length)
		return buf.decode("utf-8")

	def getstatus(self):
		self.send("status")
		vars = {}
		while True:
			buf = self.recvone()
			if len(buf) == 0:
				break
			key, _, val = buf.partition(": ")
			key = key.rstrip()
			val = val.rstrip("\n")
			if not vars and key != "APC":
				raise UpsProtocolError("Status did not start with 'APC': %r" % [key, val])
			if "END APC" in vars:
				raise UpsProtocolError("Unexpected data after 'END APC': %r" % [key, val])
			vars[key] = val
		if "END APC" not in vars:
			raise UpsProtocolError("Status did not finish with 'END APC': %r" % vars)
		return vars

	def listvars(self):
		intmap = {
			"BATTV":	"battery.voltage",
			"BCHARGE": 	"battery.charge",
			"LINEV":	"input.voltage",
			"LOADPCT":	"ups.load",
			"NOMPOWER":	"ups.realpower.nominal",
		}
		strmap = {
			"UPSNAME":	"ups.id",
		}
		statusmap = {
			"CAL":		"CAL",
			"TRIM":		"TRIM",
			"BOOST":	"BOOST",
			"ONLINE":	"OL",
			"ONBATT":	"OB",
			"OVERLOAD":	"OVER",
			"LOWBATT":	"LB",
			"REPLACEBATT":	"RB",
			# Mappings not yet checked against what NUT would show:
			"NOBATT":	"NOBATT?",
			"COMMLOST":	"COMMLOST?",
			"SELFTEST":	"SELFTEST?",
		}
		avars = self.getstatus()
		nvars = {}
		for akey, aval in avars.items():
			if akey in intmap:
				nvars[intmap[akey]] = float(aval.split()[0])
			elif akey in strmap:
				nvars[strmap[akey]] = aval.strip()
			elif akey == "TIMELEFT":
				aval, unit = aval.split()
				assert unit == "Minutes"
				nvars["battery.runtime"] = float(aval) * 60
			elif akey == "STATUS":
				if aval == "SHUTTING DOWN":
					nval = ["FSD"]
				elif aval == "NETWORK ERROR":
					nval = ["COMMLOST"]
				else:
					nval = [statusmap.get(v, "%s?" % v)
					        for v in aval.split()]
				nvars["ups.status"] = (" ".join(nval) or "UNKNOWN")
		return nvars

# Load configured hosts

confpaths = configpaths("upslist.conf")
if len(sys.argv) > 1:
	servers = [(a, None) for a in sys.argv[1:]]
else:
	servers = tryloadservers(confpaths)

# Poll all servers

descr_width = 0
status_width = 12 # len("*on battery*")
batt_width = 2+15
load_width = 2+10
pct_width = len("100%")
eta_width = max(len("9h 99m"), len("RUNTIME"))

descr_width = max([len(desc or addr) for addr, desc in servers])

print("%-*s" % (descr_width, "UPS"),
      "%-*s" % (status_width, "STATUS"),
      "%-*s" % (batt_width, "BATTERY"),
      #"%*s" % (pct_width, "BAT%"),
      "%*s" % (eta_width, "RUNTIME"),
      "%-*s" % (load_width, "LOAD"),
      "%*s" % (pct_width, "LOAD"),
      sep="  ")

print("-" * descr_width,
      "-" * status_width,
      "-" * batt_width,
      #"-" * pct_width,
      "-" * eta_width,
      "-" * load_width,
      "-" * pct_width,
      sep="  ")

for addr, desc in servers:
	if addr.startswith("@"):
		ups = ApcupsdUps("apcupsd" + addr)
	elif "@" in addr:
		ups = NutUps(addr)
	else:
		exit("error: Invalid UPS address '%s'." % (addr,))

	vars = ups.listvars()
	# XXX: ups.id is not factored into width calc; that needs a two-pass loop
	# (gather into a list of rows, then print)
	#desc = desc or vars.get("ups.id") or addr
	desc = desc or addr
	batt_pct = float(vars["battery.charge"])
	load_pct = float(vars["ups.load"]) if "ups.load" in vars else None
	batt_str = "%.0f%%" % batt_pct
	load_str = "%.0f%%" % load_pct if "ups.load" in vars else "n/a"
	eta_secs = float(vars["battery.runtime"])
	if eta_secs > 3600:
		eta_secs = round(eta_secs / 600) * 600		# 10 min. precision
	eta_str = hms(int(eta_secs))

	status_flags = vars["ups.status"]
	if status_flags == "OL":
		status_str = "online"
	elif status_flags == "OB":
		status_str = "*on battery*"
	else:
		status_flags = status_flags.split()
		if "OL" in status_flags:
			status_flags.remove("OL")
		status_str = (" ".join(status_flags))

	print("%-*s" % (descr_width, desc),
	      "%-*s" % (status_width, status_str),
	      "%-*s" % (batt_width, gauge(batt_pct, batt_width)),
	      #"%*s" % (pct_width, batt_str),
	      "%*s" % (eta_width, eta_str),
	      "%-*s" % (load_width, gauge(load_pct, load_width)),
	      "%*s" % (pct_width, load_str),
	      sep="  ")
